"""The one answer to "is this person paying?" — a Stripe subscription OR an Apple
in-app subscription that currently entitles them.

Every check that used to read ``stripe_subscription_id`` to mean "paying" goes
through here (DIRECTIVE_ios_in_app_purchase.md §2.4). Stripe-specific code (price
lookup, donations, cancel-on-delete) still reads the Stripe fields directly.
"""

from __future__ import annotations

import uuid

from django.db.models import Q

STRIPE = "stripe"
APPLE = "apple"


def has_apple_subscription(tenant) -> bool:
    pk = getattr(tenant, "pk", None)
    if not isinstance(pk, (uuid.UUID, str)):  # unsaved, or a test double
        return False
    from .models import AppStoreSubscription

    return AppStoreSubscription.objects.filter(tenant_id=tenant.pk, entitles=True).exists()


def is_paying(tenant) -> bool:
    """A live Stripe subscription or an entitling Apple subscription."""
    if tenant is None:
        return False
    return bool(getattr(tenant, "stripe_subscription_id", "")) or has_apple_subscription(tenant)


def paying_q(prefix: str = "") -> Q:
    """``is_paying`` as a queryset filter on Tenant (``prefix`` for related lookups,
    e.g. ``"tenant__"``). ``Exists`` rather than a join, so a tenant with several App
    Store rows is never returned more than once."""
    from django.db.models import Exists, OuterRef

    from .models import AppStoreSubscription

    apple = Exists(AppStoreSubscription.objects.filter(tenant_id=OuterRef(f"{prefix}pk"), entitles=True))
    return Q(**{f"{prefix}stripe_subscription_id__gt": ""}) | Q(apple)


def billing_source(tenant) -> str:
    """Who bills this person right now: "apple", "stripe" or "" (nobody). Apple wins
    when both are live — Apple has already charged and can't be cancelled by us."""
    if has_apple_subscription(tenant):
        return APPLE
    if tenant is not None and getattr(tenant, "stripe_subscription_id", ""):
        return STRIPE
    return ""
