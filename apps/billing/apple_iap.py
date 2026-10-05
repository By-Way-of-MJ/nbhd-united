"""Apple in-app subscriptions (DIRECTIVE_ios_in_app_purchase.md §2).

Apple's current status is the source of truth. A verified App Store Server
Notification, or the app posting a transaction after a purchase/restore, only
TRIGGERS ``refresh()``: we ask the App Store Server API for every status of that
subscription and set ours from Apple's answer. Late, duplicate or out-of-order
messages therefore can't flip anyone the wrong way.

Environments: production verifies BOTH Production and Sandbox payloads (App Review
buys in Sandbox against the production backend; TestFlight purchases are Sandbox and
free). A Sandbox purchase entitles only users on ``APPLE_IAP_SANDBOX_USER_IDS``.

Nothing here ever raises into a caller's transaction for an Apple-side problem:
``TransientAppleError`` means "try again later" (the notification endpoint answers
non-200 so Apple retries); ``AppleVerificationError`` means the payload isn't Apple's.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import AppStoreNotification, AppStoreSubscription

logger = logging.getLogger(__name__)

_ROOTS_DIR = Path(__file__).with_name("apple_roots")
_STATUS_BY_CODE = {
    1: AppStoreSubscription.Status.ACTIVE,
    2: AppStoreSubscription.Status.EXPIRED,
    3: AppStoreSubscription.Status.BILLING_RETRY,
    4: AppStoreSubscription.Status.GRACE,
    5: AppStoreSubscription.Status.REVOKED,
}
_ENTITLING = {AppStoreSubscription.Status.ACTIVE, AppStoreSubscription.Status.GRACE}
PRODUCTION = "Production"
# How long a last-known paying state is trusted while Apple can't be reached
# (matches the 16-day billing grace period).
_GRACE_FALLBACK = timedelta(days=16)
SANDBOX = "Sandbox"


class AppleIAPError(Exception):
    pass


class AppleNotConfigured(AppleIAPError):
    pass


class AppleVerificationError(AppleIAPError):
    pass


class TransientAppleError(AppleIAPError):
    pass


class AccountMismatch(AppleIAPError):
    """The Apple purchase is bound to a different NBHD account."""


# ── Configuration ────────────────────────────────────────────────────────────


def is_configured() -> bool:
    return bool(
        getattr(settings, "APPLE_IAP_ISSUER_ID", "")
        and getattr(settings, "APPLE_IAP_KEY_ID", "")
        and getattr(settings, "APPLE_IAP_PRIVATE_KEY", "")
        and getattr(settings, "APPLE_IAP_BUNDLE_ID", "")
    )


def _require_configured() -> None:
    if not is_configured():
        raise AppleNotConfigured("App Store in-app purchase is not configured")


def _env_enum(name: str):
    from appstoreserverlibrary.models.Environment import Environment

    return Environment.PRODUCTION if name == PRODUCTION else Environment.SANDBOX


@lru_cache(maxsize=1)
def _root_certificates() -> tuple[bytes, ...]:
    return tuple(p.read_bytes() for p in sorted(_ROOTS_DIR.glob("*.cer")))


@lru_cache(maxsize=4)
def _verifier(env_name: str):
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

    return SignedDataVerifier(
        list(_root_certificates()),
        True,  # online revocation checks (OCSP) on the leaf chain
        _env_enum(env_name),
        settings.APPLE_IAP_BUNDLE_ID,
        int(settings.APPLE_IAP_APP_APPLE_ID) if env_name == PRODUCTION else None,
    )


@lru_cache(maxsize=4)
def _client(env_name: str):
    from appstoreserverlibrary.api_client import AppStoreServerAPIClient

    return AppStoreServerAPIClient(
        settings.APPLE_IAP_PRIVATE_KEY.encode(),
        settings.APPLE_IAP_KEY_ID,
        settings.APPLE_IAP_ISSUER_ID,
        settings.APPLE_IAP_BUNDLE_ID,
        _env_enum(env_name),
    )


def _verify(kind: str, signed: str):
    """Decode a signed payload with whichever environment's verifier accepts it.
    Returns ``(decoded, env_name)``."""
    from appstoreserverlibrary.signed_data_verifier import VerificationException

    last: Exception | None = None
    for env_name in (PRODUCTION, SANDBOX):
        verifier = _verifier(env_name)
        try:
            if kind == "notification":
                return verifier.verify_and_decode_notification(signed), env_name
            if kind == "transaction":
                return verifier.verify_and_decode_signed_transaction(signed), env_name
            return verifier.verify_and_decode_renewal_info(signed), env_name
        except VerificationException as exc:
            last = exc
            continue
    raise AppleVerificationError(f"payload failed verification: {last}")


def _ms(value) -> datetime | None:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC) if value else None


def _sandbox_allowed(tenant) -> bool:
    allowed = {str(v).strip().lower() for v in getattr(settings, "APPLE_IAP_SANDBOX_USER_IDS", []) if str(v).strip()}
    return tenant is not None and str(tenant.user_id).lower() in allowed


def _product_ok(product_id: str) -> bool:
    products = [p for p in getattr(settings, "APPLE_IAP_PRODUCT_IDS", []) if p]
    return not products or product_id in products


# ── Binding a purchase to an NBHD account ────────────────────────────────────


def _tenant_for_token(token) -> object | None:
    if not token:
        return None
    from apps.tenants.models import Tenant

    try:
        user_id = uuid.UUID(str(token))
    except ValueError:
        return None
    return Tenant.objects.filter(user_id=user_id).first()


def _tell_apple_the_owner(sub: AppStoreSubscription, tenant) -> None:
    """Set Apple's appAccountToken for this purchase to the owner's user id (so
    renewals and restores carry it). Called after our transaction commits — never
    while holding the row lock. Our binding stands even if Apple's call fails."""
    try:
        from appstoreserverlibrary.models.UpdateAppAccountTokenRequest import UpdateAppAccountTokenRequest

        _client(sub.environment or PRODUCTION).set_app_account_token(
            sub.original_transaction_id, UpdateAppAccountTokenRequest(appAccountToken=str(tenant.user_id))
        )
    except Exception:  # noqa: BLE001
        logger.warning("apple_iap: set_app_account_token failed for %s", sub.original_transaction_id, exc_info=True)


