"""Durable topic holds in placeholder space; tenant row locks serialize writers."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.common.tenant_tz import tenant_today
from apps.router.chat_gates import _tenant_allowed
from apps.tenants.models import AttentionHold, Tenant


def attention_holds_enabled(tenant):
    return _tenant_allowed(tenant, "ATTENTION_HOLD_TENANT_IDS")


def active_holds(tenant):
    """Expire at the start of resume_on in the person's timezone, even after sleep."""
    rows = AttentionHold.objects.filter(tenant=tenant, released_at__isnull=True)
    rows.filter(resume_on__lte=tenant_today(tenant)).update(
        released_at=timezone.now(), release_reason=AttentionHold.ReleaseReason.EXPIRED
    )
    return list(rows.order_by("id"))


@transaction.atomic
def hold_topic(tenant, topic, resume_on=None):
    if not isinstance(topic, str) or not topic.strip() or len(topic) > 200 or any(c in topic for c in "\n\r\x00"):
        raise ValidationError("Please give a short topic on one line (up to 200 characters).")
    if resume_on is not None and resume_on <= tenant_today(tenant):
        raise ValidationError("pick a day after today, or no date")
    Tenant.objects.select_for_update().get(pk=tenant.pk)
    rows = active_holds(tenant)
    # Compare in Python: preserve the exact authored topic, including placeholders
    # and Unicode case folding. No predicates on a content column.
    row = next((h for h in rows if h.topic.casefold() == topic.casefold()), None)
    if row is None:
        if len(rows) >= 20:
            raise ValidationError("There are already 20 active holds. Lift one before adding another.")
        row = AttentionHold(tenant=tenant)
    row.topic = topic
    row.resume_on = resume_on
    row.save()
    return row


@transaction.atomic
def release_hold(tenant, hold_id):
    Tenant.objects.select_for_update().get(pk=tenant.pk)
    active_holds(tenant)
    row = AttentionHold.objects.get(tenant=tenant, pk=hold_id)
    if row.released_at is None:
        row.released_at = timezone.now()
        row.release_reason = AttentionHold.ReleaseReason.USER
        row.save(update_fields=["released_at", "release_reason"])
    return row
