"""Closure survives missing hooks, retries, and simultaneous result/watchdog sends."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.orchestrator.tasks import close_expired_helpers
from apps.router.cron_delivery import _rate_counts
from apps.router.helper_runs import FAILURE_MESSAGE, deliver_helper
from apps.router.models import ChatThread, HelperRun, ProactiveOutbound
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key


def seed(test):
    user = get_user_model().objects.create_user(username="helper-owner")
    test.tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
    seed_internal_key(test.tenant, "helper-key")
    test.thread = ChatThread.objects.create(tenant=test.tenant, user=user)
    test.key = "helper:" + "a" * 48
    test.row = HelperRun.objects.create(
        tenant=test.tenant, thread=test.thread, occurrence_key=test.key, deadline=timezone.now() - timedelta(seconds=1)
    )


@override_settings(SUBAGENT_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class HelperClosureTests(TestCase):
    def setUp(self):
        seed(self)
        _rate_counts.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_X_NBHD_INTERNAL_KEY="helper-key", HTTP_X_NBHD_TENANT_ID=str(self.tenant.id))
        self.base = f"/api/v1/integrations/runtime/{self.tenant.id}/"

    def send(self, message="A useful result.", **kwargs):
        return self.client.post(
            self.base + "send-to-user/",
            {"message": message, "thread_id": str(self.thread.id), **kwargs},
            format="json",
            HTTP_X_NBHD_JOB_NAME="_subagent_result",
            HTTP_X_NBHD_OCCURRENCE_KEY=self.key,
        )

    def admit(self, **kwargs):
        return self.client.post(
            self.base + "helpers/admit/",
            {"occurrence_key": "helper:" + "b" * 48, "thread_id": str(self.thread.id), **kwargs},
            format="json",
        )

    def test_admission_is_durable_and_retries_do_not_extend_deadline(self):
        self.assertEqual(self.admit().status_code, 200)
        row = HelperRun.objects.exclude(pk=self.row.pk).get()
        self.assertAlmostEqual((row.deadline - row.created_at).total_seconds(), 480, delta=2)
        self.assertEqual(self.admit().status_code, 200)
        self.assertEqual(HelperRun.objects.get(pk=row.pk).deadline, row.deadline)
        self.assertEqual(HelperRun.objects.count(), 2)

    @override_settings(KEEP_CHATTING_TENANT_IDS="*")
    def test_cancelled_helper_closes_with_ack_and_never_delivers_raw_result(self):
        self.thread.side_jobs = {
            "job": {"main_ids": [], "side_ids": [], "kind": "cancel", "finished": True, "helpers": [self.key]}
        }
        self.thread.save(update_fields=["side_jobs"])
        self.assertEqual(self.send("RAW CANCELLED PLAN").data["status"], "sent")
        self.assertEqual(self.send("RAW CANCELLED PLAN").data["status"], "duplicate")
        self.assertEqual(self.send(FAILURE_MESSAGE).data["status"], "duplicate")
        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(row.message_text, "Cancelled — I won't send that result.")
        self.row.refresh_from_db()
        self.assertEqual(self.row.closed_by, HelperRun.ClosedBy.RESULT)

    @override_settings(KEEP_CHATTING_TENANT_IDS="*")
    def test_announce_context_uses_only_its_captured_child_session(self):
        from apps.pii.redactor import RedactionOutcome
        from apps.router.models import AppChatMessage

        other = "helper:" + "b" * 48
        first_session = "agent:main:subagent:00000000-0000-4000-8000-000000000001"
        second_session = "agent:main:subagent:00000000-0000-4000-8000-000000000002"
        HelperRun.objects.create(
            tenant=self.tenant, thread=self.thread, occurrence_key=other, deadline=self.row.deadline
        )
        for cid, text in (("old-side", "cancel the old desk job"), ("new-side", "what time is my dentist visit?")):
            AppChatMessage.objects.create(
                tenant=self.tenant,
                user=self.tenant.user,
                thread=self.thread,
                client_msg_id=cid,
                user_text=text,
                reply_text="Noted.",
                status="ready",
            )
        self.thread.side_jobs = {
            "old": {
                "main_ids": [],
                "side_ids": ["old-side"],
                "kind": "cancel",
                "finished": True,
                "helpers": [self.key],
                "helper_sessions": {self.key: first_session},
            },
            "new": {
                "main_ids": [],
                "side_ids": ["new-side"],
                "kind": "none",
                "finished": True,
                "helpers": [other],
                "helper_sessions": {other: second_session},
            },
        }
        self.thread.save(update_fields=["side_jobs"])
        with patch(
            "apps.pii.redactor.redact_user_message_checked",
            side_effect=lambda text, *a, **kw: RedactionOutcome(text, True, "ok"),
        ):
            response = self.client.post(
                self.base + "helpers/context/",
                {"thread_id": str(self.thread.pk), "source_session_key": second_session},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("dentist", response.data["context"])
        self.assertNotIn("cancel", response.data["context"].split("]\n", 1)[1])

    def test_active_job_selection_uses_admission_time_not_jsonb_key_order(self):
        from apps.router.job_changes import active_helper_job

        other = "helper:" + "b" * 48
        newer = HelperRun.objects.create(
            tenant=self.tenant, thread=self.thread, occurrence_key=other, deadline=self.row.deadline
        )
        self.thread.side_jobs = {
            "00000000-0000-4000-8000-000000000001": {"helpers": [other]},
            "ffffffff-ffff-4fff-8fff-ffffffffffff": {"helpers": [self.key]},
        }
        self.thread.save(update_fields=["side_jobs"])
        self.thread.refresh_from_db()
        self.assertEqual(active_helper_job(self.tenant, self.thread)[1]["helpers"], [newer.occurrence_key])

    def test_admission_refuses_closed_and_expired_keys(self):
        self.assertEqual(self.admit(occurrence_key=self.key).status_code, 409)
        self.row.closed_at = timezone.now()
        self.row.save()
        self.assertEqual(self.admit(occurrence_key=self.key).status_code, 409)

    def test_admission_auth_gate_and_thread_ownership(self):
        with override_settings(SUBAGENT_TENANT_IDS=""):
            self.assertEqual(self.admit().status_code, 403)
        self.assertEqual(self.admit(thread_id="00000000-0000-4000-8000-000000000001").status_code, 404)
        self.assertEqual(self.admit(occurrence_key="bad").status_code, 400)
        self.client.credentials(HTTP_X_NBHD_INTERNAL_KEY="wrong", HTTP_X_NBHD_TENANT_ID=str(self.tenant.id))
        self.assertEqual(self.admit().status_code, 401)

    def test_rejected_spawn_release_is_scoped_idempotent_and_never_undoes_delivery(self):
        payload = {"occurrence_key": self.key, "thread_id": str(self.thread.id)}
        url = self.base + "helpers/release/"
        wrong = {**payload, "thread_id": "00000000-0000-4000-8000-000000000001"}
        self.assertEqual(self.client.post(url, wrong, format="json").status_code, 400)
        self.assertTrue(HelperRun.objects.filter(pk=self.row.pk).exists())
        self.assertEqual(self.send().data["status"], "sent")
        self.assertEqual(self.client.post(url, payload, format="json").data["status"], "released")
        self.assertTrue(HelperRun.objects.filter(pk=self.row.pk, closed_by=HelperRun.ClosedBy.RESULT).exists())
        self.assertEqual(close_expired_helpers(), {"sent": 0})
        self.admit()
        payload["occurrence_key"] = "helper:" + "b" * 48
        for _ in range(2):
            self.assertEqual(self.client.post(url, payload, format="json").data["status"], "released")
        self.assertEqual(HelperRun.objects.count(), 1)
        self.client.credentials(HTTP_X_NBHD_INTERNAL_KEY="wrong", HTTP_X_NBHD_TENANT_ID=str(self.tenant.id))
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 401)

    def test_restart_without_any_runtime_completion_delivers_failure_once(self):
        # Only Postgres state exists: no hook, parent announce, process timer,
        # runtime container, or in-memory child mapping is required.
        self.assertEqual(close_expired_helpers(), {"sent": 1})
        self.assertEqual(close_expired_helpers(), {"sent": 0})
        outbound = ProactiveOutbound.objects.get()
        self.assertEqual(outbound.message_text, FAILURE_MESSAGE)
        self.assertEqual(outbound.thread_id, self.thread.id)
        self.row.refresh_from_db()
        self.assertEqual(self.row.closed_by, HelperRun.ClosedBy.FAILURE)

    def test_result_then_watchdog_and_repeated_parent_runs_send_once(self):
        self.assertEqual(self.send().data["status"], "sent")
        self.assertEqual(close_expired_helpers(), {"sent": 0})
        self.assertEqual(self.send("Repeated parent completion.").data["status"], "duplicate")
        self.assertEqual(ProactiveOutbound.objects.get().message_text, "A useful result.")

    def test_late_result_after_watchdog_failure_is_duplicate(self):
        self.assertEqual(close_expired_helpers(), {"sent": 1})
        self.assertEqual(self.send().data["status"], "duplicate")
        self.assertEqual(self.send("Sorry, I couldn't finish that.").data["status"], "duplicate")
        self.assertEqual(ProactiveOutbound.objects.get().message_text, FAILURE_MESSAGE)
        self.row.refresh_from_db()
        self.assertEqual(self.row.closed_by, HelperRun.ClosedBy.FAILURE)

    def test_failure_after_result_sends_nothing(self):
        self.assertEqual(self.send().data["status"], "sent")
        self.assertEqual(self.send(FAILURE_MESSAGE).data["status"], "duplicate")
        self.assertEqual(close_expired_helpers(), {"sent": 0})
        self.assertEqual(ProactiveOutbound.objects.get().message_text, "A useful result.")
        self.row.refresh_from_db()
        self.assertEqual(self.row.closed_by, HelperRun.ClosedBy.RESULT)

    def test_late_result_after_ended_hook_failure_is_duplicate(self):
        self.assertEqual(self.send(FAILURE_MESSAGE).data["status"], "sent")
        self.assertEqual(self.send().data["status"], "duplicate")
        self.assertEqual(self.send("Sorry, I couldn't finish.").data["status"], "duplicate")
        self.assertEqual(ProactiveOutbound.objects.get().message_text, FAILURE_MESSAGE)

    def test_late_post_after_failure_does_not_attempt_a_closure_write(self):
        close_expired_helpers()
        with patch.object(HelperRun, "save", side_effect=RuntimeError("must not write")) as save:
            self.assertEqual(self.send("Sorry, I could not finish.").data["status"], "duplicate")
            self.assertEqual(self.send("A late result.").data["status"], "duplicate")
        save.assert_not_called()
        self.assertEqual(ProactiveOutbound.objects.get().message_text, FAILURE_MESSAGE)

    def test_live_helpers_are_not_closed_early(self):
        self.row.deadline = timezone.now() + timedelta(minutes=8)
        self.row.save()
        self.assertEqual(close_expired_helpers(), {"sent": 0})
        self.assertFalse(ProactiveOutbound.objects.exists())

    def test_failed_feed_write_is_retried_by_watchdog(self):
        with patch("apps.router.proactive_context.record_proactive_outbound", return_value=None):
            self.assertEqual(self.send().status_code, 503)
            self.assertEqual(close_expired_helpers(), {"sent": 0})
        self.row.refresh_from_db()
        self.assertIsNone(self.row.closed_at)
        self.assertEqual(close_expired_helpers(), {"sent": 1})

    def test_crash_between_feed_write_and_closure_rolls_back_both(self):
        with patch.object(HelperRun, "save", side_effect=RuntimeError("worker crash")), self.assertRaises(RuntimeError):
            self.send()
        self.assertFalse(ProactiveOutbound.objects.exists())
        self.row.refresh_from_db()
        self.assertIsNone(self.row.closed_at)
        self.assertEqual(close_expired_helpers(), {"sent": 1})

    def test_push_is_only_dispatched_after_commit(self):
        with (
            patch("apps.router.push_views.eligible_device_tokens") as devices,
            patch("apps.router.proactive_context._dispatch_ios_push") as push,
            self.captureOnCommitCallbacks(execute=True),
        ):
            devices.return_value.exists.return_value = True
            self.assertEqual(self.send().data["status"], "sent")
            push.assert_not_called()
        push.assert_called_once()

    def test_rollback_never_dispatches_push(self):
        with (
            patch("apps.router.push_views.eligible_device_tokens") as devices,
            patch("apps.router.proactive_context._dispatch_ios_push") as push,
            self.captureOnCommitCallbacks(execute=True),
        ):
            devices.return_value.exists.return_value = True
            with transaction.atomic():
                self.send()
                transaction.set_rollback(True)
        push.assert_not_called()
        self.assertFalse(ProactiveOutbound.objects.exists())

    def test_missing_or_wrong_tenant_key_and_wrong_thread_never_send(self):
        self.key = "helper:" + "c" * 48
        self.assertEqual(self.send().status_code, 404)
        self.key = self.row.occurrence_key
        self.assertEqual(self.send(thread_id="00000000-0000-4000-8000-000000000001").status_code, 400)
        other_user = get_user_model().objects.create_user(username="other-helper-owner")
        other = Tenant.objects.create(user=other_user, status=Tenant.Status.ACTIVE)
        self.assertEqual(deliver_helper(tenant=other, occurrence_key=self.key, message_text="wrong").status_code, 404)
        self.assertFalse(ProactiveOutbound.objects.exists())

    def test_sweep_isolation_one_failure_does_not_starve_other_helpers(self):
        HelperRun.objects.create(
            tenant=self.tenant, thread=self.thread, occurrence_key="helper:" + "b" * 48, deadline=self.row.deadline
        )
        from apps.router.proactive_context import record_proactive_outbound

        calls = 0

        def fail_once(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary write failure")
            return record_proactive_outbound(**kwargs)

        with patch("apps.router.proactive_context.record_proactive_outbound", side_effect=fail_once):
            self.assertEqual(close_expired_helpers(), {"sent": 1})
        self.assertEqual(close_expired_helpers(), {"sent": 1})
        self.assertEqual(ProactiveOutbound.objects.count(), 2)

    def test_watchdog_runs_after_rollout_gate_is_disabled(self):
        with override_settings(SUBAGENT_TENANT_IDS=""):
            self.assertEqual(close_expired_helpers(), {"sent": 1})

    def test_watchdog_is_registered_every_minute(self):
        from apps.cron.management.commands.register_system_crons import SYSTEM_CRONS
        from apps.cron.views import TASK_MAP

        self.assertIn(("close-expired-helpers", "* * * * *", "/api/cron/trigger/close_expired_helpers/"), SYSTEM_CRONS)
        self.assertEqual(TASK_MAP["close_expired_helpers"], "apps.orchestrator.tasks.close_expired_helpers")


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class HelperRaceTests(TransactionTestCase):
    def setUp(self):
        seed(self)

    @override_settings(KEEP_CHATTING_TENANT_IDS="*")
    def test_result_waits_for_in_progress_cancel_without_holding_a_transaction(self):
        from apps.router import helper_runs

        self.thread.side_jobs = {
            "job": {
                "main_ids": [],
                "side_ids": [],
                "kind": "none",
                "finished": True,
                "helpers": [self.key],
                "checking_until": timezone.now().timestamp() + 10,
            }
        }
        self.thread.save(update_fields=["side_jobs"])
        waiting = Event()
        original = helper_runs._deliver_helper_once

        def observed(**kwargs):
            try:
                return original(**kwargs)
            except helper_runs._JobChangePending:
                waiting.set()
                raise

        def deliver():
            close_old_connections()
            try:
                tenant = Tenant.objects.select_related("user").get(pk=self.tenant.pk)
                return deliver_helper(tenant=tenant, occurrence_key=self.key, message_text="RAW CANCELLED PLAN")
            finally:
                close_old_connections()

        with (
            patch("apps.router.helper_runs._deliver_helper_once", side_effect=observed),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(deliver)
            self.assertTrue(waiting.wait(5))
            with transaction.atomic():
                thread = ChatThread.objects.select_for_update().get(pk=self.thread.pk)
                thread.side_jobs["job"].pop("checking_until")
                thread.side_jobs["job"]["kind"] = "cancel"
                thread.save(update_fields=["side_jobs"])
            self.assertEqual(future.result(timeout=5).data["status"], "sent")
        self.assertEqual(ProactiveOutbound.objects.get().message_text, "Cancelled — I won't send that result.")

    def send_concurrently(self, messages):
        barrier = Barrier(2)

        def send(message):
            close_old_connections()
            try:
                tenant = Tenant.objects.select_related("user").get(pk=self.tenant.pk)
                barrier.wait(timeout=10)
                return deliver_helper(tenant=tenant, occurrence_key=self.key, message_text=message).data["status"]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(send, messages))

    def test_simultaneous_result_and_failure_publish_first_closure_only(self):
        statuses = self.send_concurrently(["Result", FAILURE_MESSAGE])
        self.assertCountEqual(statuses, ["sent", "duplicate"])
        self.assertEqual(ProactiveOutbound.objects.count(), 1)
        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.closed_at)
        self.assertEqual(
            self.row.closed_by, HelperRun.ClosedBy.RESULT if statuses[0] == "sent" else HelperRun.ClosedBy.FAILURE
        )

    def test_simultaneous_late_results_after_failure_are_duplicates(self):
        self.assertEqual(close_expired_helpers(), {"sent": 1})
        self.assertEqual(self.send_concurrently(["Result", "Result"]), ["duplicate", "duplicate"])
        self.assertEqual(ProactiveOutbound.objects.get().message_text, FAILURE_MESSAGE)
        self.row.refresh_from_db()
        self.assertEqual(self.row.closed_by, HelperRun.ClosedBy.FAILURE)
