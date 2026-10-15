"""Offline interleavings of an image push, idle hibernation and delayed tasks."""

import threading
from concurrent.futures import wait
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import call, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.orchestrator import hibernation, tasks
from apps.tenants.models import Tenant, User

AZ = "apps.orchestrator.azure_client"
HIB = "apps.orchestrator.hibernation"
TAG = "2026.5.28-new"
PREVIOUS = "registry.test/nbhd-openclaw:2026.5.28-old"


@override_settings(AZURE_ACR_SERVER="registry.test", TENANT_IDLE_HIBERNATE_MINUTES=30)
class ImagePushHibernateRaceTests(TestCase):
    def setUp(self):
        self.publish = self.enterContext(patch("apps.cron.publish.publish_task"))
        self.gateway = self.enterContext(
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []})
        )
        self.image = self.enterContext(patch(f"{AZ}.update_container_image"))
        self.sleep = self.enterContext(patch(f"{AZ}.hibernate_container_app"))
        self.config = self.enterContext(patch("apps.orchestrator.services.update_tenant_config"))
        self.live = self.enterContext(patch(f"{HIB}._live_openclaw_image", return_value=PREVIOUS))
        self.enterContext(patch("apps.cron.suspension.tenant_uses_file_cron_sync", return_value=False))
        self.enterContext(patch("apps.cron.share_cron_sync.tenant_uses_file_cron_sync", return_value=False))
        self.enterContext(patch(f"{AZ}.is_mock", return_value=False))
        self.azure = self.enterContext(patch(f"{AZ}.get_container_client"))
        self.azure.return_value.container_apps.get.return_value = SimpleNamespace(
            latest_revision_name="new", latest_ready_revision_name="new"
        )
        self.now = timezone.now()
        self.tenant = Tenant.objects.create(
            user=User.objects.create_user(username="image-race"),
            status=Tenant.Status.ACTIVE,
            container_id="oc-image-race",
            container_fqdn="oc-image-race.invalid",
            openclaw_version="2026.5.28",
            container_image_tag="2026.5.28-old",
            last_message_at=self.now - timedelta(hours=3),
            last_wake_at=self.now - timedelta(hours=4),
            pending_config_version=2,
        )
        self.tid = str(self.tenant.pk)
        self.azure_awake = True

    def overlap(self, *_args):
        # Idle sweep finishes after the task's initial awake check; Azure's
        # image LRO then finishes with a new active revision.
        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=self.now, cron_wake_at=None)
        self.azure_awake = True

    def run_image(self):
        tasks.apply_single_tenant_image_task(self.tid, TAG)
        self.tenant.refresh_from_db()

    def restore_call(self, *, overlap=False):
        kwargs = {"resume_hibernated_crons": True} if overlap else {}
        return call(
            "restore_crons_after_image_update",
            self.tid,
            delay_seconds=90,
            idempotency_key=f"post-image-restore-{self.tid}-{TAG}",
            **kwargs,
        )

    def test_production_overlap_keeps_new_image_awake_and_verifiable(self):
        self.image.side_effect = self.overlap
        with self.assertLogs("apps.orchestrator.tasks", level="WARNING") as logs:
            self.run_image()
        self.assertIn(self.tid[:8], logs.output[0])
        self.assertIsNone(self.tenant.hibernated_at)
        self.assertTrue(self.azure_awake)
        self.assertEqual(self.tenant.last_wake_at, self.now - timedelta(hours=4))
        self.assertIsNone(self.tenant.cron_wake_at)
        self.assertEqual(self.tenant.container_image_tag, TAG)
        self.assertEqual(self.tenant.config_version, 2)
        self.assertCountEqual(
            self.publish.call_args_list,
            [
                self.restore_call(overlap=True),
                call("verify_wake_image_refresh", self.tid, TAG, PREVIOUS, 1, delay_seconds=240),
            ],
        )
        self.assertEqual(hibernation.verify_wake_image_refresh_task(self.tid, TAG, PREVIOUS), {"status": "ready"})
        self.image.assert_called_once_with(self.tenant.container_id, f"registry.test/nbhd-openclaw:{TAG}")
        self.sleep.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.image_refresh_blocked_tag)

    def test_no_overlap_preserves_calls_and_scheduling(self):
        self.run_image()
        self.image.assert_called_once_with(self.tenant.container_id, f"registry.test/nbhd-openclaw:{TAG}")
        self.config.assert_called_once_with(self.tid)
        self.gateway.assert_called_once_with(self.tenant, "cron.list", {"includeDisabled": True})
        self.assertEqual(
            self.publish.call_args_list,
            [
                call("verify_wake_image_refresh", self.tid, TAG, PREVIOUS, 1, delay_seconds=240),
                self.restore_call(),
            ],
        )
        self.assertIsNone(self.tenant.hibernated_at)
        self.sleep.assert_not_called()

    def test_already_hibernated_is_skipped(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=self.now)
        self.run_image()
        self.assertEqual(self.tenant.hibernated_at, self.now)
        for mock in (self.image, self.gateway, self.config, self.publish, self.live):
            mock.assert_not_called()

    def test_partial_azure_failure_reconciles_and_restores_crons_without_claiming_tag(self):
        def fail(*args):
            self.overlap(*args)
            raise RuntimeError("LRO failed after activation")

        self.image.side_effect = fail
        with self.assertLogs("apps.orchestrator.tasks", level="ERROR"):
            self.run_image()
        self.assertIsNone(self.tenant.hibernated_at)
        self.assertTrue(self.azure_awake)
        self.assertEqual(self.tenant.container_image_tag, "2026.5.28-old")
        self.assertFalse(self.tenant.image_refresh_blocked_tag)
        self.assertEqual(self.publish.call_args_list, [self.restore_call(overlap=True)])
        self.config.assert_not_called()
        self.sleep.assert_not_called()

    def test_failure_without_overlap_preserves_no_followups(self):
        self.image.side_effect = RuntimeError("image failed")
        with self.assertLogs("apps.orchestrator.tasks", level="ERROR"):
            self.run_image()
        self.publish.assert_not_called()
        self.config.assert_not_called()

    def test_duplicate_after_reconciliation_does_not_push_or_schedule_again(self):
        self.image.side_effect = self.overlap
        self.run_image()
        self.assertIsNone(self.tenant.hibernated_at)
        scheduled = list(self.publish.call_args_list)
        self.run_image()
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual(self.publish.call_args_list, scheduled)

    def test_backstop_rechecks_after_image_task_reconciles(self):
        old_sleep = self.now - timedelta(hours=1)
        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=old_sleep)

        caller = threading.current_thread()

        def finish_image_before_result(futures, **kwargs):
            done, pending = wait(futures, **kwargs)
            self.assertTrue(done)
            self.assertIs(threading.current_thread(), caller)
            # Simulate another writer after the sleeping snapshot and Azure
            # read, but before the result is consumed and the row re-checked.
            # Keep the image task's ORM work on the caller's TestCase connection.
            Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=None)
            self.image.side_effect = self.overlap
            self.run_image()
            return done, pending

        with (
            patch(f"{AZ}.container_app_has_active_revision", return_value=True) as active,
            patch("concurrent.futures.wait", side_effect=finish_image_before_result) as wait_for_check,
        ):
            counts = tasks.rehibernate_drifted_tenants_task()
        self.assertEqual(
            counts,
            dict(checked=1, awake_drifted=1, rehibernated=0, skipped_in_flight=1, failed=0, timed_out=False),
        )
        wait_for_check.assert_called_once()
        active.assert_called_once()
        self.image.assert_called_once_with(self.tenant.container_id, f"registry.test/nbhd-openclaw:{TAG}")
        self.sleep.assert_not_called()
        self.assertIsNone(self.tenant.hibernated_at)

    def test_stale_cron_booking_when_awake_only_rearms(self):
        self.image.side_effect = self.overlap
        self.run_image()
        with (
            patch(f"{HIB}._capture_tenant_cron_schedules", return_value=[]) as capture,
            patch(f"{HIB}._schedule_next_cron_wake") as schedule,
            patch(f"{HIB}.wake_hibernated_tenant") as wake,
        ):
            self.assertEqual(hibernation.wake_for_cron_task(self.tid), {"status": "already_awake"})
        wake.assert_not_called()
        capture.assert_called_once()
        schedule.assert_called_once()

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS="")
    def test_stale_booking_after_new_sleep_uses_normal_wake_and_later_booking_is_noop(self):
        self.image.side_effect = self.overlap
        self.run_image()
        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=self.now + timedelta(minutes=10))
        self.publish.reset_mock()
        with (
            patch(f"{AZ}.ensure_plugin_runtime_deps_mount", return_value=False),
            patch(f"{AZ}.wake_container_app") as wake,
            patch(f"{HIB}._capture_tenant_cron_schedules", return_value=[]),
            patch(f"{HIB}._schedule_next_cron_wake"),
        ):
            self.assertEqual(hibernation.wake_for_cron_task(self.tid), {"status": "woken_for_cron"})
            self.assertEqual(hibernation.wake_for_cron_task(self.tid), {"status": "already_awake"})
        wake.assert_called_once_with(self.tenant.container_id)
        self.tenant.refresh_from_db()
        self.assertIsNone(self.tenant.hibernated_at)
        self.assertGreaterEqual(self.tenant.cron_wake_at, self.now)
        self.assertGreaterEqual(self.tenant.last_wake_at, self.now)
        self.assertIn("check_cron_wake_idle", [c.args[0] for c in self.publish.call_args_list])

    def test_overlap_restore_resumes_existing_disabled_jobs(self):
        disabled = {"id": "custom-job", "name": "Custom", "enabled": False}
        self.gateway.return_value = {"jobs": [disabled]}
        Tenant.objects.filter(pk=self.tenant.pk).update(cron_jobs_snapshot={"jobs": [dict(disabled, enabled=True)]})
        with (
            patch("apps.cron.suspension.invoke_gateway_tool", return_value={"jobs": [disabled]}) as resume_gateway,
            patch("apps.orchestrator.config_generator.build_cron_seed_jobs", return_value=[]),
            patch("apps.orchestrator.tasks._fire_missed_crons_after_restore", return_value=0),
        ):
            tasks.restore_crons_after_image_update_task(self.tid, resume_hibernated_crons=True)
        self.assertIn(
            call(self.tenant, "cron.update", {"jobId": "custom-job", "patch": {"enabled": True}}),
            resume_gateway.call_args_list,
        )

    def test_overlap_restore_does_not_resume_after_new_sleep_or_suspension(self):
        for updates in ({"hibernated_at": self.now}, {"hibernated_at": None, "status": Tenant.Status.SUSPENDED}):
            with self.subTest(updates=updates):
                Tenant.objects.filter(pk=self.tenant.pk).update(**updates)
                with patch("apps.cron.suspension.resume_tenant_crons") as resume:
                    tasks.restore_crons_after_image_update_task(self.tid, resume_hibernated_crons=True)
                resume.assert_not_called()
        self.gateway.assert_not_called()

    def test_overlap_restore_enables_jobs_captured_after_suspension(self):
        disabled = {"id": "custom-job", "name": "Custom", "enabled": False}
        Tenant.objects.filter(pk=self.tenant.pk).update(cron_jobs_snapshot={"jobs": [disabled]})
        with (
            patch("apps.cron.suspension.invoke_gateway_tool", return_value={"jobs": []}),
            patch("apps.orchestrator.config_generator.build_cron_seed_jobs", return_value=[]),
            patch("apps.orchestrator.tasks._fire_missed_crons_after_restore", return_value=0),
        ):
            tasks.restore_crons_after_image_update_task(self.tid, resume_hibernated_crons=True)
        self.assertIn(
            call(self.tenant, "cron.add", {"job": {"name": "Custom", "enabled": True}}),
            self.gateway.call_args_list,
        )

    def test_file_sync_overlap_restore_republishes_without_gateway_calls(self):
        with (
            patch("apps.cron.share_cron_sync.tenant_uses_file_cron_sync", return_value=True),
            patch("apps.cron.suspension.tenant_uses_file_cron_sync", return_value=True),
            patch("apps.cron.share_cron_sync.write_tenant_crons_file", return_value=2) as write,
            patch("apps.cron.suspension.invoke_gateway_tool") as resume_gateway,
        ):
            tasks.restore_crons_after_image_update_task(self.tid, resume_hibernated_crons=True)
        write.assert_called_once_with(self.tenant)
        resume_gateway.assert_not_called()
        self.gateway.assert_not_called()

    def test_ordinary_restore_does_not_resume_disabled_jobs(self):
        with (
            patch("apps.cron.suspension.resume_tenant_crons") as resume,
            patch("apps.orchestrator.services.seed_cron_jobs") as seed,
        ):
            tasks.restore_crons_after_image_update_task(self.tid)
        resume.assert_not_called()
        seed.assert_called_once_with(self.tenant)

    def test_ready_verifier_does_not_weaken_unready_awake_revert(self):
        self.run_image()
        self.azure.return_value.container_apps.get.return_value.latest_ready_revision_name = "old"
        self.assertEqual(
            hibernation.verify_wake_image_refresh_task(self.tid, TAG, PREVIOUS, attempt=2), {"status": "reverted"}
        )
        self.image.assert_called_with(self.tenant.container_id, PREVIOUS)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.image_refresh_blocked_tag, TAG)
