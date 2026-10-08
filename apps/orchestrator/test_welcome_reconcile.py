"""Tests for the daily reconcile_welcomes watchdog task.

The watchdog is the third leg of welcome delivery (after the live
toggle path and the deploy backfill). Its job is to retry tenants
whose welcome was orphaned — e.g., the agent crashed mid-turn during
the original fire and never self-removed the cron, leaving the system
"convinced" a welcome is still pending when its fire date is in the
past. Phase 1.3.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from apps.orchestrator.tasks import reconcile_welcomes_task
from apps.orchestrator.welcome_scheduler import WelcomeStatus
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_active_tenant(*, suffix: int, fuel: bool = False, finance: bool = False):
    tenant = create_tenant(display_name=f"WelcomeReconcile-{suffix}", telegram_chat_id=901000 + suffix)
    tenant.status = Tenant.Status.ACTIVE
    tenant.container_id = "/some/container/id"
    tenant.container_fqdn = "oc-fake.example.com"
    tenant.fuel_enabled = fuel
    tenant.finance_enabled = finance
    tenant.save()
    return tenant


class ReconcileWelcomesTaskTests(TestCase):
    def test_first_session_stamp_is_not_a_reconcile_feature(self):
        tenant = _make_active_tenant(suffix=7, fuel=False, finance=False)
        tenant.welcomes_sent = {"first_session": "2026-08-02T00:00:00+00:00"}
        tenant.save(update_fields=["welcomes_sent"])

        with mock.patch(
            "apps.orchestrator.first_session_welcome.seed_first_session_welcome",
            side_effect=AssertionError("fleet reconcile must not seed first-session welcomes"),
        ) as first_session:
            totals = reconcile_welcomes_task()

        first_session.assert_not_called()
        self.assertEqual(set(totals), {"tenants", "fuel", "finance", "statuses"})
        self.assertEqual(totals["fuel"], {})
        self.assertEqual(totals["finance"], {})

    def test_walks_only_feature_enabled_tenants(self):
        _make_active_tenant(suffix=1, fuel=True, finance=False)
        _make_active_tenant(suffix=2, fuel=False, finance=True)
        _make_active_tenant(suffix=3, fuel=False, finance=False)  # not walked

        with (
            mock.patch(
                "apps.fuel.views._schedule_fuel_welcome",
                return_value=WelcomeStatus.SCHEDULED,
            ) as mock_fuel,
            mock.patch(
                "apps.finance.views._schedule_finance_welcome",
                return_value=WelcomeStatus.SCHEDULED,
            ) as mock_fin,
        ):
            totals = reconcile_welcomes_task()

        self.assertEqual(mock_fuel.call_count, 1)
        self.assertEqual(mock_fin.call_count, 1)
        self.assertEqual(totals["fuel"], {"scheduled": 1})
        self.assertEqual(totals["finance"], {"scheduled": 1})

    def test_counts_failures_per_feature(self):
        """A scheduler exception is tallied, not propagated — one bad
        tenant must not abort the fleet sweep."""
        _make_active_tenant(suffix=4, fuel=True)
        _make_active_tenant(suffix=5, fuel=True)

        with mock.patch(
            "apps.fuel.views._schedule_fuel_welcome",
            side_effect=[WelcomeStatus.SCHEDULED, RuntimeError("simulated gateway down")],
        ):
            totals = reconcile_welcomes_task()

        # Both tenants visited; one scheduled, one failed.
        self.assertEqual(totals["fuel"].get("scheduled", 0), 1)
        self.assertEqual(totals["fuel"].get("failed", 0), 1)

    def test_distinguishes_status_categories(self):
        _make_active_tenant(suffix=6, fuel=True, finance=True)

        with (
            mock.patch(
                "apps.fuel.views._schedule_fuel_welcome",
                return_value=WelcomeStatus.REPLACED_STALE,
            ),
            mock.patch(
                "apps.finance.views._schedule_finance_welcome",
                return_value=WelcomeStatus.SKIPPED_ALREADY_DELIVERED,
            ),
        ):
            totals = reconcile_welcomes_task()

        self.assertEqual(totals["fuel"], {"replaced_stale": 1})
        self.assertEqual(totals["finance"], {"skipped_already_delivered": 1})


class Oc94WelcomeRowTests(TestCase):
    """9.4 gates the gateway cron.*: a welcome is a one-shot ``at`` CronJob row
    published in the signed crons file."""

    def setUp(self):
        from apps.tenants.models import Tenant

        self.tenant = create_tenant(display_name="Welcome94", telegram_chat_id=901994)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-w94.example.com"
        self.tenant.openclaw_version = "2026.9.4"
        self.tenant.save(update_fields=["status", "container_fqdn", "openclaw_version"])

    def _schedule(self):
        from apps.orchestrator.welcome_scheduler import schedule_welcome

        return schedule_welcome(
            self.tenant, feature="fuel", cron_name="_fuel:welcome", prompt_template="Hi {tenant_id}"
        )

    @mock.patch("apps.cron.gateway_client.invoke_gateway_tool")
    @mock.patch("apps.cron.share_cron_sync.write_tenant_crons_file", return_value=1)
    def test_creates_at_row_publishes_and_stamps(self, mock_write, mock_invoke):
        from apps.cron.models import CronJob
        from apps.orchestrator.welcome_scheduler import WelcomeStatus

        self.assertEqual(self._schedule(), WelcomeStatus.SCHEDULED)
        row = CronJob.objects.get(tenant=self.tenant, name="_fuel:welcome")
        self.assertEqual(row.data["schedule"]["kind"], "at")
        self.assertEqual(row.data["payload"]["message"], f"Hi {self.tenant.id}")
        self.assertFalse(row.managed)
        mock_write.assert_called_once_with(self.tenant)
        mock_invoke.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertIn("fuel", self.tenant.welcomes_sent)

    @mock.patch("apps.cron.share_cron_sync.write_tenant_crons_file", return_value=1)
    def test_pending_row_skips_and_stale_row_is_replaced(self, _mock_write):
        from datetime import timedelta

        from django.utils import timezone

        from apps.cron.models import CronJob
        from apps.orchestrator.welcome_scheduler import WelcomeStatus

        self._schedule()
        self.tenant.welcomes_sent = {}
        self.tenant.save(update_fields=["welcomes_sent"])
        self.assertEqual(self._schedule(), WelcomeStatus.SKIPPED_PENDING)

        row = CronJob.objects.get(tenant=self.tenant, name="_fuel:welcome")
        row.data["schedule"]["at"] = (timezone.now() - timedelta(hours=1)).isoformat()
        row.save(update_fields=["data"])
        self.assertEqual(self._schedule(), WelcomeStatus.REPLACED_STALE)
        self.assertEqual(CronJob.objects.filter(tenant=self.tenant, name="_fuel:welcome").count(), 1)

    @mock.patch("apps.cron.share_cron_sync.write_tenant_crons_file", side_effect=RuntimeError("share down"))
    def test_publish_failure_leaves_no_row_and_no_stamp(self, _mock_write):
        from apps.cron.models import CronJob

        with self.assertRaises(RuntimeError):
            self._schedule()
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name="_fuel:welcome").exists())
        self.tenant.refresh_from_db()
        self.assertNotIn("fuel", self.tenant.welcomes_sent or {})