# Apple says retry: rate limit, server errors, and these API error codes
# (AccountNotFoundRetryable, AppNotFoundRetryable, OriginalTransactionIdNotFoundRetryable,
# GeneralInternalRetryable). 401/403 mean OUR key is wrong — also retry (Apple keeps
# re-sending) so a key problem never silently drops EXPIRED/REFUND notifications.
_RETRYABLE_API_ERRORS = {4040002, 4040004, 4040006, 5000001}


def _api_error_is_transient(exc) -> bool:
    code = exc.http_status_code or 0
    return code >= 500 or code in {401, 403, 429} or (exc.raw_api_error or 0) in _RETRYABLE_API_ERRORS


# ── The refresh: Apple's status → ours → the tenant ──────────────────────────


def refresh(original_transaction_id: str, env_name: str, *, bind_to=None) -> AppStoreSubscription:
    """Ask Apple for this subscription's current state and apply it.

    ``bind_to``: a tenant the caller has proven owns the purchase (the signed-in app
    user). The purchase is always recorded; if it belongs to a DIFFERENT live account
    ``AccountMismatch`` is raised after recording, and nothing is bound.
    """
    _require_configured()
    from appstoreserverlibrary.api_client import APIException

    try:
        response = _client(env_name).get_all_subscription_statuses(original_transaction_id)
    except APIException as exc:
        if _api_error_is_transient(exc):
            raise TransientAppleError(str(exc)) from exc
        raise AppleIAPError(f"App Store Server API refused: {exc.http_status_code} {exc.raw_api_error}") from exc
    except Exception as exc:  # network
        raise TransientAppleError(str(exc)) from exc

    item = None
    for group in response.data or []:
        for last in group.lastTransactions or []:
            if last.originalTransactionId == original_transaction_id:
                item = last
    if item is None:
        raise AppleIAPError(f"Apple returned no status for {original_transaction_id}")

    try:
        txn, _ = _verify("transaction", item.signedTransactionInfo)
        renewal = _verify("renewal", item.signedRenewalInfo)[0] if item.signedRenewalInfo else None
    except AppleVerificationError as exc:
        # Apple's own API answer failing verification is an outage (e.g. OCSP), not forgery.
        raise TransientAppleError(str(exc)) from exc
    status = _STATUS_BY_CODE.get(int(item.rawStatus or 0), AppStoreSubscription.Status.UNKNOWN)
    signed = _ms(txn.signedDate)
    mismatch = False
    tell_apple = None

    with transaction.atomic():
        sub, _created = AppStoreSubscription.objects.select_for_update().get_or_create(
            original_transaction_id=original_transaction_id,
            defaults={"environment": env_name},
        )
        if signed and sub.signed_date and signed < sub.signed_date:
            # An older answer that lost the race to a newer one: change nothing.
            stale = True
        else:
            stale = False
            token = txn.appAccountToken or ""
            if token and not sub.app_account_token:
                sub.app_account_token = uuid.UUID(token)
            token_owner = _tenant_for_token(sub.app_account_token) if sub.app_account_token else None

            if bind_to is not None:
                owner_id = sub.tenant_id or (token_owner.pk if token_owner else None)
                if owner_id and str(owner_id) != str(bind_to.pk):
                    mismatch = True  # recorded below, never bound to the caller
                elif sub.tenant_id is None:
                    # Unbound: a purchase without a token, or one whose token names an
                    # account that was deleted (re-signed up, tapped Restore).
                    sub.tenant = bind_to
                    sub.bound_at = timezone.now()
                    if str(sub.app_account_token or "") != str(bind_to.user_id):
                        sub.app_account_token = bind_to.user_id
                        tell_apple = bind_to
            elif sub.tenant_id is None and token_owner is not None:
                sub.tenant = token_owner
                sub.bound_at = timezone.now()

            sub.environment = env_name
            sub.product_id = txn.productId or sub.product_id
            sub.status = status
            sub.expires_at = _ms(txn.expiresDate)
            sub.auto_renew = (
                (int(renewal.rawAutoRenewStatus) == 1) if renewal and renewal.rawAutoRenewStatus is not None else None
            )
            if signed:
                sub.signed_date = signed
            sub.entitles = bool(
                sub.tenant_id
                and status in _ENTITLING
                and _product_ok(sub.product_id)
                and (env_name == PRODUCTION or _sandbox_allowed(sub.tenant))
            )
            sub.last_status_payload = {"status": int(item.rawStatus or 0), "refreshed_at": timezone.now().isoformat()}
            sub.save()

    if tell_apple is not None:
        _tell_apple_the_owner(sub, tell_apple)
    if mismatch:
        raise AccountMismatch("This Apple ID's subscription belongs to another NBHD account.")
    if not stale:
        _apply_to_tenant(sub)
    return sub


