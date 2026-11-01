import copy
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.cron.models import CronJob, CronJobSource
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator.config_generator import build_cron_seed_jobs
from apps.orchestrator.cron_drift import job_drift
from apps.orchestrator.send_test import PERSONAL_QUESTION_SEND_TEST_BLOCK, SEND_TEST_BLOCK, SEND_TEST_JOBS
from apps.orchestrator.services import refresh_system_cron_rows_from_seed, update_system_cron_prompts
from apps.tenants.services import create_tenant


@override_settings(SEND_TEST_TENANT_IDS="", ONE_QUESTION_TENANT_IDS="")
class SendTestRefreshTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Send test", telegram_chat_id=87654321)
        self.tenant.container_id = "local-test"
        self.tenant.status = "active"
        self.tenant.save(update_fields=["container_id", "status"])
        self.enterContext(suppress_cronjob_reconcile())
        self.clock = self.enterContext(patch("apps.orchestrator.config_generator.datetime"))
        self.clock.now.return_value = datetime(2026, 11, 1, 8, tzinfo=UTC)
        refresh_system_cron_rows_from_seed(self.tenant)

    def rows(self):
        return {j.name: j for j in CronJob.objects.filter(tenant=self.tenant)}

    def test_flag_flip_converges_once_and_reverts_without_drift(self):
        before = {name: copy.deepcopy(row.data) for name, row in self.rows().items()}
        with override_settings(SEND_TEST_TENANT_IDS="*"):
            result = refresh_system_cron_rows_from_seed(self.tenant)
            self.assertEqual(result["updated"], len(SEND_TEST_JOBS))
            applied = {name: copy.deepcopy(row.data) for name, row in self.rows().items() if name in SEND_TEST_JOBS}
            self.clock.now.return_value += timedelta(hours=2)
            refresh_system_cron_rows_from_seed(self.tenant)
            self.assertEqual({name: row.data for name, row in self.rows().items() if name in SEND_TEST_JOBS}, applied)
            for row in self.rows().values():
                if row.name in SEND_TEST_JOBS:
                    self.assertEqual(row.data["payload"]["message"].count(SEND_TEST_BLOCK), 1)
                else:
                    self.assertEqual(job_drift(before[row.name], row.data), [])
        result = refresh_system_cron_rows_from_seed(self.tenant)
        self.assertEqual(result["updated"], len(SEND_TEST_JOBS))
        for name, row in self.rows().items():
            self.assertEqual(job_drift(before[name], row.data), [])
        self.clock.now.return_value += timedelta(hours=2)
        reverted = {name: row.data for name, row in self.rows().items() if name in SEND_TEST_JOBS}
        refresh_system_cron_rows_from_seed(self.tenant)
        self.assertEqual({name: row.data for name, row in self.rows().items() if name in SEND_TEST_JOBS}, reverted)

    def test_user_source_name_collision_and_edited_default_are_preserved(self):
        user_job = self.rows()["Morning Briefing"]
        user_job.source = CronJobSource.USER
        user_job.save(update_fields=["source"])
        edited = self.rows()["Evening Check-in"]
        edited.data["payload"]["message"] += "\n\nMy own evening instruction."
        edited.save(update_fields=["data"])
        original_user = copy.deepcopy(user_job.data)
        original_edit = copy.deepcopy(edited.data)
        with override_settings(SEND_TEST_TENANT_IDS="*"):
            refresh_system_cron_rows_from_seed(self.tenant)
        user_job.refresh_from_db()
        edited.refresh_from_db()
        self.assertEqual(user_job.data, original_user)
        self.assertEqual(edited.data, original_edit)

    def test_disable_removes_only_managed_rule_from_later_edit(self):
        with override_settings(SEND_TEST_TENANT_IDS="*"):
            refresh_system_cron_rows_from_seed(self.tenant)
        row = self.rows()["Morning Briefing"]
        row.data["payload"]["message"] += "\n\nKeep this edit."
        row.save(update_fields=["data"])
        refresh_system_cron_rows_from_seed(self.tenant)
        row.refresh_from_db()
        self.assertNotIn(SEND_TEST_BLOCK, row.data["payload"]["message"])
        self.assertTrue(row.data["payload"]["message"].endswith("Keep this edit."))

    def test_previous_managed_send_rule_upgrades_once(self):
        from apps.orchestrator.send_test import _PREVIOUS_SEND_TEST_BLOCK

        with override_settings(SEND_TEST_TENANT_IDS="*"):
            refresh_system_cron_rows_from_seed(self.tenant)
            row = self.rows()["Morning Briefing"]
            row.data["payload"]["message"] = row.data["payload"]["message"].replace(
                SEND_TEST_BLOCK, _PREVIOUS_SEND_TEST_BLOCK
            )
            row.save(update_fields=["data"])
            self.assertEqual(refresh_system_cron_rows_from_seed(self.tenant)["updated"], 1)
            self.assertEqual(refresh_system_cron_rows_from_seed(self.tenant)["updated"], 0)
        row.refresh_from_db()
        self.assertEqual(row.data["payload"]["message"].count(SEND_TEST_BLOCK), 1)

    def test_retired_personal_trial_reverts_once_and_preserves_edits(self):
        for edited in (False, True):
            with self.subTest(edited=edited):
                row = self.rows()["Personal Question"]
                original = row.data["payload"]["message"]
                row.data["payload"]["message"] += PERSONAL_QUESTION_SEND_TEST_BLOCK
                if edited:
                    row.data["payload"]["message"] += "\n\nKeep my personal edit."
                row.save(update_fields=["data"])
                with override_settings(SEND_TEST_TENANT_IDS="*"):
                    refresh_system_cron_rows_from_seed(self.tenant)
                    self.assertEqual(refresh_system_cron_rows_from_seed(self.tenant)["updated"], 0)
                row.refresh_from_db()
                self.assertEqual(
                    row.data["payload"]["message"], original + ("\n\nKeep my personal edit." if edited else "")
                )
                self.assertNotIn("Personal Question", SEND_TEST_JOBS)

    def test_management_command_scopes_one_tenant(self):
        with patch(
            "apps.orchestrator.management.commands.update_cron_prompts.refresh_system_cron_rows_from_seed"
        ) as refresh:
            refresh.return_value = {"created": 0, "updated": 0, "preserved_custom": 0}
            call_command("update_cron_prompts", tenant=str(self.tenant.id), stdout=StringIO())
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(refresh.call_args.args[0].id, self.tenant.id)

    def test_legacy_gateway_update_exempts_user_source_and_edited_prompt(self):
        jobs = copy.deepcopy(build_cron_seed_jobs(self.tenant))
        for i, job in enumerate(jobs):
            job["id"] = f"local-{i}"
            if "model" in job:
                job["payload"]["model"] = job.pop("model")
        row = self.rows()["Morning Briefing"]
        row.source = CronJobSource.USER
        row.save(update_fields=["source"])
        evening = next(j for j in jobs if j["name"] == "Evening Check-in")
        evening["payload"]["message"] += "\n\nKeep the edited evening."
        with (
            override_settings(SEND_TEST_TENANT_IDS="*"),
            patch("apps.orchestrator.services.invoke_gateway_tool") as gateway,
            self.assertLogs("apps.orchestrator.services", level="INFO") as logs,
        ):
            gateway.return_value = {"jobs": jobs}
            update_system_cron_prompts(self.tenant)
        removed = {c.args[2]["jobId"] for c in gateway.call_args_list if c.args[1] == "cron.remove"}
        self.assertNotIn(jobs[0]["id"], removed)
        self.assertNotIn(evening["id"], removed, logs.output)
        added = [c.args[2]["job"] for c in gateway.call_args_list if c.args[1] == "cron.add"]
        self.assertEqual({j["name"] for j in added}, SEND_TEST_JOBS - {"Morning Briefing", "Evening Check-in"})
