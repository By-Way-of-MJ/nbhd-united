"""Import/preserve paths must not bring the retired Phase 2 block back."""

from __future__ import annotations

import copy
from unittest.mock import patch

from django.test import TestCase

from apps.cron.models import CronJob, CronJobSource
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator.config_generator import PHASE2_SYNC_MARKER, build_cron_seed_jobs
from apps.orchestrator.send_test import SEND_TEST_BLOCK
from apps.orchestrator.services import refresh_system_cron_rows_from_seed, update_system_cron_prompts
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _legacy_message() -> str:
    return "base" + "\n\n---\n**" + PHASE2_SYNC_MARKER + ":**\nold"


class RefreshPreservedCustomMessageTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Phase 2 preserved", telegram_chat_id=86420976)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status"])
        self.enterContext(suppress_cronjob_reconcile())

    def test_custom_prompt_preserved_by_seed_refresh_is_stripped(self):
        seed = next(job for job in build_cron_seed_jobs(self.tenant) if job["name"] == "Morning Briefing")
        seed["payload"]["message"] = _legacy_message()
        row = CronJob.objects.create(
            tenant=self.tenant,
            name="Morning Briefing",
            data=seed,
            source=CronJobSource.SYSTEM,
            managed=True,
            enabled=True,
        )

        summary = refresh_system_cron_rows_from_seed(self.tenant)

        row.refresh_from_db()
        self.assertEqual(row.data["payload"]["message"], "base")
        self.assertEqual(summary["preserved_custom"], 1)


class UpdateSystemCronPromptsPhase2Test(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Phase 2 update", telegram_chat_id=86420977)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status"])
        self.enterContext(suppress_cronjob_reconcile())

    def test_custom_message_written_by_send_test_cleanup_is_stripped(self):
        jobs = copy.deepcopy(build_cron_seed_jobs(self.tenant))
        for i, job in enumerate(jobs):
            job["id"] = f"local-{i}"
        target = next(job for job in jobs if job["name"] == "Morning Briefing")
        target["payload"]["message"] = _legacy_message() + SEND_TEST_BLOCK

        with patch("apps.orchestrator.services.invoke_gateway_tool", return_value={"jobs": jobs}) as gateway:
            update_system_cron_prompts(self.tenant)

        replacements = [
            call.args[2]["job"]
            for call in gateway.call_args_list
            if call.args[1] == "cron.add" and call.args[2]["job"]["name"] == "Morning Briefing"
        ]
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0]["payload"]["message"], "base")
