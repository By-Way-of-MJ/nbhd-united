"""Canary-gated OpenClaw sub-agent config and validation coverage."""

import json
import re
from copy import deepcopy
from pathlib import Path

from django.test import TestCase, override_settings

from apps.billing.constants import DEEPSEEK_MODEL, GEMMA_MODEL
from apps.orchestrator.config_generator import (
    SUBAGENT_READ_ONLY_TOOLS,
    generate_openclaw_config,
    subagents_enabled,
)
from apps.orchestrator.config_validator import assert_config_writable, validate_openclaw_config
from apps.orchestrator.personas import render_workspace_files
from apps.orchestrator.tool_policy import generate_tool_config
from apps.tenants.services import create_tenant


class SubagentCanaryConfigTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Subagent Config", telegram_chat_id=908001)

    @override_settings(SUBAGENT_TENANT_IDS="")
    def test_empty_gate_keeps_legacy_deny_and_subagents_blocks_exactly(self):
        config = generate_openclaw_config(self.tenant)
        baseline_tools = generate_tool_config("starter", version=self.tenant.openclaw_version)

        self.assertEqual(config["tools"]["deny"], baseline_tools["deny"])
        self.assertNotIn("subagents", config["tools"])
        self.assertEqual(
            config["agents"]["defaults"]["subagents"],
            {
                "maxConcurrent": 2,
                "model": config["agents"]["defaults"]["model"]["primary"],
            },
        )
        self.assertNotIn("nbhd-subagent-bridge", config.get("plugins", {}).get("allow", []))
        usage_entry = config["plugins"]["entries"].get("nbhd-usage-reporter")
        if usage_entry is not None:
            self.assertEqual(usage_entry, {"enabled": True})

    def test_non_allowlisted_tenant_snapshot_is_identical_to_empty_gate(self):
        with override_settings(SUBAGENT_TENANT_IDS=""):
            before = generate_openclaw_config(self.tenant)
        with override_settings(SUBAGENT_TENANT_IDS="00000000-0000-4000-8000-000000000999"):
            after = generate_openclaw_config(self.tenant)

        self.assertEqual(after, before)
        self.assertEqual(after["tools"]["deny"], before["tools"]["deny"])
        self.assertEqual(
            after["agents"]["defaults"]["subagents"],
            before["agents"]["defaults"]["subagents"],
        )

    def test_enabled_tenant_unlocks_exact_tools_and_gets_bounded_helpers(self):
        with override_settings(SUBAGENT_TENANT_IDS=f" {str(self.tenant.id).upper()} "):
            config = generate_openclaw_config(self.tenant)

        from apps.router.helper_runs import HELPER_TIMEOUT_SECONDS

        self.assertEqual(config["agents"]["defaults"]["subagents"]["runTimeoutSeconds"], HELPER_TIMEOUT_SECONDS)
        self.assertEqual(config["tools"]["loopDetection"], {"enabled": True})
        evidence = json.loads(Path(__file__).with_name("fixtures").joinpath("helper_runtime_94.json").read_text())
        self.assertEqual(evidence["version"], "2026.9.4")
        self.assertEqual(
            evidence["files"]["dist/zod-schema.agent-runtime-Ca6cjqf9.mjs"]["excerpts"][0]["text"],
            "const ToolLoopDetectionSchema = object({ enabled: boolean().optional() }).strict().optional();",
        )

        baseline_deny = generate_tool_config("starter", version=self.tenant.openclaw_version)["deny"]
        self.assertEqual(
            config["tools"]["deny"],
            [name for name in baseline_deny if name != "sessions_spawn"],
        )
        self.assertNotIn("sessions_spawn", config["tools"]["deny"])
        self.assertIn("subagents", config["tools"]["deny"])
        self.assertIn("agents_wait", config["tools"]["deny"])
        self.assertIn("sessions_yield", config["tools"]["deny"])
        for still_denied in (
            "sessions_send",
            "message",
            "gateway",
            "agents_list",
            "sessions_list",
            "sessions_history",
            "session_status",
            "nodes",
        ):
            self.assertIn(still_denied, config["tools"]["deny"])
        self.assertEqual(
            config["tools"]["subagents"],
            {"tools": {"allow": list(SUBAGENT_READ_ONLY_TOOLS)}},
        )
        self.assertNotIn("nbhd_send_to_user", SUBAGENT_READ_ONLY_TOOLS)
        for tool in ("agents_wait", "subagents", "sessions_spawn"):
            self.assertNotIn(tool, SUBAGENT_READ_ONLY_TOOLS)
        self.assertNotIn("nbhd_generate_image", SUBAGENT_READ_ONLY_TOOLS)
        self.assertNotIn("publish_portfolio_image", SUBAGENT_READ_ONLY_TOOLS)
        self.assertFalse([tool for tool in SUBAGENT_READ_ONLY_TOOLS if tool.startswith("shop_")])
        self.assertEqual(
            config["agents"]["defaults"]["subagents"],
            {
                "maxConcurrent": 2,
                "maxChildrenPerAgent": 2,
                "maxSpawnDepth": 1,
                "runTimeoutSeconds": 300,
                "announceTimeoutMs": 120000,
                "archiveAfterMinutes": 60,
                "model": config["agents"]["defaults"]["model"]["primary"],
                "delegationMode": "suggest",
            },
        )
        plugins = config["plugins"]
        self.assertIn("nbhd-subagent-bridge", plugins["allow"])
        self.assertEqual(
            next(path for path in plugins["load"]["paths"] if path.endswith("subagent-bridge")),
            "/opt/nbhd/plugins/nbhd-journal-tools/subagent-bridge",
        )
        self.assertEqual(
            plugins["entries"]["nbhd-subagent-bridge"],
            {
                "enabled": True,
                "hooks": {
                    "allowConversationAccess": True,
                    "timeoutMs": 30000,
                },
            },
        )
        self.assertEqual(
            plugins["entries"]["nbhd-usage-reporter"],
            {
                "enabled": True,
                "hooks": {"allowConversationAccess": True},
                "config": {"meterScopes": ["helper"]},
            },
        )

    def test_enabled_helpers_follow_tenant_chat_model_preference(self):
        for primary in (DEEPSEEK_MODEL, GEMMA_MODEL):
            with self.subTest(primary=primary), override_settings(SUBAGENT_TENANT_IDS=str(self.tenant.id)):
                self.tenant.preferred_model = primary
                config = generate_openclaw_config(self.tenant)
                defaults = config["agents"]["defaults"]
                self.assertEqual(defaults["model"]["primary"], primary)
                self.assertEqual(defaults["subagents"]["model"], primary)

    def test_gate_helper_is_fail_closed_and_case_insensitive(self):
        with override_settings(SUBAGENT_TENANT_IDS=""):
            self.assertFalse(subagents_enabled(self.tenant))
        with override_settings(SUBAGENT_TENANT_IDS=str(self.tenant.id).upper()):
            self.assertTrue(subagents_enabled(self.tenant))

    def test_gate_accepts_only_exact_wildcard_tokens(self):
        for raw, enabled in (("*", True), ("other, * ", True), ("prefix*", False), ("**", False)):
            with self.subTest(raw=raw), override_settings(SUBAGENT_TENANT_IDS=raw):
                self.assertEqual(subagents_enabled(self.tenant), enabled)
        with override_settings(SUBAGENT_TENANT_IDS="*"):
            self.assertFalse(subagents_enabled(None))
            config = generate_openclaw_config(self.tenant)
            self.assertNotIn("sessions_spawn", config["tools"]["deny"])
            self.assertIn("nbhd-subagent-bridge", config["plugins"]["allow"])

    def test_chat_offloads_concrete_requests_and_explains_tool_discovery(self):
        for gate in (str(self.tenant.id), "*"):
            with self.subTest(gate=gate), override_settings(SUBAGENT_TENANT_IDS=gate):
                prompt = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
                for marker in (
                    "Follow an `[NBHD routing: ...]` note on the current message",
                    "An explicit user request to work in the background always means spawn",
                    "`sessions_spawn` FIRST, before doing the work",
                    "2+ web searches",
                    "reading or summarising a long document or many pages",
                    "multi-part plan, report, or comparison",
                    "find it via tool search",
                    "On it: comparing those three vacuums. Keep chatting, I'll send it when it's done.",
                    "After spawning, do not wait or poll for the helper — reply with the acknowledgement "
                    "and end your turn; the result arrives later as an internal completion event.",
                    "Answer directly for everything else",
                    "Compare three holiday destinations",
                ):
                    self.assertIn(marker, prompt)
                self.assertNotIn("more than about 30 seconds", prompt)

        with override_settings(SUBAGENT_TENANT_IDS=""):
            prompt = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
            self.assertNotIn("## Slow tasks", prompt)
            self.assertNotIn("`sessions_spawn` FIRST", prompt)

    def test_python_and_javascript_helper_allowlists_are_equal(self):
        source = Path("runtime/openclaw/plugins/nbhd-routing-context/index.js").read_text()
        match = re.search(
            r"SUBAGENT_READ_ONLY_TOOL_IDS\s*=\s*new Set\(\[(.*?)\]\);",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        javascript_allowlist = tuple(re.findall(r'"([a-z0-9_-]+)"', match.group(1)))
        self.assertEqual(javascript_allowlist, SUBAGENT_READ_ONLY_TOOLS)


class SubagentConfigValidatorTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Subagent Validation", telegram_chat_id=908002)

    def _enabled_config(self):
        with override_settings(SUBAGENT_TENANT_IDS=str(self.tenant.id)):
            return generate_openclaw_config(self.tenant)

    def test_validator_accepts_enabled_nested_blocks(self):
        config = self._enabled_config()
        self.assertEqual(
            [issue for issue in validate_openclaw_config(config, strict=True) if issue.severity == "error"],
            [],
        )
        assert_config_writable(config)

    def test_validator_rejects_bad_subagent_knob_and_tool_allow_shape(self):
        config = self._enabled_config()
        broken = deepcopy(config)
        broken["agents"]["defaults"]["subagents"]["maxSpawnDepth"] = "one"
        broken["tools"]["subagents"]["tools"]["allow"] = "web_search"
        errors = [issue.path for issue in validate_openclaw_config(broken, strict=True) if issue.severity == "error"]
        self.assertIn("agents.defaults.subagents.maxSpawnDepth", errors)
        self.assertIn("tools.subagents.tools.allow", errors)
