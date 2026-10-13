"""Reconcile Azure revisions without changing the existing sleep/wake contract."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.orchestrator import tasks
from apps.orchestrator.services import bump_openclaw_version_for_tenant
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

AZ = "apps.orchestrator.azure_client"
SERVICES = "apps.orchestrator.services"


@override_settings(TENANT_IDLE_HIBERNATE_MINUTES=30)
class RehibernateDriftedTests(TestCase):
    def setUp(self):
        self.active = self.enterContext(patch(f"{AZ}.container_app_has_active_revision", return_value=True))
        self.hibernate = self.enterContext(patch(f"{AZ}.hibernate_container_app"))
        self.in_flight = self.enterContext(
            patch("apps.orchestrator.openclaw_auto_upgrade.in_flight", return_value=False)
        )
        self.gateway = self.enterContext(patch("apps.cron.gateway_client.invoke_gateway_tool"))
        self.publish = self.enterContext(patch("apps.cron.publish.publish_task"))
        self.suspend = self.enterContext(patch("apps.cron.suspension.suspend_tenant_crons"))

    def _tenant(self, suffix=1, *, minutes=180, status=Tenant.Status.ACTIVE):
        tenant = create_tenant(display_name=f"Drift {suffix}", telegram_chat_id=920_000_000 + suffix)
        tenant.status = status
        tenant.container_id = f"oc-drift-{suffix}"
        tenant.hibernated_at = timezone.now() - timedelta(minutes=minutes)
        tenant.save()
        return tenant

    def _run(self, **counts):
        expected = dict(checked=0, awake_drifted=0, rehibernated=0, skipped_in_flight=0, failed=0)
        expected.update(counts)
        self.assertEqual(tasks.rehibernate_drifted_tenants_task(), expected)

    def test_active_revision_is_reslept_without_changing_sleep_state(self):
        tenant = self._tenant()
        before = Tenant.objects.filter(pk=tenant.pk).values().get()
        with self.assertLogs("apps.orchestrator.tasks", level="WARNING") as logs:
            self._run(checked=1, awake_drifted=1, rehibernated=1)
        self.hibernate.assert_called_once_with(tenant.container_id)
        self.assertEqual(Tenant.objects.filter(pk=tenant.pk).values().get(), before)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].args, (str(tenant.id)[:8], tenant.container_id))
        self.gateway.assert_not_called()
        self.publish.assert_not_called()
        self.suspend.assert_not_called()

    def test_inactive_revision_is_untouched(self):
        self._tenant()
        self.active.return_value = False
        self._run(checked=1)
        self.hibernate.assert_not_called()

    def test_awake_row_is_never_checked(self):
        tenant = self._tenant()
        Tenant.objects.filter(pk=tenant.pk).update(hibernated_at=None)
        self._run()
        self.active.assert_not_called()

    def test_recent_sleep_is_never_checked(self):
        self._tenant(minutes=29)
        self._run()
        self.active.assert_not_called()

    def test_empty_container_is_never_checked(self):
        tenant = self._tenant()
        Tenant.objects.filter(pk=tenant.pk).update(container_id="")
        self._run()
        self.active.assert_not_called()

    def test_wake_during_azure_check_is_not_deactivated(self):
        tenant = self._tenant()

        def wake(_container):
            Tenant.objects.filter(pk=tenant.pk).update(hibernated_at=None)
            return True

        self.active.side_effect = wake
        self._run(checked=1, awake_drifted=1, skipped_in_flight=1)
        self.hibernate.assert_not_called()

    def test_new_sleep_during_azure_check_is_not_deactivated(self):
        tenant = self._tenant()

        def resleep(_container):
            Tenant.objects.filter(pk=tenant.pk).update(hibernated_at=timezone.now())
            return True

        self.active.side_effect = resleep
        self._run(checked=1, awake_drifted=1, skipped_in_flight=1)
        self.hibernate.assert_not_called()

    def test_container_replacement_during_check_is_not_deactivated(self):
        tenant = self._tenant()

        def replace(_container):
            Tenant.objects.filter(pk=tenant.pk).update(container_id="oc-replacement")
            return True

        self.active.side_effect = replace
        self._run(checked=1, awake_drifted=1, skipped_in_flight=1)
        self.hibernate.assert_not_called()

    def test_auto_upgrade_in_flight_is_skipped(self):
        tenant = self._tenant()
        self.in_flight.return_value = True
        self._run(checked=1, awake_drifted=1, skipped_in_flight=1)
        self.in_flight.assert_called_once_with(tenant.id)
        self.hibernate.assert_not_called()

    def test_revision_check_error_does_not_stop_next_tenant(self):
        self._tenant(1)
        self._tenant(2)
        self.active.side_effect = [RuntimeError("azure down"), True]
        with self.assertLogs("apps.orchestrator.tasks", level="ERROR"):
            self._run(checked=2, awake_drifted=1, rehibernated=1, failed=1)
        self.assertEqual(self.active.call_count, 2)
        self.hibernate.assert_called_once_with(self.active.call_args.args[0])

    def test_deactivation_error_does_not_stop_next_tenant(self):
        self._tenant(1)
        self._tenant(2)
        self.hibernate.side_effect = [RuntimeError("azure down"), None]
        with self.assertLogs("apps.orchestrator.tasks", level="ERROR"):
            self._run(checked=2, awake_drifted=2, rehibernated=1, failed=1)
        self.assertEqual(self.hibernate.call_count, 2)

    def test_suspended_tenant_is_reslept(self):
        tenant = self._tenant(status=Tenant.Status.SUSPENDED)
        self._run(checked=1, awake_drifted=1, rehibernated=1)
        self.hibernate.assert_called_once_with(tenant.container_id)
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.SUSPENDED)

    def test_image_bump_claims_sleep_before_azure_creates_revision(self):
        tenant = self._tenant()

        def bump_image(*_args):
            self.assertIsNone(Tenant.objects.get(pk=tenant.pk).hibernated_at)
            self._run()

        with (
            patch(f"{SERVICES}.download_config_from_file_share", return_value=b"{}"),
            patch(f"{SERVICES}.upload_config_to_file_share"),
            patch(f"{SERVICES}.update_tenant_config"),
            patch(f"{SERVICES}.update_container_image", side_effect=bump_image),
        ):
            bump_openclaw_version_for_tenant(tenant, "2026.5.28", "2026.5.28-test", "registry.test")
        self.hibernate.assert_not_called()
        tenant.refresh_from_db()
        self.assertIsNone(tenant.hibernated_at)

    def test_failed_image_bump_restores_sleep_for_reconcile(self):
        tenant = self._tenant()
        original_sleep = tenant.hibernated_at
        original_version = tenant.openclaw_version

        def fail_image(*_args):
            self.assertIsNone(Tenant.objects.get(pk=tenant.pk).hibernated_at)
            raise RuntimeError("image failed")

        with (
            patch(f"{SERVICES}.download_config_from_file_share", return_value=b"{}"),
            patch(f"{SERVICES}.upload_config_to_file_share"),
            patch(f"{SERVICES}.update_tenant_config"),
            patch(f"{SERVICES}.update_container_image", side_effect=fail_image),
            self.assertRaisesRegex(RuntimeError, "image failed"),
        ):
            bump_openclaw_version_for_tenant(tenant, "2026.5.28", "2026.5.28-test", "registry.test")
        tenant.refresh_from_db()
        self.assertEqual(tenant.hibernated_at, original_sleep)
        self.assertEqual(tenant.openclaw_version, original_version)
