"""Static instructions must not promise an unavailable conversational channel."""

from pathlib import Path

from django.test import SimpleTestCase

from apps.orchestrator.services import converge_tools_md_text


class TelegramInstructionTests(SimpleTestCase):
    def test_static_docs_require_current_availability(self):
        root = Path(__file__).resolve().parents[2] / "templates/openclaw"
        for filename in ("TOOLS.md", "docs/platform-guide.md"):
            with self.subTest(filename=filename):
                text = (root / filename).read_text()
                self.assertIn("telegram_available", text)
                self.assertIn("false", text)
                self.assertIn("true", text)
                self.assertNotIn("Telegram and LINE are fully supported", text)

    def test_converge_old_supported_claim_preserves_custom_notes(self):
        current = "# Tools\n- Telegram and LINE are fully supported. Never assume a message came from Telegram.\n## Notes\nMy custom notes.\n"
        result = converge_tools_md_text(current)
        self.assertIsNotNone(result)
        self.assertIn("telegram_available", result)
        self.assertTrue(result.endswith("## Notes\nMy custom notes.\n"))
        self.assertIsNone(converge_tools_md_text(result))


class TelegramFormattingTests(SimpleTestCase):
    def test_channel_formatting_respects_availability(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from django.test import override_settings

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
