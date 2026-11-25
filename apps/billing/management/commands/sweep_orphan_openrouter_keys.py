"""Reap orphan OpenRouter sub-keys + KV secrets (PR #1.6 Phase 5b).

Two failure modes can leave orphans behind:

  1. OR create_sub_key succeeded but KV write failed — orphan OR sub-key
     with no Tenant row referencing its hash.
  2. KV write succeeded but Tenant.save failed — orphan KV secret with
     no Tenant row referencing it.

This sweeper enumerates OR keys via the management API, cross-
references each against the Tenant table (matching on
``openrouter_key_hash``), and DELETEs any OR-side key whose hash is
absent from the Tenant table AND whose created_at is older than the
``--min-age-hours`` threshold (default 24h). The age gate prevents the
sweeper from racing a tenant provision in flight.

SAFETY: ``list_sub_keys`` returns EVERY key on the OpenRouter account,
including platform keys no Tenant row references (2026-11-25 prod:
``nbhd-openrouter-platform-key``, ``nbhd-functionality`` and two
others). Only keys whose ``name`` is exactly ``tenant-<8 hex>`` — the
label ``create_sub_key`` is always called with — are ever candidates.

KV orphan cleanup isn't included here — the Azure SDK paths for
listing/deleting secrets by prefix vary by Vault SKU, and the safer
move is operator-driven via ``az keyvault secret list``. Reach out
to MJ if you need that step automated.

Usage:

    python manage.py sweep_orphan_openrouter_keys              # 24h age threshold
    python manage.py sweep_orphan_openrouter_keys --min-age-hours 1
    python manage.py sweep_orphan_openrouter_keys --dry-run    # report only
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _parse_or_timestamp(value) -> datetime | None:
    """OR returns timestamps as ISO 8601 strings; some endpoints use Z, some +00:00."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        # Python 3.11+ handles "Z" but be defensive.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


_TENANT_KEY_NAME = re.compile(r"^tenant-[0-9a-f]{8}$")


def is_tenant_sub_key(entry: dict) -> bool:
    """True only for per-tenant sub-keys minted by ``create_sub_key``."""
    return bool(_TENANT_KEY_NAME.match(str(entry.get("name") or "")))


def sweep_orphan_keys(*, dry_run: bool, min_age_hours: int = 24, stdout=None) -> dict:
    """Delete (or, with ``dry_run``, report) orphan per-tenant OR sub-keys."""
    from apps.billing.openrouter_admin import OpenRouterAdminError, delete_sub_key, list_sub_keys

    def write(line: str) -> None:
        if stdout is not None:
            stdout.write(line)

    cutoff = datetime.now(UTC) - timedelta(hours=min_age_hours)
    all_keys = list_sub_keys(include_disabled=True)
    known_hashes = set(Tenant.objects.exclude(openrouter_key_hash="").values_list("openrouter_key_hash", flat=True))

    totals = {
        "total_keys": len(all_keys),
        "not_tenant_keys": 0,
        "orphans": 0,
        "deleted": 0,
        "skipped_young": 0,
        "failed": 0,
        "dry_run": dry_run,
    }
    for entry in all_keys:
        if not isinstance(entry, dict):
            continue
        key_hash = (entry.get("hash") or "").strip()
        if not key_hash or key_hash in known_hashes:
            continue
        name = entry.get("name") or "?"
        if not is_tenant_sub_key(entry):
            totals["not_tenant_keys"] += 1
            continue
        created = _parse_or_timestamp(entry.get("created_at"))
        totals["orphans"] += 1

        if created is None or created > cutoff:
            totals["skipped_young"] += 1
            write(f"[skip-young] hash={key_hash[:12]}… name={name!r} created={created}")
            continue

        if dry_run:
            write(f"[DRY-RUN] would delete orphan hash={key_hash[:12]}… name={name!r} created={created}")
            totals["deleted"] += 1
            continue

        try:
            delete_sub_key(key_hash)
            totals["deleted"] += 1
            write(f"[deleted] hash={key_hash[:12]}… name={name!r}")
        except OpenRouterAdminError as exc:
            totals["failed"] += 1
            write(f"[fail] hash={key_hash[:12]}… {exc}")

    logger.info("sweep_orphan_openrouter_keys: %s", totals)
    return totals


class Command(BaseCommand):
    help = "Reap per-tenant OpenRouter sub-keys that have no matching Tenant row."

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-age-hours",
            type=int,
            default=24,
            help="Don't delete keys younger than this (protects in-flight provisioning). Default 24h.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without calling DELETE.",
        )

    def handle(self, *args, **options):
        from apps.billing.openrouter_admin import OpenRouterAdminError

        try:
            totals = sweep_orphan_keys(
                dry_run=options["dry_run"],
                min_age_hours=options["min_age_hours"],
                stdout=self.stdout,
            )
        except OpenRouterAdminError as exc:
            raise CommandError(f"list_sub_keys failed: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Done: total_keys={total_keys} not_tenant_keys={not_tenant_keys} orphans={orphans} "
                "deleted={deleted} skipped_young={skipped_young} failed={failed}".format(**totals)
            )
        )
