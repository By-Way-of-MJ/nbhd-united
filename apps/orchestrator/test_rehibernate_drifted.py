"""Reconcile Azure revisions without changing the existing sleep/wake contract."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.orchestrator import tasks
from apps.orchestrator.azure_client import container_app_has_active_revision as real_active_revision
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
        expected = dict(checked=0, awake_drifted=0, rehibernated=0, skipped_in_flight=0, failed=0, timed_out=False)
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

    def test_mock_mode_has_no_drift(self):
        self._tenant()
        self.active.side_effect = real_active_revision
        with patch(f"{AZ}._is_mock", return_value=True):
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

        refresh = Tenant.refresh_from_db

        def wake(instance, **kwargs):
            self.assertIs(threading.current_thread(), threading.main_thread())
            Tenant.objects.filter(pk=tenant.pk).update(hibernated_at=None)
            refresh(instance, **kwargs)

        with patch.object(Tenant, "refresh_from_db", wake):
            self._run(checked=1, awake_drifted=1, skipped_in_flight=1)
        self.hibernate.assert_not_called()

    def test_new_sleep_during_azure_check_is_not_deactivated(self):
        tenant = self._tenant()

        refresh = Tenant.refresh_from_db

        def resleep(instance, **kwargs):
            self.assertIs(threading.current_thread(), threading.main_thread())
            Tenant.objects.filter(pk=tenant.pk).update(hibernated_at=timezone.now())
            refresh(instance, **kwargs)

        with patch.object(Tenant, "refresh_from_db", resleep):
            self._run(checked=1, awake_drifted=1, skipped_in_flight=1)
        self.hibernate.assert_not_called()

    def test_container_replacement_during_check_is_not_deactivated(self):
        tenant = self._tenant()

        refresh = Tenant.refresh_from_db

        def replace(instance, **kwargs):
            self.assertIs(threading.current_thread(), threading.main_thread())
            Tenant.objects.filter(pk=tenant.pk).update(container_id="oc-replacement")
            refresh(instance, **kwargs)

        with patch.object(Tenant, "refresh_from_db", replace):
            self._run(checked=1, awake_drifted=1, skipped_in_flight=1)
        self.hibernate.assert_not_called()

    def test_auto_upgrade_in_flight_is_skipped(self):
        tenant = self._tenant()
        self.in_flight.return_value = True
        self._run(checked=1, awake_drifted=1, skipped_in_flight=1)
        self.in_flight.assert_called_once_with(tenant.id)
        self.hibernate.assert_not_called()

    def test_revision_check_error_does_not_stop_next_tenant(self):
        bad = self._tenant(1)
        good = self._tenant(2)

        def check(container, **kwargs):
            if container == bad.container_id:
                raise RuntimeError("azure down")
            return True

        self.active.side_effect = check
        with self.assertLogs("apps.orchestrator.tasks", level="ERROR"):
            self._run(checked=2, awake_drifted=1, rehibernated=1, failed=1)
        self.assertEqual(self.active.call_count, 2)
        self.hibernate.assert_called_once_with(good.container_id)

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

    def test_many_checks_finish_out_of_order_exactly_once(self):
        tenants = [self._tenant(i) for i in range(1, 18)]
        first, second = list(
            Tenant.objects.filter(pk__in=[t.pk for t in tenants]).values_list("container_id", flat=True)
        )[:2]
        second_finished = threading.Event()
        finished = []
        lock = threading.Lock()
        active_names = {t.container_id for t in tenants[::2]}

        def check(container, **kwargs):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            if container == first:
                self.assertTrue(second_finished.wait(0.5), "checks ran serially")
            with lock:
                finished.append(container)
            if container == second:
                second_finished.set()
            return container in active_names

        self.active.side_effect = check
        self._run(checked=17, awake_drifted=9, rehibernated=9)
        self.assertCountEqual(finished, [t.container_id for t in tenants])
        self.assertLess(finished.index(second), finished.index(first))
        self.assertCountEqual([c.args[0] for c in self.hibernate.call_args_list], active_names)

    def test_checks_overlap_and_lifecycle_stays_on_caller(self):
        self._tenant(1)
        self._tenant(2)
        caller = threading.current_thread()
        barrier = threading.Barrier(2)
        refresh = Tenant.refresh_from_db

        def check(_container, **kwargs):
            self.assertIsNot(threading.current_thread(), caller)
            barrier.wait(timeout=0.5)
            return True

        def guarded_refresh(instance, **kwargs):
            self.assertIs(threading.current_thread(), caller)
            refresh(instance, **kwargs)

        def guarded_in_flight(_tenant_id):
            self.assertIs(threading.current_thread(), caller)
            return False

        def guarded_hibernate(_container):
            self.assertIs(threading.current_thread(), caller)

        self.active.side_effect = check
        self.in_flight.side_effect = guarded_in_flight
        self.hibernate.side_effect = guarded_hibernate
        with patch.object(Tenant, "refresh_from_db", guarded_refresh):
            self._run(checked=2, awake_drifted=2, rehibernated=2)

    def test_worker_exception_is_logged_with_traceback_and_isolated(self):
        bad = self._tenant(1)
        good = self._tenant(2)

        def check(container, **kwargs):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            if container == bad.container_id:
                raise RuntimeError("read failed")
            return True

        self.active.side_effect = check
        with self.assertLogs("apps.orchestrator.tasks", level="ERROR") as logs:
            self._run(checked=2, awake_drifted=1, rehibernated=1, failed=1)
        self.assertIsNotNone(logs.records[0].exc_info)
        self.hibernate.assert_called_once_with(good.container_id)

    @override_settings(REHIBERNATE_DRIFT_BUDGET_SECONDS=120)
    def test_budget_stops_refill_after_handling_found_drift(self):
        for i in range(1, 18):
            self._tenant(i)
        now = [0.0]

        def hibernate(_container):
            now[0] = 121.0

        self.hibernate.side_effect = hibernate
        with patch.object(tasks.time, "monotonic", side_effect=lambda: now[0]):
            result = tasks.rehibernate_drifted_tenants_task()
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["rehibernated"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertLessEqual(self.active.call_count, 8)
        self.assertEqual(self.hibernate.call_count, 1)

    @override_settings(REHIBERNATE_DRIFT_BUDGET_SECONDS=120)
    def test_budget_cancels_queued_reads_without_waiting_for_running_read(self):
        for i in range(1, 18):
            self._tenant(i)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        submitted = []
        now = [0.0]

        class OneWorkerPool(ThreadPoolExecutor):
            def __init__(self, **kwargs):
                super().__init__(max_workers=1)

            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                submitted.append(future)
                return future

        def check(_container, **kwargs):
            entered.set()
            try:
                self.assertTrue(release.wait(0.5), "request waited for blocked worker")
                return True
            finally:
                finished.set()

        def exhaust_budget(futures, **kwargs):
            self.assertEqual(kwargs["timeout"], 120)
            self.assertTrue(entered.wait(0.5))
            now[0] = 120.0
            return set(), set(futures)

        self.active.side_effect = check
        try:
            with (
                patch("concurrent.futures.ThreadPoolExecutor", OneWorkerPool),
                patch("concurrent.futures.wait", side_effect=exhaust_budget),
                patch.object(tasks.time, "monotonic", side_effect=lambda: now[0]),
            ):
                self._run(timed_out=True)
            self.assertFalse(finished.is_set(), "executor shutdown waited for read")
            self.assertEqual(len(submitted), 8)
            self.assertEqual(sum(f.cancelled() for f in submitted), 7)
            self.assertEqual(self.active.call_count, 1)
        finally:
            release.set()
            self.assertTrue(finished.wait(0.5))
        self.hibernate.assert_not_called()

    def test_final_summary_has_every_count_and_elapsed_seconds(self):
        self._tenant()
        with self.assertLogs("apps.orchestrator.tasks", level="INFO") as logs:
            self._run(checked=1, awake_drifted=1, rehibernated=1)
        summaries = [r.getMessage() for r in logs.records if "checked=" in r.getMessage()]
        self.assertEqual(len(summaries), 1)
        self.assertRegex(
            summaries[0],
            r"rehibernate_drifted_tenants: checked=1 awake_drifted=1 rehibernated=1 "
            r"skipped_in_flight=0 failed=0 timed_out=False elapsed_seconds=\d+\.\d+",
        )


class ActiveRevisionTimeoutTests(SimpleTestCase):
    @override_settings(AZURE_PROVISIONER_CLIENT_ID="test-identity", AZURE_SUBSCRIPTION_ID="test-sub")
    def test_bounded_read_owns_client_and_credential_and_passes_timeouts(self):
        from apps.orchestrator.azure_client import container_app_has_active_revision

        with (
            patch(f"{AZ}._is_mock", return_value=False),
            patch(f"{AZ}.get_container_client") as shared,
            patch("azure.identity.ManagedIdentityCredential") as credential,
            patch("azure.mgmt.appcontainers.ContainerAppsAPIClient") as client_type,
        ):
            client = client_type.return_value.__enter__.return_value
            client.container_apps_revisions.list_revisions.return_value = [SimpleNamespace(active=True)]
            self.assertTrue(container_app_has_active_revision("oc-test", timeout_seconds=7))
        shared.assert_not_called()
        credential.assert_called_once_with(
            client_id="test-identity", connection_timeout=7, read_timeout=7, retry_total=0
        )
        kwargs = client.container_apps_revisions.list_revisions.call_args.kwargs
        self.assertEqual(kwargs, dict(connection_timeout=7, read_timeout=7, timeout=7, retry_total=0))
        credential.return_value.__exit__.assert_called_once()
        client_type.return_value.__exit__.assert_called_once()

    def test_default_read_keeps_cached_client_and_sdk_defaults(self):
        from apps.orchestrator.azure_client import container_app_has_active_revision

        with patch(f"{AZ}._is_mock", return_value=False), patch(f"{AZ}.get_container_client") as shared:
            shared.return_value.container_apps_revisions.list_revisions.return_value = [SimpleNamespace(active=False)]
            self.assertFalse(container_app_has_active_revision("oc-test"))
        self.assertEqual(shared.return_value.container_apps_revisions.list_revisions.call_args.kwargs, {})

    def test_mock_read_never_constructs_client(self):
        from apps.orchestrator.azure_client import container_app_has_active_revision

        with (
            patch(f"{AZ}._is_mock", return_value=True),
            patch("azure.mgmt.appcontainers.ContainerAppsAPIClient") as client_type,
        ):
            self.assertFalse(container_app_has_active_revision("oc-test", timeout_seconds=7))
        client_type.assert_not_called()
