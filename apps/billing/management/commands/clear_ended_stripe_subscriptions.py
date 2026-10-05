"""One-off clean-up: paused or deleted accounts that still carry a Stripe
subscription id whose subscription has ended. Until 2026-10 the id was kept after a
cancel or final failed payment, so these read as "paying" (free service through
other channels, and the app would hide Subscribe). Asks Stripe about each one.

    python manage.py clear_ended_stripe_subscriptions            # dry run
    python manage.py clear_ended_stripe_subscriptions --apply
"""

from django.core.management.base import BaseCommand

from apps.billing.apple_iap import _STRIPE_ENDED, _stripe, stripe_field
from apps.billing.services import _end_stripe_subscription
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Clear Stripe subscription ids whose subscription has ended (paused/deleted accounts)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Clear them (default: report only).")

    def handle(self, *args, **options):
        from apps.billing.views import _is_missing_subscription_error
        from apps.tenants.middleware import set_rls_context

        set_rls_context(service_role=True)
        stripe = _stripe()
        candidates = Tenant.objects.filter(
            status__in=[Tenant.Status.SUSPENDED, Tenant.Status.DELETED], stripe_subscription_id__gt=""
        )
        ended = live = unknown = 0
        for tenant in candidates:
            try:
                state = stripe_field(stripe.Subscription.retrieve(tenant.stripe_subscription_id), "status", "")
            except Exception as exc:  # noqa: BLE001
                if _is_missing_subscription_error(exc):
                    state = "missing"
                else:
                    unknown += 1
                    self.stdout.write(f"  ? {str(tenant.id)[:8]}: {type(exc).__name__}")
                    continue
            if state in _STRIPE_ENDED or state == "missing":
                ended += 1
                if options["apply"]:
                    _end_stripe_subscription(tenant)
            else:
                live += 1
                self.stdout.write(f"  live {str(tenant.id)[:8]}: {state}")
        verb = "cleared" if options["apply"] else "would clear"
        self.stdout.write(f"{verb} {ended}; still live {live}; unknown {unknown}")
