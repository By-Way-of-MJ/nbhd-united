"""Replay App Store Server Notifications we missed (an outage, or before the endpoint
was configured) from Apple's Notification History. Safe to re-run: every notification
is idempotent on its notificationUUID, and each only triggers a status refresh.

    python manage.py backfill_apple_notifications --days 7 [--environment Sandbox] [--dry-run]
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.billing import apple_iap


class Command(BaseCommand):
    help = "Replay App Store Server Notifications from Apple's Notification History."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="How far back (Apple keeps 180 days).")
        parser.add_argument(
            "--environment", choices=[apple_iap.PRODUCTION, apple_iap.SANDBOX], default=apple_iap.PRODUCTION
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not apple_iap.is_configured():
            raise CommandError("App Store in-app purchase is not configured.")
        from appstoreserverlibrary.models.NotificationHistoryRequest import NotificationHistoryRequest

        from apps.tenants.middleware import set_rls_context

        set_rls_context(service_role=True)
        end = timezone.now()
        start = end - timedelta(days=options["days"])
        request = NotificationHistoryRequest(
            startDate=int(start.timestamp() * 1000), endDate=int(end.timestamp() * 1000)
        )
        client = apple_iap._client(options["environment"])
        token, seen, outcomes = None, 0, {}
        while True:
            page = client.get_notification_history(token, request)
            for item in page.notificationHistory or []:
                seen += 1
                if options["dry_run"] or not item.signedPayload:
                    continue
                try:
                    outcome = apple_iap.handle_notification(item.signedPayload)
                except apple_iap.AppleIAPError as exc:
                    outcome = f"error:{type(exc).__name__}"
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if not page.hasMore:
                break
            token = page.paginationToken
        self.stdout.write(f"{seen} notifications from {options['environment']}; outcomes: {outcomes or 'dry run'}")
