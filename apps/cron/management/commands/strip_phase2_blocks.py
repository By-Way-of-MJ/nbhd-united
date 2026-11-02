"""Strip the retired Phase 2 sync block from stored cron job messages.

Some ``CronJob`` rows still carry the old Phase 2 wrapper (or the old
"proceed to the FINAL STEP" prose) from before the feature was retired.
This command sweeps those rows at rest. It is a dry run unless ``--apply``
is passed.

Usage::

    python manage.py strip_phase2_blocks --tenant <uuid>
    python manage.py strip_phase2_blocks --tenant <uuid> --apply
    python manage.py strip_phase2_blocks --all --apply
"""

from __future__ import annotations

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.cron.models import CronJob
from apps.orchestrator.config_generator import (
    _RETIRED_FINAL_STEP_PROSE,
    PHASE2_SYNC_MARKER,
    upgrade_retired_final_step_prose,
)
from apps.tenants.models import Tenant

_RETIRED_OLD_PROSE = tuple(old for old, _new in _RETIRED_FINAL_STEP_PROSE)


def _matching_message(message: object) -> bool:
    """Whether a stored message needs the Phase 2 / old-prose cleanup."""
    if not isinstance(message, str):
        return False
    return PHASE2_SYNC_MARKER in message or any(old in message for old in _RETIRED_OLD_PROSE)


def _cleaned_message(message: str) -> str:
    from apps.cron.tenant_views import _strip_phase2_block

    return upgrade_retired_final_step_prose(_strip_phase2_block(message))


class Command(BaseCommand):
    help = "Strip the retired Phase 2 sync block and old FINAL STEP prose from stored cron messages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            type=UUID,
            help="Only sweep a single tenant (UUID).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Sweep every tenant.",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Report matching rows without writing (the default).",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Write the cleaned messages.",
        )

    def handle(self, *args, **options):
        tenant_id = options.get("tenant")
        all_tenants = options.get("all")
        if not tenant_id and not all_tenants:
            raise CommandError("Pass --tenant <uuid> or --all")
        if tenant_id and all_tenants:
            raise CommandError("Pass either --tenant or --all, not both")

        if tenant_id:
            tenants = list(Tenant.objects.filter(id=tenant_id).select_related("user"))
            if not tenants:
                raise CommandError(f"Tenant {tenant_id} not found")
        else:
            tenants = list(Tenant.objects.all().select_related("user"))

        apply = bool(options.get("apply"))
        self.stdout.write(f"Scanning {len(tenants)} tenant(s)" + ("" if apply else " [DRY RUN]"))

        tenants_scanned = 0
        rows_matched = 0
        rows_written = 0
        skipped_noncanonical = 0

        for tenant in tenants:
            tenants_scanned += 1
            matched = 0
            written = 0
            rows = CronJob.objects.filter(tenant=tenant).order_by("name")
            for row in rows:
                data = row.data if isinstance(row.data, dict) else {}
                payload = data.get("payload")
                if not isinstance(payload, dict):
                    continue
                message = payload.get("message")
                if not _matching_message(message):
                    continue

                matched += 1
                if not tenant.postgres_cron_canonical:
                    skipped_noncanonical += 1
                    continue
                if not apply:
                    continue

                new_message = _cleaned_message(message)
                if new_message == message:
                    continue
                row.data = {**data, "payload": {**payload, "message": new_message}}
                # Normal save: the post_save signal must fire so the reconciler
                # pushes the clean prompt to the container.
                row.save(update_fields=["data", "updated_at"])
                written += 1

            if matched:
                prefix = str(tenant.id)[:8]
                if tenant.postgres_cron_canonical:
                    self.stdout.write(f"{prefix} rows_matched={matched} rows_written={written}")
                else:
                    self.stdout.write(f"{prefix} rows_matched={matched} rows_written=0 skipped_noncanonical={matched}")
                rows_matched += matched
                rows_written += written

        self.stdout.write(
            self.style.SUCCESS(
                f"tenants_scanned={tenants_scanned} rows_matched={rows_matched} "
                f"rows_written={rows_written} skipped_noncanonical={skipped_noncanonical}"
            )
        )
