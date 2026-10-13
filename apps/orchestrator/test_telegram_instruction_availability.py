"""Availability is rendered server-side; tenant instructions need no API lookup."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase, TestCase, override_settings

from apps.orchestrator.services import _TOOLS_MD_CHANNEL_BODY, converge_tools_md_text

ROOT = Path(__file__).resolve().parents[2]
SUPPORTED = "- Telegram and LINE are fully supported. Never assume a message came from Telegram."


def template_text(filename):
    return (ROOT / "templates/openclaw" / filename).read_bytes().decode("utf-8")


class TelegramInstructionTests(SimpleTestCase):
    def test_enabled_tools_and_channel_body_match_template(self):
        original = template_text("TOOLS.md")
        communication = original.split("## Communication\n", 1)[1].split("\n## ", 1)[0].strip()
        self.assertEqual(_TOOLS_MD_CHANNEL_BODY, communication)
        with override_settings(TELEGRAM_INBOUND_ENABLED=True):
            self.assertIsNone(converge_tools_md_text(original))

    def test_tools_template_round_trips_exactly(self):
        original = template_text("TOOLS.md")
        with override_settings(TELEGRAM_INBOUND_ENABLED=False):
            retired = converge_tools_md_text(original)
            self.assertIn("Telegram chat is not available", retired)
            self.assertNotIn(SUPPORTED, retired)
            self.assertIsNone(converge_tools_md_text(retired))
        with override_settings(TELEGRAM_INBOUND_ENABLED=True):
            self.assertEqual(converge_tools_md_text(retired).encode("utf-8"), original.encode("utf-8"))

    def test_tools_reversible_and_idempotent_without_changing_notes_or_fences(self):
        original = (
            "# Tools\n## Communication\n"
            + SUPPORTED
            + "\n```md\n"
            + SUPPORTED
            + "\n## Notes\n```\n## Notes\n"
            + SUPPORTED
            + "\nMy custom notes.\n```md\n"
            + SUPPORTED
            + "\n```\n"
        )
        with override_settings(TELEGRAM_INBOUND_ENABLED=False):
            retired = converge_tools_md_text(original)
            self.assertIsNotNone(retired)
            self.assertIn("Telegram chat is not available", retired)
            self.assertEqual(retired.count(SUPPORTED), 3)
            self.assertIsNone(converge_tools_md_text(retired))
        with override_settings(TELEGRAM_INBOUND_ENABLED=True):
            self.assertEqual(converge_tools_md_text(retired), original)
            self.assertIsNone(converge_tools_md_text(original))

    def test_fenced_heading_does_not_start_managed_section(self):
        text = "# Tools\n```md\n## Communication\n" + SUPPORTED + "\n```\n## Notes\n" + SUPPORTED
        for enabled in (True, False):
            with override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                self.assertIsNone(converge_tools_md_text(text))

    def test_legacy_convergence_uses_selected_state(self):
        from apps.orchestrator.services import _TOOLS_MD_LEGACY_CHANNEL_LINE

        original = "## Communication\n" + _TOOLS_MD_LEGACY_CHANNEL_LINE + "\n## Notes\nUntouched\n"
        for enabled in (True, False):
            with override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                result = converge_tools_md_text(original)
                self.assertEqual(SUPPORTED in result, enabled)
                self.assertTrue(result.endswith("## Notes\nUntouched\n"))
                self.assertIsNone(converge_tools_md_text(result))

    def test_platform_guide_render_is_reversible_and_scoped(self):
        from apps.orchestrator.telegram_instructions import render_platform_guide

        template = template_text("docs/platform-guide.md")
        notes = "\n## Notes\n**What:** The user can connect both Telegram and LINE and choose a preferred channel\n"
        for suffix in ("", notes):
            original = template + suffix
            with self.subTest(custom_notes=bool(suffix)):
                with override_settings(TELEGRAM_INBOUND_ENABLED=False):
                    retired = render_platform_guide(original)
                    self.assertIn("Telegram chat is not available", retired)
                    self.assertTrue(retired.endswith(suffix))
                    self.assertEqual(render_platform_guide(retired), retired)
                with override_settings(TELEGRAM_INBOUND_ENABLED=True):
                    self.assertEqual(render_platform_guide(retired).encode("utf-8"), original.encode("utf-8"))
                    self.assertEqual(render_platform_guide(original), original)

    def test_rendered_workspace_guide_matches_flag(self):
        from apps.orchestrator.personas import render_workspace_files

        original = template_text("docs/platform-guide.md").strip()
        for enabled in (True, False):
            with override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                result = render_workspace_files("neighbor")["NBHD_DOC_PLATFORM_GUIDE"]
                if enabled:
                    self.assertEqual(result, original)
                else:
                    self.assertIn("Telegram chat is not available", result)
                    self.assertNotIn("user can connect both Telegram", result)


class TelegramFormattingTests(SimpleTestCase):
    @override_settings(TELEGRAM_INBOUND_ENABLED=True)
    def test_enabled_formatting_matches_templates_for_all_surfaces(self):
        from itertools import product

        from apps.orchestrator.personas import _resolve_channel_formatting

        for device, telegram, line in product((True, False), repeat=3):
            tenant = SimpleNamespace(
                user=SimpleNamespace(
                    telegram_chat_id=12345 if telegram else None, line_user_id="line" if line else None
                ),
                device_tokens=Mock(),
            )
            tenant.device_tokens.exists.return_value = device
            with self.subTest(device=device, telegram=telegram, line=line):
                channel = "app" if device else "telegram" if telegram else "line" if line else "app"
                expected = template_text(f"docs/{channel}-formatting.md").strip()
                self.assertEqual(_resolve_channel_formatting(tenant), expected)

    def test_channel_formatting_respects_availability(self):
        from apps.orchestrator.personas import _resolve_channel_formatting

        for enabled in (True, False):
            for line_id in (None, "line-test"):
                with self.subTest(enabled=enabled, line=line_id), override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                    tenant = SimpleNamespace(
                        user=SimpleNamespace(telegram_chat_id=12345, line_user_id=line_id),
                        device_tokens=Mock(),
                    )
                    tenant.device_tokens.exists.return_value = False
                    content = _resolve_channel_formatting(tenant)
                    if enabled:
                        self.assertIn("# Telegram Formatting", content)
                    elif line_id:
                        self.assertIn("# LINE", content)
                    else:
                        self.assertIn("# NBHD App", content)

    def test_default_preference_without_telegram_keeps_full_heartbeat_and_commitments(self):
        from apps.orchestrator.config_generator import (
            HEARTBEAT_MODEL,
            _build_commitments_config,
            _build_heartbeat_defaults,
        )

        tenant = SimpleNamespace(
            user=SimpleNamespace(preferred_channel="telegram", telegram_chat_id=None, timezone="UTC"),
            experimental_built_in_heartbeat=True,
            heartbeat_start_hour=8,
            heartbeat_window_hours=12,
        )
        expected_heartbeat = {
            "every": "1h",
            "target": "last",
            "model": HEARTBEAT_MODEL,
            "directPolicy": "allow",
            "lightContext": True,
            "isolatedSession": True,
            "skipWhenBusy": True,
            "activeHours": {"start": "08:00", "end": "20:00", "timezone": "UTC"},
        }
        results = []
        for enabled in (True, False):
            with self.subTest(enabled=enabled), override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                heartbeat = _build_heartbeat_defaults(tenant)
                commitments = _build_commitments_config(tenant)
                results.append((heartbeat, commitments))
                self.assertEqual(heartbeat, expected_heartbeat)
                self.assertEqual(commitments, {"enabled": True, "maxPerDay": 3})
        self.assertEqual(results[0], results[1])

    def test_line_preference_keeps_heartbeat_and_commitments_disabled(self):
        from apps.orchestrator.config_generator import _build_commitments_config, _build_heartbeat_defaults

        tenant = SimpleNamespace(
            user=SimpleNamespace(preferred_channel="line", telegram_chat_id=None, timezone="UTC"),
            experimental_built_in_heartbeat=True,
            heartbeat_start_hour=8,
            heartbeat_window_hours=12,
        )
        for enabled in (True, False):
            with self.subTest(enabled=enabled), override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                self.assertEqual(_build_heartbeat_defaults(tenant), {"every": "0m"})
                self.assertEqual(_build_commitments_config(tenant), {"enabled": False})


class CronRuntimeRenderingTests(TestCase):
    def test_undeliverable_payload_uses_existing_runtime_json_fallback(self):
        from django.utils import timezone

        from apps.actions.models import ActionStatus, ActionType, PendingAction
        from apps.cron.gate import cron_action_state
        from apps.tenants.models import Tenant, User

        tenant = Tenant.objects.create(user=User.objects.create_user(username="cron-rendering"))
        action = PendingAction.objects.create(
            tenant=tenant,
            action_type=ActionType.CRON_CREATE,
            action_payload={},
            display_summary="Reminder",
            expires_at=timezone.now(),
        )
        for delivery_state in ("no_channel", "failed"):
            with self.subTest(delivery_state=delivery_state):
                action.delivery_state = delivery_state
                payload = cron_action_state(action)
                self.assertEqual(payload["state"], "approval_undeliverable")
                self.assertNotEqual(payload["state"], "pending_approval")
                self.assertEqual(payload["delivery_state"], delivery_state)
                self.assertIn("not delivered", payload["detail"])
                self.assertEqual(action.status, ActionStatus.PENDING)

        runtime = (ROOT / "runtime/openclaw/plugins/nbhd-automation-tools/index.js").read_text()
        # The backend image has no JS runtime. Pin the source contract instead:
        # only pending_approval receives special delivery narration; other
        # states reach the final raw-JSON renderer.
        renderer = runtime[runtime.index("function renderCronCreatePayload(") : runtime.index("function hiddenOrigin(")]
        self.assertIn('if (payload.state === "pending_approval")', renderer)
        self.assertEqual(re.findall(r"payload\.state", renderer), ["payload.state"])
        self.assertTrue(renderer.rstrip().endswith("return renderPayload(payload);\n}"))
        raw_renderer = runtime[
            runtime.index("function renderPayload(") : runtime.index("function renderCronCreatePayload(")
        ]
        self.assertIn("text: JSON.stringify(payload, null, 2)", raw_renderer)
        self.assertIn("details: { json: payload }", raw_renderer)


class ManagedPassageEdgeTests(SimpleTestCase):
    @override_settings(TELEGRAM_INBOUND_ENABLED=False)
    def test_indented_notes_heading_ends_communication(self):
        current = "## Communication\nCustom text\n  ## Notes\n" + SUPPORTED + "\n"
        self.assertIsNone(converge_tools_md_text(current))

    def test_missing_optional_guide_keeps_workspace_render_fail_soft(self):
        from unittest.mock import patch

        from apps.orchestrator import personas

        load = personas._load_doc_template
        with patch.object(
            personas, "_load_doc_template", side_effect=lambda name: None if name == "platform-guide.md" else load(name)
        ):
            self.assertNotIn("NBHD_DOC_PLATFORM_GUIDE", personas.render_workspace_files("neighbor"))
