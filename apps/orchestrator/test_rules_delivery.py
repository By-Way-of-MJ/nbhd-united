"""Rules-delivery boundaries for chat prompts and cron sessions."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from apps.orchestrator.config_generator import _prepare_cron_prompt
from apps.orchestrator.personas import PERSONAS, render_workspace_files
from apps.orchestrator.test_reminder_capability import MaximalTenantBudgetTest
from apps.tenants.models import Tenant, User
from apps.tenants.services import create_tenant

_ROOT = Path(__file__).resolve().parents[2]
_TOOLS_REFERENCE = _ROOT / "templates/openclaw/docs/tools-reference.md"
_TOOLS_REFERENCE_HEADER = (
    "<!-- CRON-ONLY: chat sessions cannot read this file; it is listed in the cron preamble. "
    "Tool descriptions are the authoritative reference. -->"
)

_CHAT_FILE_POINTER = re.compile(r"(?:rules|docs)/[a-z_-]+\.md")
_CRON_INDEX_ROWS = (
    ("rules/journal-capture.md", "Journal capture"),
    ("rules/lessons-constellation.md", "Lessons"),
    ("rules/messaging.md", "Cron delivery, check-in windows, automated routines"),
    ("rules/week-ahead.md", "Weekly review"),
    ("rules/fuel.md", "Fuel"),
    ("docs/tools-reference.md", "before using any tool you're unsure about"),
    ("docs/cron-management.md", "before creating, editing, or disabling scheduled tasks"),
    ("docs/error-handling.md", "when a tool fails or a feature isn't working"),
)


@override_settings(GRAVITY_ENABLED=True)
class RulesDeliveryTest(TestCase):
    def _all_gates_tenant(self):
        budget_case = MaximalTenantBudgetTest(methodName="test_maximal_render_stays_under_the_cap")
        return budget_case._tenant(all_gates=True)

    def test_chat_prompt_has_no_file_pointers(self):
        all_gates_tenant = self._all_gates_tenant()

        for persona_key in PERSONAS:
            for shape, tenant in (("lean", None), ("all-gates", all_gates_tenant)):
                with self.subTest(persona=persona_key, shape=shape):
                    prompt = render_workspace_files(persona_key, tenant=tenant)["NBHD_AGENTS_MD"]
                    self.assertIsNone(
                        _CHAT_FILE_POINTER.search(prompt),
                        f"{persona_key} {shape} chat prompt points at a workspace file",
                    )

    def test_cron_preamble_carries_rule_index(self):
        tenant = create_tenant(display_name="Cron Rule Index", telegram_chat_id=920101)
        with override_settings(SUBAGENT_TENANT_IDS=str(tenant.id)):
            cron_prompt = _prepare_cron_prompt("Task body", tenant)

        self.assertIn("On-demand rule files (you can read these in this session)", cron_prompt)
        self.assertIn(
            "Call `nbhd_journal_context` only when the task below needs recent journal/backbone context; "
            "otherwise skip it.",
            cron_prompt,
        )
        for path, load_for in _CRON_INDEX_ROWS:
            with self.subTest(path=path):
                self.assertIn(f"| `{path}` | {load_for} |", cron_prompt)
        self.assertNotIn("rules/subagents.md", cron_prompt)
        self.assertNotIn("| `rules/memory.md` |", cron_prompt)
        self.assertNotIn("| `rules/document-ingestion.md` |", cron_prompt)
        self.assertNotIn("| `rules/reply-markers.md` |", cron_prompt)
        self.assertNotIn("| `rules/onboarding.md` |", cron_prompt)

    def test_gated_chat_contains_inline_delegation_contract(self):
        tenant = create_tenant(display_name="Inline delegation", telegram_chat_id=920102)
        with override_settings(SUBAGENT_TENANT_IDS=str(tenant.id)):
            for persona_key in PERSONAS:
                prompt = render_workspace_files(persona_key, tenant=tenant)["NBHD_AGENTS_MD"]
                for marker in (
                    "Follow an `[NBHD routing: ...]` note on the current message",
                    "An explicit user request to work in the background always means spawn",
                    "2+ web searches",
                    "long document or many pages",
                    "multi-part plan, report, or comparison",
                    "`sessions_spawn` FIRST, before doing the work",
                    "find it via tool search",
                    "reply immediately",
                    "On it: comparing those three vacuums. Keep chatting, I'll send it when it's done.",
                    'Never pass `context: "fork"`',
                    "helper is read-only, reports back",
                    "write one final reply",
                    "requesting app thread",
                    "bridge delivers that reply",
                    "timeout/failure",
                ):
                    with self.subTest(persona=persona_key, marker=marker):
                        self.assertIn(marker, prompt)
                self.assertIsNone(_CHAT_FILE_POINTER.search(prompt))

    def test_ungated_chat_is_byte_identical_to_pre_change_render(self):
        # Hash captured from the base implementation, before the inline block.
        # Re-captured 2026-10-06: the chart sentence now bans character drawings on every channel.
        # Re-captured 2026-10-22: recall line in AGENTS.md.
        tenant = Tenant(user=User(username="synthetic", timezone="Asia/Tokyo", location_city="Tokyo"))
        for allowlist in ("", "00000000-0000-0000-0000-000000000001"):
            with (
                override_settings(SUBAGENT_TENANT_IDS=allowlist),
                patch("apps.orchestrator.personas.render_templates_md", return_value=""),
            ):
                prompt = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
            self.assertEqual(
                hashlib.sha256(prompt.encode()).hexdigest(),
                "1f01c454714a687fb91ca11b855d65be2e3905cdfca5afcff2d5a465740c37aa",
            )

    def test_all_gates_render_within_pin(self):
        prompt = render_workspace_files("neighbor", tenant=self._all_gates_tenant())["NBHD_AGENTS_MD"]

        # Measured after rules-delivery W0: 21,920 chars (R0 ceiling 22,759). 2026-08-30 KSE-9: the approval-code Website edit gate yields 23,505; runtime cap BOOTSTRAP_MAX_CHARS=26,000.
        # 2026-10-06: the one-line Shop gate (site-publishing tenants) yields 23,682; still 2,268 under the 25,950 alarm.
        # 2026-10-22: recall line (read vague asks against open tasks; retry recall once) yields 23,888; 2,062 under the 25,950 alarm.
        self.assertLessEqual(len(prompt), 23_888)


class ToolsReferenceCronStubTest(SimpleTestCase):
    def test_stub_is_small_cron_only_and_keeps_only_cross_tool_contracts(self):
        content = _TOOLS_REFERENCE.read_text()

        self.assertTrue(content.startswith(_TOOLS_REFERENCE_HEADER))
        self.assertLessEqual(len(content), 2_500)
        self.assertIn("last line, or immediately before a final `[[quick-replies: ...]]` line", content)
        self.assertIn("**Session-start check:**", content)
        self.assertIn("the user has explicitly approved it", content)
        self.assertIn(
            "For Fuel plans/fill-ins, first use `tool_search` for exact `nbhd_fuel_search_exercises` and "
            "call it per accessory/mobility group; then find/call `nbhd_fuel_create_plan`/"
            "`nbhd_fuel_update_plan`. Plans four weeks or longer rotate accessories every 1–2 weeks.",
            content,
        )
        self.assertNotIn("| Tool |", content)
