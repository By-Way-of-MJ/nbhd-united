"""Retry Apple Ads attribution lookups that Apple had not resolved yet.

Apple answers 404 until an attribution token is ready, so the endpoint resolves
once inline and this command retries while the token is still fresh. Rows older
than 24 h are closed as ``error`` rather than kept pending forever. Scheduling is
intentionally out of scope here.

    python manage.py resolve_ad_attributions
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing.adservices import resolve
from apps.billing.models import AdAttribution

RETRY_WINDOW = timedelta(hours=24)


class Command(BaseCommand):
    help = "Retry pending Apple Ads attribution lookups."

    def handle(self, *args, **options):
        from apps.tenants.middleware import set_rls_context

        set_rls_context(service_role=True)
        cutoff = timezone.now() - RETRY_WINDOW

        expired = AdAttribution.objects.filter(
            status=AdAttribution.Status.PENDING,
            created_at__lt=cutoff,
        ).update(
            status=AdAttribution.Status.ERROR,
            raw={"reason": "expired"},
            token="",
            resolved_at=timezone.now(),
        )

        retryable = (
            AdAttribution.objects.filter(
                status=AdAttribution.Status.PENDING,
                created_at__gte=cutoff,
            )
            .select_related("tenant")
            .order_by("created_at")
        )
        resolved = 0
        still_pending = 0
        errored = 0
        for row in retryable:
            resolve(row)
            if row.status == AdAttribution.Status.PENDING:
                still_pending += 1
            elif row.status == AdAttribution.Status.ERROR:
                errored += 1
            else:
                resolved += 1

        self.stdout.write(f"resolved={resolved} pending={still_pending} error={errored} expired={expired}")
