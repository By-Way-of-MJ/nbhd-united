"""Tests for ``ensure_at_cron_wakes_task`` — backstop wake scheduling
for one-off ``kind:"at"`` crons.

Background: the hibernation path only schedules QStash wakes when Django
itself decides to hibernate a tenant. If a container goes down out-of-band
(Azure replica recycle, OOM, crash) between an at-cron's creation and its
fire time, the fire is missed. This sweep walks the fleet every 5 minutes
and publishes ``wake_for_cron`` tasks idempotency-keyed on the fire time
so any at-cron firing within the look-ahead window has a wake guaranteed.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.cron.models import CronJob
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_active_tenant(*, name: str, chat_id: int, openclaw_version: str = "2026.5.28") -> Tenant:
    # Default 5.28 = the gateway cron.list path these tests mock; 9.4 reads the
    # in-container state file instead (Oc94StateFileTest).
    t = create_tenant(display_name=name, telegram_chat_id=chat_id)
    t.openclaw_version = openclaw_version
    t.status = Tenant.Status.ACTIVE
    t.container_id = f"oc-{name.lower().replace(' ', '-')}"
    t.container_fqdn = f"{t.container_id}.example.com"
    t.save()
    return t


def _at_job(*, name: str, job_id: str, fires_ms: int, enabled: bool = True) -> dict:
    return {
        "id": job_id,
        "name": name,
        "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
        "state": {"nextRunAtMs": fires_ms},
        "enabled": enabled,
        "payload": {"kind": "agentTurn", "message": "msg"},
    }


def _recurring_job(*, name: str, job_id: str) -> dict:
    return {
        "id": job_id,
        "name": name,
        "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
        "enabled": True,
    }


class EnsureAtCronWakesTaskTest(TestCase):
    def setUp(self):
        self.tenant = _make_active_tenant(name="Sweep One", chat_id=111111111)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_schedules_wake_for_at_in_window(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        fires_ms = int(time.time() * 1000) + 30 * 60 * 1000  # 30 min from now
        mock_invoke.return_value = {"details": {"jobs": [_at_job(name="laundry", job_id="at-1", fires_ms=fires_ms)]}}
        result = ensure_at_cron_wakes_task()
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(mock_publish.call_count, 1)
        call_args = mock_publish.call_args
        self.assertEqual(call_args.args, ("wake_for_cron", str(self.tenant.id)))
        self.assertEqual(call_args.kwargs["idempotency_key"], f"wake-cron-{self.tenant.id}-{fires_ms}")
        # Lead time of 240s should be subtracted; result is positive.
        self.assertGreater(call_args.kwargs["delay_seconds"], 0)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_ignores_recurring_crons(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        mock_invoke.return_value = {"details": {"jobs": [_recurring_job(name="Morning Briefing", job_id="rec-1")]}}
        result = ensure_at_cron_wakes_task()
        self.assertEqual(result["scheduled"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_skips_past_due_at(self, mock_invoke, mock_publish):
        """An at-cron whose fire time already passed gets no wake — the
        reconciler janitor reaps it; we don't try to revive it here."""
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        past_ms = int(time.time() * 1000) - 60_000  # 1 min ago
        mock_invoke.return_value = {"details": {"jobs": [_at_job(name="stale", job_id="at-stale", fires_ms=past_ms)]}}
        result = ensure_at_cron_wakes_task()
        self.assertEqual(result["scheduled"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_skips_beyond_lookahead_window(self, mock_invoke, mock_publish):
        """An at-cron firing more than 2h out is left for the next sweep
        — bounds QStash scheduled-task count."""
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        far_ms = int(time.time() * 1000) + 3 * 60 * 60 * 1000  # 3 hours
        mock_invoke.return_value = {"details": {"jobs": [_at_job(name="far-future", job_id="at-far", fires_ms=far_ms)]}}
        result = ensure_at_cron_wakes_task()
        self.assertEqual(result["scheduled"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_skips_disabled_at_crons(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        fires_ms = int(time.time() * 1000) + 30 * 60 * 1000
        mock_invoke.return_value = {
            "details": {"jobs": [_at_job(name="paused", job_id="at-off", fires_ms=fires_ms, enabled=False)]}
        }
        result = ensure_at_cron_wakes_task()
        self.assertEqual(result["scheduled"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_skips_hibernated_tenants(self, mock_invoke, mock_publish):
        """Hibernated tenants are owned by the hibernate-idle wake-scheduling
        path. Calling them here would risk waking them with no idle-recheck
        scheduled and would duplicate the work."""
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])
        result = ensure_at_cron_wakes_task()
        self.assertEqual(result["tenants"], 0)
        mock_invoke.assert_not_called()
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_gateway_error_is_swallowed_per_tenant(self, mock_invoke, mock_publish):
        from apps.cron.gateway_client import GatewayError
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        # Two tenants — one errors, one succeeds. Sweep must continue.
        t2 = _make_active_tenant(name="Sweep Two", chat_id=222222222)
        fires_ms = int(time.time() * 1000) + 30 * 60 * 1000

        def _stub(tenant, tool, args):
            if tenant.id == self.tenant.id:
                raise GatewayError("boom")
            return {"details": {"jobs": [_at_job(name="x", job_id="ok", fires_ms=fires_ms)]}}

        mock_invoke.side_effect = _stub
        result = ensure_at_cron_wakes_task()
        self.assertEqual(result["tenants"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["scheduled"], 1)
        # Verify the successful tenant's id was the one passed to publish_task.
        mock_publish.assert_called_once()
        self.assertEqual(mock_publish.call_args.args[1], str(t2.id))

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_idempotency_key_includes_fire_time(self, mock_invoke, mock_publish):
        """Two sweeps over the same at-cron emit publish_task with the same
        idempotency key — QStash dedups."""
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        fires_ms = int(time.time() * 1000) + 30 * 60 * 1000
        mock_invoke.return_value = {"details": {"jobs": [_at_job(name="dup", job_id="at-1", fires_ms=fires_ms)]}}
        ensure_at_cron_wakes_task()
        ensure_at_cron_wakes_task()
        self.assertEqual(mock_publish.call_count, 2)
        keys = [c.kwargs["idempotency_key"] for c in mock_publish.call_args_list]
        self.assertEqual(keys[0], keys[1])


class Oc94StateFileTest(TestCase):
    """9.4 gates the gateway cron.list; the backstop reads the helper's state file."""

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    @patch("apps.orchestrator.azure_client.download_workspace_file_binary")
    def test_schedules_wake_from_state_file(self, mock_download, mock_invoke, mock_publish):
        import json

        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        tenant = _make_active_tenant(name="Sweep 94", chat_id=111111194, openclaw_version="2026.9.4")
        now_ms = int(time.time() * 1000)
        fires_ms = now_ms + 30 * 60 * 1000
        job = _at_job(name="laundry", job_id="at-94", fires_ms=fires_ms)
        job.pop("payload")  # the state file never carries payloads
        mock_download.return_value = json.dumps({"v": 1, "writtenAtMs": now_ms, "jobs": [job]}).encode()

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["scheduled"], 1)
        mock_invoke.assert_not_called()
        self.assertEqual(mock_publish.call_args.kwargs["idempotency_key"], f"wake-cron-{tenant.id}-{fires_ms}")


class HibernatedCronWakesTest(TestCase):
    """Hibernated tenants get a DB-backed wake for rows added while asleep."""

    def setUp(self):
        cache.clear()
        self.tenant = _make_active_tenant(name="Hibernated Sweep", chat_id=333333333)
        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=timezone.now())
        self.tenant.refresh_from_db()

    def _row(self, *, name: str, data: dict, enabled: bool = True) -> CronJob:
        return CronJob.objects.create(tenant=self.tenant, name=name, data=data, enabled=enabled)

    def _at_data(self, *, minutes: int) -> tuple[dict, int]:
        fires_at = timezone.now() + timedelta(minutes=minutes)
        return {"schedule": {"kind": "at", "at": fires_at.isoformat()}}, int(fires_at.timestamp() * 1000)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_at_row_in_window_schedules_wake(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        data, fires_ms = self._at_data(minutes=10)
        self._row(name="at-ten", data=data)

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_tenants"], 1)
        self.assertEqual(result["hibernated_scheduled"], 1)
        mock_publish.assert_called_once()
        self.assertEqual(mock_publish.call_args.args, ("wake_for_cron", str(self.tenant.id)))
        self.assertEqual(
            mock_publish.call_args.kwargs["idempotency_key"],
            f"wake-cron-{self.tenant.id}-{fires_ms}",
        )
        self.assertGreaterEqual(mock_publish.call_args.kwargs["delay_seconds"], 60)
        mock_invoke.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_cron_row_within_window_schedules_wake(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        target = timezone.now() + timedelta(minutes=10)
        expr = f"{target.minute} {target.hour} * * *"
        self._row(name="cron-ten", data={"schedule": {"kind": "cron", "expr": expr, "tz": "UTC"}})

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_scheduled"], 1)
        mock_publish.assert_called_once()
        self.assertEqual(mock_publish.call_args.args, ("wake_for_cron", str(self.tenant.id)))
        expected = int(target.replace(second=0, microsecond=0).timestamp() * 1000)
        self.assertEqual(
            mock_publish.call_args.kwargs["idempotency_key"],
            f"wake-cron-{self.tenant.id}-{expected}",
        )
        self.assertGreaterEqual(mock_publish.call_args.kwargs["delay_seconds"], 60)
        mock_invoke.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_row_unchanged_since_hibernation_is_skipped(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        data, _ = self._at_data(minutes=10)
        self._row(name="at-old", data=data)
        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=timezone.now() + timedelta(seconds=1))

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_scheduled"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_second_sweep_does_not_republish_same_fire(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        data, _ = self._at_data(minutes=10)
        self._row(name="at-once", data=data)

        ensure_at_cron_wakes_task()
        second = ensure_at_cron_wakes_task()

        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(second["hibernated_scheduled"], 0)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_publish_failure_allows_retry_next_sweep(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        data, _ = self._at_data(minutes=10)
        self._row(name="at-retry", data=data)
        mock_publish.side_effect = [RuntimeError("x"), None]

        first = ensure_at_cron_wakes_task()
        second = ensure_at_cron_wakes_task()

        self.assertEqual(mock_publish.call_count, 2)
        self.assertGreaterEqual(first["errors"], 1)
        self.assertEqual(second["hibernated_scheduled"], 1)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_non_utc_cron_row(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        target = timezone.now().astimezone(ZoneInfo("Asia/Tokyo")) + timedelta(minutes=10)
        expr = f"{target.minute} {target.hour} * * *"
        self._row(
            name="cron-tokyo",
            data={"schedule": {"kind": "cron", "expr": expr, "tz": "Asia/Tokyo"}},
        )

        result = ensure_at_cron_wakes_task()

        expected = int(target.replace(second=0, microsecond=0).timestamp() * 1000)
        self.assertEqual(result["hibernated_scheduled"], 1)
        mock_publish.assert_called_once()
        self.assertEqual(
            mock_publish.call_args.kwargs["idempotency_key"],
            f"wake-cron-{self.tenant.id}-{expected}",
        )

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_tenant_without_container_is_skipped(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        Tenant.objects.filter(pk=self.tenant.pk).update(container_fqdn="")
        data, _ = self._at_data(minutes=10)
        self._row(name="at-no-container", data=data)

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_tenants"], 0)
        self.assertEqual(result["hibernated_scheduled"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_two_rows_schedules_only_earliest(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        ten_data, ten_ms = self._at_data(minutes=10)
        fifteen_data, _ = self._at_data(minutes=15)
        self._row(name="at-fifteen", data=fifteen_data)
        self._row(name="at-ten", data=ten_data)

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_scheduled"], 1)
        mock_publish.assert_called_once()
        self.assertEqual(
            mock_publish.call_args.kwargs["idempotency_key"],
            f"wake-cron-{self.tenant.id}-{ten_ms}",
        )
        mock_invoke.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_row_firing_in_two_hours_is_outside_window(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        data, _ = self._at_data(minutes=120)
        self._row(name="at-far", data=data)

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_tenants"], 1)
        self.assertEqual(result["hibernated_scheduled"], 0)
        mock_publish.assert_not_called()
        mock_invoke.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_disabled_row_is_ignored(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        data, _ = self._at_data(minutes=10)
        self._row(name="at-disabled", data=data, enabled=False)

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_scheduled"], 0)
        mock_publish.assert_not_called()
        mock_invoke.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_awake_tenant_row_is_not_scheduled_by_hibernated_pass(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=None)
        data, _ = self._at_data(minutes=10)
        self._row(name="at-awake", data=data)
        mock_invoke.return_value = {"details": {"jobs": []}}

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_tenants"], 0)
        self.assertEqual(result["hibernated_scheduled"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_suspended_hibernated_tenant_is_ignored(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.SUSPENDED)
        data, _ = self._at_data(minutes=10)
        self._row(name="at-suspended", data=data)

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_tenants"], 0)
        self.assertEqual(result["hibernated_scheduled"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_every_kind_row_is_ignored_without_crashing(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        self._row(
            name="every-row",
            data={"schedule": {"kind": "every", "everyMs": 600000}},
        )

        result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_tenants"], 1)
        self.assertEqual(result["hibernated_scheduled"], 0)
        self.assertEqual(result["errors"], 0)
        mock_publish.assert_not_called()
        mock_invoke.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_cache_outage_returning_none_still_publishes(self, mock_invoke, mock_publish):
        from apps.orchestrator.tasks import ensure_at_cron_wakes_task

        data, _ = self._at_data(minutes=10)
        self._row(name="at-cache-down", data=data)

        with patch("django.core.cache.cache.add", return_value=None):
            result = ensure_at_cron_wakes_task()

        self.assertEqual(result["hibernated_scheduled"], 1)
        self.assertEqual(mock_publish.call_count, 1)
