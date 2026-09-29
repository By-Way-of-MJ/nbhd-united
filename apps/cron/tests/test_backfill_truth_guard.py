"""backfill_postgres_cron_truth must not import over canonical 9.4 tenants."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.cron.models import CronJob
from apps.tenants.models import Tenant, User


class BackfillTruthOc94GuardTest(TestCase):
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_refuses_canonical_9_4_tenant(self, mock_invoke):
        user = User.objects.create_user(username="backfill94", password="x")
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_id="oc-bf94",
            container_fqdn="oc-bf94.internal",
            postgres_cron_canonical=True,
            openclaw_version="2026.9.4",
            cron_jobs_snapshot={"jobs": [{"name": "Morning Briefing", "enabled": True}]},
        )
        CronJob.objects.create(
            tenant=tenant, name="Morning Briefing", data={"payload": {"kind": "agentTurn", "message": "real"}}
        )
        out = StringIO()
        call_command("backfill_postgres_cron_truth", "--tenant", str(tenant.id), stdout=out)
        self.assertIn("already canonical on OpenClaw 9.4", out.getvalue())
        self.assertEqual(CronJob.objects.get(tenant=tenant).data["payload"]["message"], "real")
        mock_invoke.assert_not_called()
