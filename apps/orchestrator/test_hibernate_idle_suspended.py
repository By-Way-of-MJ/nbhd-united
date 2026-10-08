"""Pin: the idle sweep puts awake, idle SUSPENDED tenants' containers to sleep.

Prod 2026-09-29: suspension deactivated revisions once but left
``hibernated_at`` NULL, and ``hibernate_idle_tenants_task`` only looked at
ACTIVE tenants — so once a suspended tenant's box woke up it ran forever
(four boxes up 168/168h, some since July).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.orchestrator.tasks import hibernate_idle_tenants_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


@override_settings(TENANT_IDLE_HIBERNATE_MINUTES=30)
@patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
@patch("apps.orchestrator.azure_client.hibernate_container_app")
class HibernateIdleSuspendedTests(TestCase):
    def _make_suspended_tenant(self, *, suffix: int, idle_minutes: int = 180) -> Tenant:
        tenant = create_tenant(
            display_name=f"Idle Suspended {suffix}",
            telegram_chat_id=910_000_000 + suffix,
        )
        tenant.status = Tenant.Status.SUSPENDED
        tenant.container_id = f"oc-susp-{suffix}"
        tenant.container_fqdn = f"oc-susp-{suffix}.internal"
        tenant.last_message_at = timezone.now() - timedelta(minutes=idle_minutes)
        tenant.save()
        return tenant

    def test_idle_awake_suspended_tenant_is_hibernated(self, mock_azure, mock_idle_hibernate):
        tenant = self._make_suspended_tenant(suffix=1)
        tenant.cron_wake_at = timezone.now() - timedelta(hours=5)
        tenant.save(update_fields=["cron_wake_at"])

        with (
            patch("apps.cron.suspension.suspend_tenant_crons") as mock_suspend_crons,
            patch("apps.orchestrator.hibernation._schedule_next_cron_wake") as mock_cron_wake,
            patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_gateway,
        ):
            result = hibernate_idle_tenants_task()

        mock_azure.assert_called_once_with("oc-susp-1")
        tenant.refresh_from_db()
        self.assertIsNotNone(tenant.hibernated_at)
        self.assertIsNone(tenant.cron_wake_at)
        self.assertEqual(tenant.status, Tenant.Status.SUSPENDED)
        self.assertEqual(result["suspended_hibernated"], 1)
        self.assertEqual(result["suspended_failed"], 0)
        # Not routed through the active-tenant path: no cron capture/suspend/wake.
        mock_idle_hibernate.assert_not_called()
        mock_suspend_crons.assert_not_called()
        mock_cron_wake.assert_not_called()
        mock_gateway.assert_not_called()

    def test_recently_messaged_suspended_tenant_is_skipped(self, mock_azure, mock_idle_hibernate):
        tenant = self._make_suspended_tenant(suffix=2, idle_minutes=5)

        result = hibernate_idle_tenants_task()

        mock_azure.assert_not_called()
        tenant.refresh_from_db()
        self.assertIsNone(tenant.hibernated_at)
        self.assertEqual(result["suspended_hibernated"], 0)

    def test_already_hibernated_suspended_tenant_is_skipped(self, mock_azure, mock_idle_hibernate):
        tenant = self._make_suspended_tenant(suffix=3)
        tenant.hibernated_at = timezone.now() - timedelta(days=2)
        tenant.save(update_fields=["hibernated_at"])

        result = hibernate_idle_tenants_task()

        mock_azure.assert_not_called()
        self.assertEqual(result["suspended_hibernated"], 0)

    def test_never_messaged_suspended_tenant_uses_provisioned_at(self, mock_azure, mock_idle_hibernate):
        tenant = self._make_suspended_tenant(suffix=4)
        tenant.last_message_at = None
        tenant.provisioned_at = timezone.now() - timedelta(hours=2)
        tenant.save(update_fields=["last_message_at", "provisioned_at"])

        result = hibernate_idle_tenants_task()

        mock_azure.assert_called_once_with("oc-susp-4")
        self.assertEqual(result["suspended_hibernated"], 1)

    def test_azure_failure_leaves_tenant_for_next_sweep(self, mock_azure, mock_idle_hibernate):
        mock_azure.side_effect = RuntimeError("azure down")
        tenant = self._make_suspended_tenant(suffix=5)

        result = hibernate_idle_tenants_task()

        tenant.refresh_from_db()
        self.assertIsNone(tenant.hibernated_at)
        self.assertEqual(result["suspended_hibernated"], 0)
        self.assertEqual(result["suspended_failed"], 1)

    def test_reactivated_between_claim_and_hibernate_is_skipped(self, mock_azure, mock_idle_hibernate):
        tenant = self._make_suspended_tenant(suffix=6)
        original_refresh = Tenant.refresh_from_db

        def reactivate_then_refresh(self_tenant, *args, **kwargs):
            Tenant.objects.filter(pk=self_tenant.pk).update(status=Tenant.Status.ACTIVE)
            return original_refresh(self_tenant, *args, **kwargs)

        with patch.object(Tenant, "refresh_from_db", reactivate_then_refresh):
            result = hibernate_idle_tenants_task()

        mock_azure.assert_not_called()
        tenant.refresh_from_db()
        self.assertIsNone(tenant.hibernated_at)
        self.assertEqual(result["suspended_hibernated"], 0)

    def test_active_tenants_still_use_active_path(self, mock_azure, mock_idle_hibernate):
        tenant = create_tenant(display_name="Idle Active", telegram_chat_id=910_000_099)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = "oc-active-99"
        tenant.container_fqdn = "oc-active-99.internal"
        tenant.openclaw_version = "2026.5.28"
        tenant.last_message_at = timezone.now() - timedelta(hours=3)
        tenant.save()

        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}):
            result = hibernate_idle_tenants_task()

        mock_idle_hibernate.assert_called_once_with(tenant)
        mock_azure.assert_not_called()
        self.assertEqual(result["hibernated"], 1)
        self.assertEqual(result["suspended_hibernated"], 0)