def _apply_to_tenant(sub: AppStoreSubscription) -> None:
    """Make the tenant match the subscription's CURRENT state (not a transition), so
    a failure part-way is simply redone by the next refresh."""
    from apps.tenants.models import Tenant

    from . import services

    if sub.tenant_id is None:
        return
    tenant = Tenant.objects.filter(pk=sub.tenant_id).first()
    if tenant is None:
        return
    if sub.entitles:
        if tenant.pending_deletion:
            # They scheduled deletion (a Stripe payer) and then paid on the iPhone:
            # keep the account — Apple is billing them.
            tenant.pending_deletion = False
            tenant.deletion_scheduled_at = None
            tenant.save(update_fields=["pending_deletion", "deletion_scheduled_at", "updated_at"])
            logger.warning("apple_iap: cleared scheduled deletion for %s (now pays through the App Store)", tenant.id)
        running = tenant.status == Tenant.Status.ACTIVE and bool(tenant.container_id)
        if not running and tenant.status != Tenant.Status.PROVISIONING:
            try:
                outcome = services.activate_paid_tenant(tenant)
            except services.ActivationDeferred as exc:
                raise TransientAppleError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 — e.g. QStash down: retry later
                raise TransientAppleError(f"activation failed: {exc}") from exc
            logger.info("apple_iap: %s pays through the App Store (%s)", tenant.id, outcome)
        _resolve_double_billing(tenant)
    elif tenant.status == Tenant.Status.ACTIVE:
        if services.pause_tenant_for_billing(tenant):
            logger.info("apple_iap: paused %s — App Store subscription no longer entitles", tenant.id)


