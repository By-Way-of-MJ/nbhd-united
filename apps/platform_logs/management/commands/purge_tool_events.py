"""Retention purge for tool-contract telemetry.

Telemetry answers "is this drifting right now" and "did this drift last month".
Beyond a quarter it is neither, and it is still a per-tenant row count that grows
without limit. Default retention is 90 days.

Scheduling: this is a management command on purpose — no new scheduling infra.
Trigger it the way the other sweeps are triggered (QStash → an ops endpoint, or a
manual run); see docs/agents/telemetry.md.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from time import monotonic

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.platform_logs.models import ToolContractEvent

DEFAULT_RETENTION_DAYS = 90


class _TimeLimitReached(Exception):
    pass


@contextmanager
def _query_budget(deadline):
    """Bound slow SQL and lock waits without holding locks across batches.

    SET LOCAL works with transaction-pooler connections. Restore the previous
    value on success too, since a caller may already be in an outer transaction.
    Database failures roll back this batch and propagate for QStash retry.
    """
    if deadline is None:
        yield
        return
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _TimeLimitReached
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SHOW statement_timeout")
                previous = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [f"{max(1, min(5000, int(remaining * 1000)))}ms"],
                )
        yield
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])


class Command(BaseCommand):
    help = "Delete tool-contract events older than the retention window (default 90 days)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"Retention window in days (default {DEFAULT_RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5000,
            help="Rows per delete batch, so a large backlog never holds one long lock (default 5000).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted, delete nothing.")
        parser.add_argument("--max-batches", type=int, default=None, help="Maximum delete batches (default unlimited).")
        parser.add_argument(
            "--max-seconds",
            type=int,
            default=None,
            help="Stop starting batches after this many seconds; cap PostgreSQL statements at 5s (default unlimited).",
        )

    def handle(self, *args, **options) -> None:
        days = options["older_than_days"]
        batch_size = options["batch_size"]
        max_batches = options["max_batches"]
        max_seconds = options["max_seconds"]
        if days < 1:
            raise CommandError("--older-than-days must be at least 1")
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1")
        if max_batches is not None and max_batches < 1:
            raise CommandError("--max-batches must be at least 1")
        if max_seconds is not None and max_seconds < 1:
            raise CommandError("--max-seconds must be at least 1")

        deadline = monotonic() + max_seconds if max_seconds is not None else None
        cutoff = timezone.now() - timedelta(days=days)
        queryset = ToolContractEvent.objects.filter(created_at__lt=cutoff)
        with _query_budget(deadline):
            targeted = queryset.count()

        self.stdout.write(f"Cutoff: {cutoff.isoformat()} (older than {days}d)")
        self.stdout.write(f"Targeted: {targeted}")

        if options["dry_run"]:
            self.stdout.write("DRY RUN — nothing deleted")
            return

        deleted = 0
        batches = 0
        remaining = 0
        reason = ""
        try:
            while max_batches is None or batches < max_batches:
                if deadline is not None and monotonic() >= deadline:
                    raise _TimeLimitReached
                with _query_budget(deadline):
                    batch_ids = list(queryset.values_list("id", flat=True)[:batch_size])
                    if not batch_ids:
                        break
                    count, _ = ToolContractEvent.objects.filter(id__in=batch_ids).delete()
                deleted += count
                batches += 1
            else:
                with _query_budget(deadline):
                    remaining = queryset.count()
                reason = "batch limit reached"
        except _TimeLimitReached:
            remaining = "unchecked"
            reason = "time limit reached"

        self.stdout.write(self.style.SUCCESS(f"deleted {deleted} tool events"))
        if remaining:
            self.stdout.write(f"Remaining: {remaining} ({reason}); rerun to continue")
