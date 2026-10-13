"""Legacy automations have no alternate transport: keep failures explicit."""

from datetime import time
from unittest.mock import AsyncMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.automations.models import Automation, AutomationRun
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class TelegramAutomationTests(TestCase):
    def check_run(self, enabled):
        tenant = create_tenant(display_name="Automation", telegram_chat_id=876543)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_fqdn = "automation.invalid"
        tenant.save()
        automation = Automation.objects.create(
            tenant=tenant,
            kind=Automation.Kind.DAILY_BRIEF,
            status=Automation.Status.ACTIVE,
            timezone="UTC",
            schedule_type=Automation.ScheduleType.DAILY,
            schedule_time=time(9),
            next_run_at=timezone.now(),
        )
        client = APIClient()
        client.force_authenticate(tenant.user)
        with (
            override_settings(TELEGRAM_INBOUND_ENABLED=enabled),
            patch(
                "apps.automations.services.forward_to_openclaw",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as forward,
        ):
            response = client.post(f"/api/v1/automations/{automation.id}/run/")
        self.assertEqual(response.status_code, 201 if enabled else 502)
        run = AutomationRun.objects.get(automation=automation)
        self.assertEqual(run.status, AutomationRun.Status.SUCCEEDED if enabled else AutomationRun.Status.FAILED)
        self.assertEqual(forward.await_count, int(enabled))
        if not enabled:
            self.assertIn("Telegram", run.error_message)
            self.assertIn("unavailable", run.error_message)

    def test_enabled_uses_existing_telegram_dispatch(self):
        self.check_run(True)

    def test_disabled_returns_failed_run_without_dispatch(self):
        self.check_run(False)