def _stripe():
    import stripe

    stripe.api_key = (
        settings.STRIPE_LIVE_SECRET_KEY
        if getattr(settings, "STRIPE_LIVE_MODE", False)
        else settings.STRIPE_TEST_SECRET_KEY
    )
    return stripe


_STRIPE_ENDED = {"canceled", "incomplete_expired"}


def _resolve_double_billing(tenant) -> None:
    """Apple has charged; if a Stripe subscription is still live, end it at period
    end and tell MJ. A Stripe id that is already dead is just cleared. Never
    refuses or reverses the Apple purchase — that money is already taken."""
    from . import services

    if not tenant.stripe_subscription_id:
        return
    stripe = _stripe()
    try:
        current = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
        state = current.get("status") if hasattr(current, "get") else getattr(current, "status", "")
        already_ending = bool(
            current.get("cancel_at_period_end")
            if hasattr(current, "get")
            else getattr(current, "cancel_at_period_end", False)
        )
    except Exception as exc:  # noqa: BLE001
        from apps.billing.views import _is_missing_subscription_error

        if _is_missing_subscription_error(exc):
            services._end_stripe_subscription(tenant)
            return
        logger.warning("apple_iap: could not read Stripe subscription for %s", tenant.id, exc_info=True)
        state, already_ending = "", False
    if state in _STRIPE_ENDED:
        services._end_stripe_subscription(tenant)
        return
    if already_ending:
        return
    message = (
        f"NBHD double billing: tenant {str(tenant.id)[:8]} pays in the App Store while a Stripe "
        "subscription is live — setting Stripe to cancel at period end."
    )
    try:
        stripe.Subscription.modify(tenant.stripe_subscription_id, cancel_at_period_end=True)
    except Exception:  # noqa: BLE001
        logger.exception("apple_iap: could not set Stripe cancel_at_period_end for %s", tenant.id)
        message += " (Stripe update FAILED — fix by hand.)"
    _alert(message)


def _alert(message: str) -> None:
    logger.warning(message)
    try:
        from apps.cron.views import _send_alert_via_pushover

        _send_alert_via_pushover(message)
    except Exception:  # noqa: BLE001
        logger.warning("apple_iap: alert not sent", exc_info=True)


# ── Entry points ─────────────────────────────────────────────────────────────


