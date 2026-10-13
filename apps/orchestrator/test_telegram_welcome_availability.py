"""The legacy provisioning welcome asks for a reply, so requires inbound."""

import os
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.orchestrator.services import provision_tenant
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class TelegramWelcomeTests(TestCase):
    def check_welcome(self, enabled):
        tenant = create_tenant(display_name="Welcome", telegram_chat_id=876543)
        with (
            override_settings(TELEGRAM_INBOUND_ENABLED=enabled, TELEGRAM_BOT_TOKEN="test"),
            patch.dict(os.environ, {"AZURE_MOCK": "true"}),
            patch("apps.router.services.send_telegram_message", return_value=True) as send,
            patch("apps.tenants.emails.send_welcome_email", return_value=True) as email,
        ):
            provision_tenant(str(tenant.id))
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertEqual(send.call_count, int(enabled))
        self.assertEqual(tenant.onboarding_step, int(enabled))
        email.assert_called_once()

    def test_enabled_sends_existing_conversational_welcome(self):
        self.check_welcome(True)

    def test_disabled_preserves_onboarding_and_uses_existing_email(self):
        self.check_welcome(False)
