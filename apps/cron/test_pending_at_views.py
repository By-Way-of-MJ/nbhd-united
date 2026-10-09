"""Tests for the pending-``at``-cron REST endpoints."""

from __future__ import annotations

import time
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.cron.test_tenant_views import _create_user_and_tenant


class PendingAtCronViewTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.tenant.container_fqdn = "oc-test.example.com"
        self.tenant.openclaw_version = "2026.5.28"  # gateway path; 9.4 reads Postgres (Oc94PendingAtTest)
        self.tenant.save(update_fields=["container_fqdn", "openclaw_version"])
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _make_at_job(self, *, name: str, job_id: str, fires_ms: int, enabled: bool = True) -> dict:
        return {
            "id": job_id,
            "name": name,
            "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
            "state": {"nextRunAtMs": fires_ms},
            "enabled": enabled,
            "payload": {"kind": "agentTurn", "message": f"reminder for {name}"},
            "delivery": {"mode": "none"},
        }

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_returns_only_at_kind_jobs_sorted_by_fire_time(self, mock_invoke):
        future_a = int(time.time() * 1000) + 60_000  # 1 min
        future_b = int(time.time() * 1000) + 600_000  # 10 min
        mock_invoke.side_effect = lambda tenant, tool, args: (
            {
                "details": {
                    "jobs": [
                        self._make_at_job(name="later", job_id="b", fires_ms=future_b),
                        self._make_at_job(name="sooner", job_id="a", fires_ms=future_a),
                        {
                            "id": "recurring",
                            "name": "Morning Briefing",
                            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                            "enabled": True,
                        },
                    ]
                }
            }
            if tool == "cron.list"
            else None
        )
        resp = self.client.get("/api/v1/cron-jobs/pending-at/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["jobs"]), 2)
        # Sorted next-to-fire first.
        self.assertEqual(body["jobs"][0]["name"], "sooner")
        self.assertEqual(body["jobs"][1]["name"], "later")
        self.assertEqual(body["soft_cap"], 20)
        self.assertFalse(body["stale"])

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_excludes_disabled_at_jobs(self, mock_invoke):
        future = int(time.time() * 1000) + 60_000
        mock_invoke.side_effect = lambda tenant, tool, args: (
            {
                "details": {
                    "jobs": [
                        self._make_at_job(name="off", job_id="a", fires_ms=future, enabled=False),
                        self._make_at_job(name="on", job_id="b", fires_ms=future),
                    ]
                }
            }
            if tool == "cron.list"
            else None
        )
        resp = self.client.get("/api/v1/cron-jobs/pending-at/")
        self.assertEqual(resp.status_code, 200)
        names = [j["name"] for j in resp.json()["jobs"]]
        self.assertEqual(names, ["on"])

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_excludes_internal_at_jobs_but_keeps_user_underscore_job(self, mock_invoke):
        from apps.cron.models import CronJobSource

        future = int(time.time() * 1000) + 60_000
        internal = self._make_at_job(name="_congrats-162769ab", job_id="internal", fires_ms=future)
        user_owned = self._make_at_job(name="_x", job_id="user", fires_ms=future)
        user_owned["source"] = CronJobSource.USER
        mock_invoke.return_value = {"details": {"jobs": [internal, user_owned]}}

        resp = self.client.get("/api/v1/cron-jobs/pending-at/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["jobs"],
            [
                {
                    "jobId": "user",
                    "name": "_x",
                    "firesAtMs": future,
                    "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
                    "payload": {"kind": "agentTurn", "message": "reminder for _x"},
                    "delivery": {"mode": "none"},
                }
            ],
        )

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_hibernated_tenant_serves_from_cache(self, mock_invoke):
        from django.utils import timezone

        self.tenant.hibernated_at = timezone.now()
        self.tenant.cron_jobs_snapshot = {
            "jobs": [
                {
                    "id": "snap-1",
                    "name": "snapshot reminder",
                    "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
                    "state": {"nextRunAtMs": int(time.time() * 1000) + 3600_000},
                    "enabled": True,
                }
            ]
        }
        self.tenant.save(update_fields=["hibernated_at", "cron_jobs_snapshot"])

        resp = self.client.get("/api/v1/cron-jobs/pending-at/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["stale"])
        self.assertEqual(len(body["jobs"]), 1)
        # Gateway must not have been called.
        for call in mock_invoke.call_args_list:
            self.fail("Hibernated path should not hit gateway")

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_gateway_unavailable_falls_back_to_cache(self, mock_invoke):
        from apps.cron.gateway_client import GatewayError
        from apps.cron.models import CronJob, CronJobSource

        CronJob.objects.create(
            tenant=self.tenant,
            name="cached reminder",
            data={
                "id": "cached-1",
                "name": "cached reminder",
                "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
                "state": {"nextRunAtMs": int(time.time() * 1000) + 3600_000},
                "enabled": True,
            },
            source=CronJobSource.USER,
            managed=False,
        )

        err = GatewayError("Container App - Unavailable")
        err.status_code = 404
        mock_invoke.side_effect = err

        resp = self.client.get("/api/v1/cron-jobs/pending-at/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["stale"])
        self.assertEqual(body["jobs"][0]["name"], "cached reminder")

    def test_unauthenticated_blocked(self):
        client = APIClient()
        resp = client.get("/api/v1/cron-jobs/pending-at/")
        self.assertEqual(resp.status_code, 401)


class PendingAtCronCancelViewTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.tenant.container_fqdn = "oc-test.example.com"
        self.tenant.openclaw_version = "2026.5.28"  # gateway path; 9.4 reads Postgres (Oc94PendingAtTest)
        self.tenant.save(update_fields=["container_fqdn", "openclaw_version"])
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_cancel_removes_at_job(self, mock_invoke):
        calls: list[tuple[str, dict]] = []

        def _stub(tenant, tool, args):
            calls.append((tool, args))
            if tool == "cron.list":
                return {
                    "details": {
                        "jobs": [
                            {
                                "id": "at-1",
                                "name": "doomed reminder",
                                "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
                                "state": {"nextRunAtMs": int(time.time() * 1000) + 60_000},
                                "enabled": True,
                            }
                        ]
                    }
                }
            return None

        mock_invoke.side_effect = _stub
        resp = self.client.delete("/api/v1/cron-jobs/pending-at/doomed reminder/")
        self.assertEqual(resp.status_code, 204)
        remove_calls = [c for c in calls if c[0] == "cron.remove"]
        self.assertEqual(remove_calls, [("cron.remove", {"jobId": "at-1"})])

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_cancel_refuses_to_remove_recurring(self, mock_invoke):
        """A recurring task with the same name must not be deleted via this route."""
        calls: list[tuple[str, dict]] = []

        def _stub(tenant, tool, args):
            calls.append((tool, args))
            if tool == "cron.list":
                return {
                    "details": {
                        "jobs": [
                            {
                                "id": "recurring-1",
                                "name": "Morning Briefing",
                                "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                                "enabled": True,
                            }
                        ]
                    }
                }
            return None

        mock_invoke.side_effect = _stub
        resp = self.client.delete("/api/v1/cron-jobs/pending-at/Morning Briefing/")
        self.assertEqual(resp.status_code, 404)
        # cron.remove must not have been invoked.
        self.assertFalse(any(c[0] == "cron.remove" for c in calls))

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_cancel_nonexistent_returns_404(self, mock_invoke):
        mock_invoke.side_effect = lambda tenant, tool, args: {"details": {"jobs": []}} if tool == "cron.list" else None
        resp = self.client.delete("/api/v1/cron-jobs/pending-at/ghost/")
        self.assertEqual(resp.status_code, 404)

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_cancel_never_falls_back_from_missing_id_to_name(self, mock_invoke):
        mock_invoke.return_value = {
            "details": {
                "jobs": [
                    {
                        "name": "missing-id",
                        "schedule": {
                            "kind": "at",
                            "at": "2099-01-01T00:00:00Z",
                        },
                    }
                ]
            }
        }

        resp = self.client.delete("/api/v1/cron-jobs/pending-at/missing-id/")

        self.assertEqual(resp.status_code, 502)
        mock_invoke.assert_called_once_with(self.tenant, "cron.list", {})


class Oc94PendingAtTest(TestCase):
    """9.4 gates the gateway cron.list/remove: pending reminders come from Postgres."""

    def setUp(self):
        from datetime import UTC, datetime, timedelta

        from apps.cron.models import CronJob
        from apps.cron.signals import suppress_cronjob_reconcile

        self.user, self.tenant = _create_user_and_tenant()
        self.tenant.container_fqdn = "oc-test.example.com"
        self.tenant.openclaw_version = "2026.9.4"
        self.tenant.postgres_cron_canonical = True  # every prod tenant
        self.tenant.save(update_fields=["container_fqdn", "openclaw_version", "postgres_cron_canonical"])
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        now = datetime.now(UTC)

        def at(name, delta, enabled=True):
            return CronJob.objects.create(
                tenant=self.tenant,
                name=name,
                enabled=enabled,
                data={
                    "schedule": {"kind": "at", "at": (now + delta).isoformat()},
                    "payload": {"kind": "agentTurn", "message": f"remind {name}"},
                    "delivery": {"mode": "none"},
                },
            )

        with suppress_cronjob_reconcile():
            at("later", timedelta(hours=2))
            at("sooner", timedelta(minutes=5))
            at("spent", timedelta(minutes=-5))
            at("off", timedelta(hours=1), enabled=False)
            CronJob.objects.create(
                tenant=self.tenant,
                name="Morning Briefing",
                data={"schedule": {"kind": "cron", "expr": "0 7 * * *"}, "payload": {"kind": "agentTurn"}},
            )

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_lists_future_enabled_at_rows_from_postgres(self, mock_invoke):
        resp = self.client.get("/api/v1/cron-jobs/pending-at/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual([j["name"] for j in body["jobs"]], ["sooner", "later"])
        self.assertEqual(body["jobs"][0]["payload"]["message"], "remind sooner")
        self.assertFalse(body["stale"])
        mock_invoke.assert_not_called()

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_lists_while_hibernated(self, mock_invoke):
        from django.utils import timezone

        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])
        resp = self.client.get("/api/v1/cron-jobs/pending-at/")
        self.assertEqual([j["name"] for j in resp.json()["jobs"]], ["sooner", "later"])
        self.assertFalse(resp.json()["stale"])
        mock_invoke.assert_not_called()

    @patch("apps.cron.pending_at_views.invoke_gateway_tool")
    def test_cancel_deletes_row(self, mock_invoke):
        from apps.cron.models import CronJob
        from apps.cron.signals import connect_cronjob_reconcile_signals, disconnect_cronjob_reconcile_signals

        connect_cronjob_reconcile_signals()
        self.addCleanup(disconnect_cronjob_reconcile_signals)
        with patch("apps.cron.signals._enqueue_regen") as regen:
            resp = self.client.delete("/api/v1/cron-jobs/pending-at/sooner/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name="sooner").exists())
        mock_invoke.assert_not_called()
        regen.assert_called_once_with(str(self.tenant.id))  # republishes the signed file

    def test_cancel_refuses_recurring_and_unknown(self):
        from apps.cron.models import CronJob

        self.assertEqual(self.client.delete("/api/v1/cron-jobs/pending-at/Morning%20Briefing/").status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/cron-jobs/pending-at/nope/").status_code, 404)
        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name="Morning Briefing").exists())
