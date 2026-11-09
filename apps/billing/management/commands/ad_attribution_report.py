"""Report Apple Ads attribution by campaign / ad group / keyword.

    python manage.py ad_attribution_report [--days N]

One line per (campaign_id, ad_group_id, keyword_id) shows how many accounts
that keyword brought in and how many of them are paying now. Rows that are not
attributed are totalled separately, so "no campaign" is never mistaken for a
campaign with zero accounts.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing.entitlement import is_paying
from apps.billing.models import AdAttribution


class Command(BaseCommand):
    help = "Report Apple Ads attribution counts (accounts and paying accounts)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Only include rows created in the last N days (default: 30; 0 = all).",
        )

    def handle(self, *args, **options):
        from apps.tenants.middleware import set_rls_context

        set_rls_context(service_role=True)
        rows = AdAttribution.objects.select_related("tenant")
        days = options["days"]
        if days > 0:
            rows = rows.filter(created_at__gte=timezone.now() - timedelta(days=days))

        groups: dict[tuple, dict[str, int]] = defaultdict(lambda: {"accounts": 0, "paying_now": 0})
        totals = {
            AdAttribution.Status.ORGANIC: 0,
            AdAttribution.Status.PENDING: 0,
            "error_expired": 0,
            "error_token_invalid": 0,
        }
        for row in rows:
            if row.status == AdAttribution.Status.ATTRIBUTED:
                key = (row.campaign_id, row.ad_group_id, row.keyword_id)
                groups[key]["accounts"] += 1
                if is_paying(row.tenant):
                    groups[key]["paying_now"] += 1
            elif row.status == AdAttribution.Status.ERROR:
                reason = row.raw.get("reason") if isinstance(row.raw, dict) else None
                if reason == "expired":
                    totals["error_expired"] += 1
                else:
                    totals["error_token_invalid"] += 1
            elif row.status in totals:
                totals[row.status] += 1

        def sort_key(key: tuple) -> tuple:
            return tuple((value is None, value if value is not None else -1) for value in key)

        for key in sorted(groups, key=sort_key):
            campaign_id, ad_group_id, keyword_id = key
            counts = groups[key]
            self.stdout.write(
                f"campaign_id={campaign_id} ad_group_id={ad_group_id} keyword_id={keyword_id} "
                f"accounts={counts['accounts']} paying_now={counts['paying_now']}"
            )

        self.stdout.write(
            f"totals organic={totals[AdAttribution.Status.ORGANIC]} "
            f"pending={totals[AdAttribution.Status.PENDING]} "
            f"error_expired={totals['error_expired']} "
            f"error_token_invalid={totals['error_token_invalid']}"
        )