def handle_notification(signed_payload: str) -> str:
    """Verify, record (idempotent on notificationUUID) and act on one App Store
    Server Notification. Returns a short outcome label."""
    _require_configured()
    payload, env_name = _verify("notification", signed_payload)
    notification_uuid = payload.notificationUUID or ""
    kind = payload.rawNotificationType or ""
    subtype = payload.rawSubtype or ""
    original_id = ""
    if payload.data and payload.data.signedTransactionInfo:
        txn, _ = _verify("transaction", payload.data.signedTransactionInfo)
        original_id = txn.originalTransactionId or ""

    record, created = AppStoreNotification.objects.get_or_create(
        notification_uuid=notification_uuid,
        defaults={
            "notification_type": kind,
            "subtype": subtype,
            "environment": env_name,
            "original_transaction_id": original_id,
            "signed_date": _ms(payload.signedDate),
        },
    )
    if not created and record.outcome and record.outcome != "retry":
        return "duplicate"
    if kind == "TEST" or not original_id:
        outcome = "test" if kind == "TEST" else "no_transaction"
    else:
        try:
            refresh(original_id, env_name)
            outcome = "applied"
        except TransientAppleError:
            record.outcome = "retry"
            record.save(update_fields=["outcome"])
            raise
        except AppleIAPError as exc:
            logger.warning("apple_iap: notification %s (%s) not applied: %s", notification_uuid, kind, exc)
            outcome = "not_applied"
    record.outcome = outcome
    record.save(update_fields=["outcome"])
    logger.info("apple_iap: notification %s %s/%s → %s", notification_uuid, kind, subtype, outcome)
    return outcome


def sync_from_app(tenant, signed_transaction: str) -> AppStoreSubscription:
    """The signed-in app posts a transaction right after a purchase or restore."""
    _require_configured()
    txn, env_name = _verify("transaction", signed_transaction)
    if not txn.originalTransactionId:
        raise AppleVerificationError("transaction has no originalTransactionId")
    return refresh(txn.originalTransactionId, env_name, bind_to=tenant)


def still_paying_on_apple(tenant) -> bool:
    """Before the daily sweep pauses anyone with an App Store subscription, ask Apple
    for its current state (a renewal notification may simply be late). If Apple
    can't be reached, don't pause someone whose last known state was paying and
    whose period ended less than a grace period ago."""
    subs = list(AppStoreSubscription.objects.filter(tenant_id=tenant.pk))
    if not subs:
        return False
    if not is_configured():
        return any(s.entitles for s in subs)
    recent = timezone.now() - _GRACE_FALLBACK
    for sub in subs:
        try:
            refreshed = refresh(sub.original_transaction_id, sub.environment or PRODUCTION)
        except TransientAppleError:
            if sub.status in {*_ENTITLING, AppStoreSubscription.Status.BILLING_RETRY} and (
                sub.expires_at is None or sub.expires_at > recent
            ):
                return True
            continue
        except AppleIAPError:
            continue
        if refreshed.entitles:
            return True
    return False


def recheck_lapsed(limit: int = 200) -> dict:
    """Daily: re-ask Apple about subscriptions we still count as paying although
    their period ended over a day ago (a lost EXPIRED/REFUND notification must not
    mean free service forever)."""
    if not is_configured():
        return {"checked": 0}
    cutoff = timezone.now() - timedelta(days=1)
    checked = errors = 0
    for sub in AppStoreSubscription.objects.filter(entitles=True, expires_at__lt=cutoff)[:limit]:
        checked += 1
        try:
            refresh(sub.original_transaction_id, sub.environment or PRODUCTION)
        except AppleIAPError:
            errors += 1
            logger.warning("apple_iap: recheck of %s failed", sub.original_transaction_id, exc_info=True)
    return {"checked": checked, "errors": errors}


def subscription_summary(tenant) -> dict:
    """What the app shows: who bills, whether it's live, renewal and trial dates."""
    from .entitlement import billing_source

    source = billing_source(tenant)
    apple = (
        AppStoreSubscription.objects.filter(tenant_id=tenant.pk)
        .order_by("-entitles", F("expires_at").desc(nulls_last=True))
        .first()
        if tenant is not None
        else None
    )
    trial_ends = tenant.trial_ends_at if (tenant is not None and tenant.is_trial) else None
    return {
        "active": bool(tenant is not None and tenant.has_entitlement),
        "source": source,
        "status": apple.status if (apple and source == "apple") else ("active" if source == "stripe" else ""),
        "renews_at": apple.expires_at.isoformat() if (apple and source == "apple" and apple.expires_at) else None,
        "auto_renew": apple.auto_renew if (apple and source == "apple") else None,
        "trial_ends_at": trial_ends.isoformat() if trial_ends else None,
    }
