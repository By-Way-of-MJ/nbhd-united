"""Gateway imports into Postgres must not restore the retired Phase 2 block."""

from __future__ import annotations

from django.test import TestCase

from apps.cron.models import CronJob
from apps.cron.postgres_canonical import upsert_from_gateway_jobs
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator.config_generator import PHASE2_SYNC_MARKER
from apps.tenants.services import create_tenant


class PostgresCanonicalPhase2ImportTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Canonical import", telegram_chat_id=86420975)
        self.enterContext(suppress_cronjob_reconcile())

    def test_upsert_from_gateway_jobs_strips_phase2_without_mutating_input(self):
        legacy = "base" + "\n\n---\n**" + PHASE2_SYNC_MARKER + ":**\nold"
        job = {"name": "Morning Briefing", "id": "j-1", "payload": {"kind": "agentTurn", "message": legacy}}

        upsert_from_gateway_jobs(self.tenant, [job])

        row = CronJob.objects.get(tenant=self.tenant, name="Morning Briefing")
        self.assertEqual(row.data["payload"]["message"], "base")
        self.assertEqual(job["payload"]["message"], legacy)
