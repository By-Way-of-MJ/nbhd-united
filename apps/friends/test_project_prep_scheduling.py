"""Person-requested dispatch, durable bounds, and post-commit wake contracts."""

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.actions.models import PendingAction
from apps.cron.cache import upsert_jobs_to_cache
from apps.cron.models import CronJob
from apps.cron.postgres_canonical import upsert_from_gateway_jobs
from apps.cron.share_cron_sync import build_signed_crons_doc
from apps.cron.signals import suppress_cronjob_reconcile
from apps.cron.system_dispatch import STEP_PREP_NOW_PREFIX
from apps.cron.tasks import expire_finished_at_crons_task
from apps.orchestrator.config_generator import step_prep_job
from apps.orchestrator.cron_reconcile import regenerate_tenant_crons
from apps.orchestrator.services import refresh_system_cron_rows_from_seed
from apps.orchestrator.tool_policy import STEP_PREP_TOOLS
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key

from . import access, project_prep
from . import project_prep_scheduling as scheduling
from .models import StepPrep
from .test_project_prep import StepPrepTests


@override_settings(
    PROJECTS_V2_TENANT_IDS="*",
    STEP_PREP_TENANT_IDS="*",
    NBHD_DISABLE_BACKGROUND_THREADS=True,
    CRON_GATE_TENANT_IDS="*",
)
class PrepSchedulingTests(TestCase):
    jwt = StepPrepTests.jwt
    start = StepPrepTests.start
    row = StepPrepTests.row

    def setUp(self):
        StepPrepTests.setUp(self)
        seed_internal_key(self.holder, "prep-scheduler-test-key")
        self.budget = self.enterContext(patch.object(scheduling, "check_budget", return_value=""))
        self.wake = self.enterContext(
            patch.object(scheduling, "wake_for_cron_task", return_value={"status": "woken_for_cron"})
        )
        self.upload = self.enterContext(patch("apps.orchestrator.azure_client._put_share_file"))
        self.holder.openclaw_version = "2026.9.4"
        self.holder.postgres_cron_canonical = True
        self.holder.save(update_fields=["openclaw_version", "postgres_cron_canonical"])

    def crons(self):
        return CronJob.objects.filter(tenant=self.holder, name__startswith=STEP_PREP_NOW_PREFIX)

    def committed_start(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.start()
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def sleep(self):
        Tenant.objects.filter(pk=self.holder.pk).update(hibernated_at=timezone.now())

    def test_start_awake_has_seed_contract_and_signed_tools_without_member_text(self):
        before = timezone.now()
        self.committed_start()
        cron = self.crons().get()
        self.assertFalse(cron.managed)
        self.assertEqual((cron.source, cron.creation_path, cron.enabled), ("system", "internal", True))
        self.assertEqual(cron.data["schedule"]["kind"], "at")
        self.assertTrue(cron.data["deleteAfterRun"])
        delay = (parse_datetime(cron.data["schedule"]["at"]) - before).total_seconds()
        self.assertGreaterEqual(delay, 90)
        self.assertLess(delay, 100)
        for key in ("payload", "model", "sessionTarget", "delivery"):
            self.assertEqual(cron.data[key], step_prep_job()[key])
        self.assertEqual(cron.data["payload"]["toolsAllow"], list(STEP_PREP_TOOLS["Step Prep"]))
        self.assertNotIn("nbhd_send_to_user", cron.data["payload"]["toolsAllow"])
        self.assertEqual(cron.data["delivery"], {"mode": "none"})
        self.assertNotIn("Near the fence", json.dumps(cron.data))
        self.wake.assert_not_called()
        self.assertFalse(PendingAction.objects.filter(tenant=self.holder).exists())
        doc = json.loads(self.upload.call_args.kwargs["data"])
        signed = next(job for job in json.loads(doc["signed"]) if job["name"] == cron.name)
        self.assertEqual(signed["payload"]["toolsAllow"], list(STEP_PREP_TOOLS["Step Prep"]))
        self.assertEqual(signed["payload"]["model"], step_prep_job()["model"])
        self.assertTrue(doc["sig"])

    def test_start_hibernated_wakes_once_and_reserves_pending_slot(self):
        self.sleep()
        before = timezone.now()
        self.committed_start()
        cron = self.crons().get()
        delay = (parse_datetime(cron.data["schedule"]["at"]) - before).total_seconds()
        self.assertGreaterEqual(delay, 270)
        self.assertLess(delay, 280)
        self.wake.assert_called_once_with(str(self.holder.id))
        # Even Stop/Start (which really re-enters working) sees the reservation.
        project_prep.stop(self.holder, self.goal.id, self.step.id)
        self.committed_start()
        self.assertEqual(self.crons().count(), 1)
        self.wake.assert_called_once()

    def test_repeated_start_on_working_row_does_not_even_dispatch(self):
        self.committed_start()
        with patch("apps.friends.project_prep.run_soon") as schedule:
            self.committed_start()
        schedule.assert_not_called()
        self.assertEqual(self.crons().count(), 1)

    def test_over_budget_never_wakes_or_creates(self):
        self.sleep()
        self.budget.return_value = "personal"
        self.committed_start()
        self.wake.assert_not_called()
        self.assertFalse(self.crons().exists())
        self.assertEqual(self.row().status, "working")

    def test_daily_cap_counts_disabled_rows_and_cache_cannot_erase_them(self):
        self.sleep()
        for _ in range(6):
            CronJob.objects.create(
                tenant=self.holder,
                name=f"{STEP_PREP_NOW_PREFIX}{uuid4()}",
                enabled=False,
                managed=False,
                source="system",
                creation_path="internal",
                data={"schedule": {"kind": "at", "at": timezone.now().isoformat()}},
            )
        upsert_jobs_to_cache(self.holder, [])
        upsert_from_gateway_jobs(self.holder, [])
        self.committed_start()
        self.assertEqual(self.crons().count(), 6)
        self.wake.assert_not_called()
        self.upload.assert_not_called()

    def test_cap_is_rolling_24_hours(self):
        for _ in range(6):
            cron = CronJob.objects.create(
                tenant=self.holder,
                name=f"{STEP_PREP_NOW_PREFIX}{uuid4()}",
                enabled=False,
                managed=False,
                source="system",
                creation_path="internal",
            )
            CronJob.objects.filter(pk=cron.pk).update(created_at=timezone.now() - timedelta(hours=25))
        self.committed_start()
        self.assertEqual(self.crons().filter(enabled=True).count(), 1)

    def test_wake_exception_or_failure_status_disables_claim_and_preserves_start(self):
        self.sleep()
        for outcome in [RuntimeError("wake failed"), {"status": "wake_failed"}]:
            self.wake.side_effect = outcome if isinstance(outcome, Exception) else None
            self.wake.return_value = outcome
            self.committed_start()
            self.assertEqual(self.row().status, "working")
            self.assertFalse(self.crons().filter(enabled=True).exists())
            project_prep.stop(self.holder, self.goal.id, self.step.id)
        self.assertEqual(self.crons().count(), 2)  # failures still spend a cap slot
        self.upload.assert_not_called()

    def test_file_failure_preserves_working_row_and_durable_claim(self):
        self.upload.side_effect = RuntimeError("share unavailable")
        self.committed_start()
        self.assertEqual(self.row().status, "working")
        self.assertTrue(self.crons().get().enabled)

    def test_choose_schedules_stop_and_note_do_not(self):
        self.start()  # Leave its after-commit callback unexecuted in this test.
        StepPrep.objects.filter(pk=self.row().pk).update(status="needs_choice", choices=["Local", "Online"])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.jwt().post(self.url + "choose/", {"choice": "Local"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.crons().count(), 1)
        self.upload.reset_mock()
        with self.captureOnCommitCallbacks(execute=True), patch("apps.friends.project_prep.run_soon") as schedule:
            self.assertEqual(self.jwt().post(self.url + "stop/", {}, format="json").status_code, 200)
            access.create_note(self.goal, self.step, self.owner_member, "Do not wake", [])
        schedule.assert_not_called()
        self.wake.assert_not_called()
        self.upload.assert_not_called()

    @override_settings(STEP_PREP_TENANT_IDS="")
    def test_flag_off_is_404_and_service_dispatch_is_also_gated(self):
        self.assertEqual(self.start().status_code, 404)
        with self.captureOnCommitCallbacks(execute=True):
            scheduling.run_soon(self.holder)
        self.assertFalse(self.crons().exists())
        self.wake.assert_not_called()

    def test_creation_waits_for_commit_and_rollback_discards_callback(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.start().status_code, 200)
            self.assertFalse(self.crons().exists())
        self.assertEqual(self.crons().count(), 1)
        self.crons().delete()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    scheduling.run_soon(self.holder)
                    raise ValueError("rollback")
            except ValueError:
                pass
        self.assertFalse(self.crons().exists())

    def test_cache_refresh_preserves_dispatch_only_and_welcome_keeps_old_behavior(self):
        for upsert in [upsert_jobs_to_cache, upsert_from_gateway_jobs]:
            with self.subTest(upsert=upsert.__name__):
                self.crons().delete()
                scheduling._schedule(self.holder.pk)
                cron = self.crons().get()
                welcome = CronJob.objects.create(
                    tenant=self.holder,
                    name="_finance:welcome",
                    source="system",
                    creation_path="internal",
                    data={"payload": {"kind": "agentTurn", "message": "Welcome"}},
                )
                upsert(
                    self.holder,
                    [
                        {"name": cron.name, "enabled": False, "payload": {}},
                        {"name": welcome.name, "payload": {"message": "Refreshed"}},
                    ],
                )
                cron.refresh_from_db()
                welcome.refresh_from_db()
                self.assertTrue(cron.enabled)
                self.assertFalse(cron.managed)
                self.assertEqual(cron.source, "system")
                self.assertEqual(welcome.data["payload"]["message"], "Refreshed")
                upsert(self.holder, [])
                self.assertTrue(self.crons().filter(pk=cron.pk).exists())
                self.assertFalse(CronJob.objects.filter(pk=welcome.pk).exists())

    def test_reconcile_and_retirement_never_recreate_spent_dispatch(self):
        self.committed_start()
        cron = self.crons().get()
        refresh_system_cron_rows_from_seed(self.holder)
        self.assertTrue(self.crons().filter(pk=cron.pk).exists())
        self.upload.reset_mock()
        regenerate_tenant_crons(self.holder)
        doc = json.loads(self.upload.call_args.kwargs["data"])
        self.assertIn(cron.name, [j["name"] for j in json.loads(doc["signed"])])
        cron.data["schedule"]["at"] = (timezone.now() - timedelta(hours=1)).isoformat()
        with suppress_cronjob_reconcile():
            cron.save(update_fields=["data"])
        regenerate_tenant_crons(self.holder)
        doc = json.loads(self.upload.call_args.kwargs["data"])
        self.assertNotIn(cron.name, [j["name"] for j in json.loads(doc["signed"])])
        self.assertIn(cron.pk, expire_finished_at_crons_task()["ids"])
        cron.refresh_from_db()
        self.assertFalse(cron.enabled)
        doc, _ = build_signed_crons_doc(self.holder)
        self.assertNotIn(cron.name, json.loads(doc)["signed"])


@override_settings(PROJECTS_V2_TENANT_IDS="*", STEP_PREP_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class PrepWakeTransactionTests(TransactionTestCase):
    jwt = StepPrepTests.jwt
    start = StepPrepTests.start
    row = StepPrepTests.row
    setUp = StepPrepTests.setUp

    def test_claim_commits_and_releases_tenant_lock_before_wake_or_push(self):
        Tenant.objects.filter(pk=self.holder.pk).update(hibernated_at=timezone.now())

        def wake(_tenant_id):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(CronJob.objects.filter(tenant=self.holder, enabled=True).exists())
            return {"status": "woken_for_cron"}

        def push(*_args):
            self.assertFalse(connection.in_atomic_block)

        with (
            patch.object(scheduling, "check_budget", return_value=""),
            patch.object(scheduling, "wake_for_cron_task", side_effect=wake) as woken,
            patch.object(scheduling, "_push_at_cron_immediately", side_effect=push) as pushed,
        ):
            self.assertEqual(self.start().status_code, 200)
        woken.assert_called_once()
        pushed.assert_called_once()
