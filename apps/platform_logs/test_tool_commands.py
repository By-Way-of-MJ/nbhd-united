"""Dead-tool reporting and telemetry retention purge."""

from __future__ import annotations

import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.test import TestCase
from django.utils import timezone

from .models import ToolContractEvent


def _event(tool_name: str, outcome: str, *, age_days: int = 0) -> ToolContractEvent:
    event = ToolContractEvent.objects.create(
        namespace="runtime",
        tool_name=tool_name,
        tenant_id=uuid.uuid4(),
        outcome=outcome,
        duration_ms=10,
    )
    if age_days:
        # auto_now_add ignores an explicit value, so age the row after insert.
        ToolContractEvent.objects.filter(id=event.id).update(created_at=timezone.now() - timedelta(days=age_days))
    return event


class ReportToolHealthTests(TestCase):
    def _run(self, **kwargs) -> str:
        out = StringIO()
        call_command("report_tool_health", stdout=out, **kwargs)
        return out.getvalue()

    def test_flags_a_dead_tool_and_ignores_a_healthy_one(self):
        for _ in range(6):
            _event("runtime-broken-tool", ToolContractEvent.Outcome.ERROR)
        for _ in range(6):
            _event("runtime-healthy-tool", ToolContractEvent.Outcome.ACCEPTED)

        output = self._run(min_calls=5)

        self.assertIn("DEAD TOOLS (1)", output)
        self.assertIn("runtime-broken-tool (6 calls, 100% error)", output)
        # The healthy tool appears in the table but never in the dead list.
        self.assertIn("runtime-healthy-tool", output)
        dead_section = output.split("DEAD TOOLS", 1)[1]
        self.assertNotIn("runtime-healthy-tool", dead_section)

    def test_mostly_broken_tool_is_not_dead(self):
        """One success is enough to prove the tool still works at all."""
        for _ in range(9):
            _event("runtime-flaky-tool", ToolContractEvent.Outcome.ERROR)
        _event("runtime-flaky-tool", ToolContractEvent.Outcome.ACCEPTED)

        output = self._run(min_calls=5)

        self.assertIn("No dead tools in window.", output)
        self.assertIn("90.0%", output)

    def test_below_min_calls_is_not_flagged(self):
        for _ in range(2):
            _event("runtime-rare-tool", ToolContractEvent.Outcome.ERROR)
        self.assertIn("No dead tools in window.", self._run(min_calls=5))

    def test_events_outside_the_window_are_ignored(self):
        for _ in range(6):
            _event("runtime-broken-tool", ToolContractEvent.Outcome.ERROR, age_days=30)
        output = self._run(days=7, min_calls=5)
        self.assertIn("No tool events in window.", output)

    def test_reports_rejects_separately_from_errors(self):
        _event("runtime-picky-tool", ToolContractEvent.Outcome.REJECTED)
        _event("runtime-picky-tool", ToolContractEvent.Outcome.ACCEPTED)
        output = self._run(min_calls=1)
        self.assertIn("No dead tools in window.", output)

    def test_is_report_only(self):
        _event("runtime-broken-tool", ToolContractEvent.Outcome.ERROR)
        before = ToolContractEvent.objects.count()
        self._run(min_calls=1)
        self.assertEqual(ToolContractEvent.objects.count(), before)


class PurgeToolEventsTests(TestCase):
    def _run(self, **kwargs) -> str:
        out = StringIO()
        call_command("purge_tool_events", stdout=out, **kwargs)
        return out.getvalue()

    def test_purges_only_rows_past_retention(self):
        _event("old-tool", ToolContractEvent.Outcome.ACCEPTED, age_days=120)
        _event("recent-tool", ToolContractEvent.Outcome.ACCEPTED, age_days=10)

        output = self._run(older_than_days=90)

        self.assertIn("deleted 1 tool events", output)
        self.assertEqual(
            list(ToolContractEvent.objects.values_list("tool_name", flat=True)),
            ["recent-tool"],
        )

    def test_dry_run_deletes_nothing(self):
        _event("old-tool", ToolContractEvent.Outcome.ACCEPTED, age_days=120)
        output = self._run(older_than_days=90, dry_run=True)
        self.assertIn("Targeted: 1", output)
        self.assertIn("DRY RUN", output)
        self.assertEqual(ToolContractEvent.objects.count(), 1)

    def test_batching_clears_a_backlog(self):
        for _ in range(7):
            _event("old-tool", ToolContractEvent.Outcome.ACCEPTED, age_days=120)
        self._run(older_than_days=90, batch_size=2)
        self.assertEqual(ToolContractEvent.objects.count(), 0)

    def test_max_batches_stops_after_two_batches_and_reports_remaining_work(self):
        for _ in range(7):
            _event("old-tool", ToolContractEvent.Outcome.ACCEPTED, age_days=120)
        recent = _event("recent-tool", ToolContractEvent.Outcome.ACCEPTED)

        output = self._run(batch_size=2, max_batches=2)

        self.assertIn("deleted 4 tool events", output)
        self.assertIn("Remaining: 3 (batch limit reached)", output)
        self.assertEqual(ToolContractEvent.objects.count(), 4)
        self.assertTrue(ToolContractEvent.objects.filter(id=recent.id).exists())

    def test_max_batches_rejects_non_positive_values(self):
        for max_batches in (0, -1):
            with self.subTest(max_batches=max_batches), self.assertRaisesMessage(CommandError, "--max-batches"):
                self._run(max_batches=max_batches)

    def test_max_seconds_rejects_non_positive_values(self):
        for max_seconds in (0, -1):
            with self.subTest(max_seconds=max_seconds), self.assertRaisesMessage(CommandError, "--max-seconds"):
                self._run(max_seconds=max_seconds)

    def test_time_budget_stops_before_starting_another_batch(self):
        for _ in range(7):
            _event("old-tool", ToolContractEvent.Outcome.ACCEPTED, age_days=120)
        # Initial count and first batch run before the deadline. It then expires.
        with patch("apps.platform_logs.management.commands.purge_tool_events.monotonic") as timer:
            timer.side_effect = [0, 0, 0, 0, 31]
            output = self._run(batch_size=2, max_seconds=30)

        self.assertIn("deleted 2 tool events", output)
        self.assertIn("time limit reached", output)
        self.assertEqual(ToolContractEvent.objects.count(), 5)

    def test_time_bounded_run_restores_postgres_statement_timeout(self):
        if connection.vendor != "postgresql":
            self.skipTest("Postgres statement timeout")
        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            before = cursor.fetchone()[0]
        _event("old-tool", ToolContractEvent.Outcome.ACCEPTED, age_days=120)

        self._run(max_seconds=30)

        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            self.assertEqual(cursor.fetchone()[0], before)

    def test_time_budget_cancels_slow_sql_and_leaves_connection_usable(self):
        if connection.vendor != "postgresql":
            self.skipTest("Postgres statement timeout")
        from apps.platform_logs.management.commands.purge_tool_events import _query_budget, monotonic

        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            before = cursor.fetchone()[0]
        with self.assertRaises(OperationalError), _query_budget(monotonic() + 0.02), connection.cursor() as cursor:
            cursor.execute("SELECT pg_sleep(0.1)")

        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            self.assertEqual(cursor.fetchone()[0], before)
