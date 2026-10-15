"""HIBERNATION_EXEMPT_TENANT_IDS keeps the App Review demo account awake.

App Review saw endless loading on 2.2.3 (2026-10-14) while the demo account's
container was asleep. Listed tenants must never be put to sleep, and the idle
sweep wakes a listed tenant that is already asleep.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.orchestrator.hibernation import hibernate_idle_tenant
from apps.orchestrator.tasks import hibernate_idle_tenants_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class HibernationExemptTests(TestCase):
    def setUp(self):
        self.enterContext(patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}))

    def _tenant(self, suffix: int, *, asleep: bool = False) -> Tenant:
        tenant = create_tenant(display_name=f"Exempt {suffix}", telegram_chat_id=910_000_000 + suffix)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = f"oc-exempt-{suffix}"
        tenant.container_fqdn = f"oc-exempt-{suffix}.internal"
        tenant.openclaw_version = "2026.5.28"
        tenant.last_message_at = timezone.now() - timedelta(hours=3)
        if asleep:
            tenant.hibernated_at = timezone.now() - timedelta(hours=1)
        tenant.save()
        return tenant

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    def test_sweep_skips_listed_tenant_and_sleeps_the_rest(self, mock_hibernate):
        listed = self._tenant(1)
        other = self._tenant(2)
        with override_settings(HIBERNATION_EXEMPT_TENANT_IDS=f" {str(listed.id).upper()} ,"):
            result = hibernate_idle_tenants_task()
        slept = [call.args[0].id for call in mock_hibernate.call_args_list]
        self.assertNotIn(listed.id, slept)
        self.assertIn(other.id, slept)
        self.assertEqual(result["hibernated"], 1)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    def test_sweep_wakes_a_listed_tenant_that_is_asleep(self, _mock_hibernate, mock_wake):
        listed = self._tenant(3, asleep=True)
        self._tenant(4, asleep=True)
        with override_settings(HIBERNATION_EXEMPT_TENANT_IDS=str(listed.id)):
            result = hibernate_idle_tenants_task()
        self.assertEqual([call.args[0].id for call in mock_wake.call_args_list], [listed.id])
        self.assertEqual(result["exempt_woken"], 1)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    def test_empty_list_changes_nothing(self, mock_hibernate, mock_wake):
        self._tenant(5)
        self._tenant(6, asleep=True)
        with override_settings(HIBERNATION_EXEMPT_TENANT_IDS=""):
            result = hibernate_idle_tenants_task()
        mock_wake.assert_not_called()
        self.assertEqual(mock_hibernate.call_count, 1)
        self.assertEqual(result["exempt_woken"], 0)

    @patch("apps.orchestrator.azure_client.hibernate_container_app")
    def test_direct_hibernate_refuses_listed_tenant(self, mock_azure):
        listed = self._tenant(7)
        with override_settings(HIBERNATION_EXEMPT_TENANT_IDS=str(listed.id)):
            self.assertFalse(hibernate_idle_tenant(listed))
        mock_azure.assert_not_called()
        listed.refresh_from_db()
        self.assertIsNone(listed.hibernated_at)
