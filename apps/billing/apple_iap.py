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
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.db import transaction
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


def _bind(sub: AppStoreSubscription, tenant, *, set_token_at_apple: bool) -> None:
    sub.tenant = tenant
    sub.bound_at = timezone.now()
    if set_token_at_apple and not sub.app_account_token:
        sub.app_account_token = tenant.user_id
        try:
            from appstoreserverlibrary.models.UpdateAppAccountTokenRequest import UpdateAppAccountTokenRequest

            _client(sub.environment or PRODUCTION).set_app_account_token(
                sub.original_transaction_id, UpdateAppAccountTokenRequest(appAccountToken=str(tenant.user_id))
            )
        except Exception:  # noqa: BLE001 — binding here stands; Apple's copy is a convenience
            logger.warning("apple_iap: set_app_account_token failed for %s", sub.original_transaction_id, exc_info=True)


# ── The refresh: Apple's status → ours → the tenant ──────────────────────────


def refresh(original_transaction_id: str, env_name: str, *, bind_to=None) -> AppStoreSubscription:
    """Ask Apple for this subscription's current state and apply it.

    ``bind_to``: a tenant the caller has proven owns the purchase (the signed-in app
    user). A purchase already bound to a DIFFERENT account raises ``AccountMismatch``.
    """
    _require_configured()
    from appstoreserverlibrary.api_client import APIException

    try:
        response = _client(env_name).get_all_subscription_statuses(original_transaction_id)
    except APIException as exc:
        if exc.http_status_code and exc.http_status_code >= 500 or exc.http_status_code == 429:
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

    txn, _ = _verify("transaction", item.signedTransactionInfo)
    renewal = _verify("renewal", item.signedRenewalInfo)[0] if item.signedRenewalInfo else None
    status = _STATUS_BY_CODE.get(int(item.rawStatus or 0), AppStoreSubscription.Status.UNKNOWN)

    with transaction.atomic():
        sub, _created = AppStoreSubscription.objects.select_for_update().get_or_create(
            original_transaction_id=original_transaction_id,
            defaults={"environment": env_name},
        )
        was_entitling, old_tenant_id = sub.entitles, sub.tenant_id
        token = txn.appAccountToken or ""
        if token and not sub.app_account_token:
            sub.app_account_token = uuid.UUID(token)

        if bind_to is not None:
            owner = sub.tenant_id or (_tenant_for_token(sub.app_account_token).pk if sub.app_account_token else None)
            if owner and str(owner) != str(bind_to.pk):
                raise AccountMismatch("This Apple ID's subscription belongs to another NBHD account.")
            if sub.tenant_id is None:
                _bind(sub, bind_to, set_token_at_apple=True)
        elif sub.tenant_id is None and sub.app_account_token:
            owner = _tenant_for_token(sub.app_account_token)
            if owner is not None:
                _bind(sub, owner, set_token_at_apple=False)

        sub.environment = env_name
        sub.product_id = txn.productId or sub.product_id
        sub.status = status
        sub.expires_at = _ms(txn.expiresDate)
        sub.auto_renew = (
            (int(renewal.rawAutoRenewStatus) == 1) if renewal and renewal.rawAutoRenewStatus is not None else None
        )
        signed = _ms(txn.signedDate)
        if signed and (sub.signed_date is None or signed > sub.signed_date):
            sub.signed_date = signed
        sub.entitles = bool(
            sub.tenant_id
            and status in _ENTITLING
            and _product_ok(sub.product_id)
            and (env_name == PRODUCTION or _sandbox_allowed(sub.tenant))
        )
        sub.last_status_payload = {"status": int(item.rawStatus or 0), "refreshed_at": timezone.now().isoformat()}
        sub.save()

    _apply_to_tenant(sub, was_entitling=was_entitling, old_tenant_id=old_tenant_id)
    return sub


def _apply_to_tenant(sub: AppStoreSubscription, *, was_entitling: bool, old_tenant_id) -> None:
    from . import services

    tenant = sub.tenant
    if tenant is None:
        return
    if sub.entitles and not was_entitling:
        outcome = services.activate_paid_tenant(tenant)
        logger.info("apple_iap: %s now pays through the App Store (%s)", tenant.id, outcome)
        _resolve_double_billing(tenant)
    elif not sub.entitles and was_entitling and str(old_tenant_id) == str(tenant.pk):
        paused = services.pause_tenant_for_billing(tenant)
        logger.info("apple_iap: App Store subscription for %s stopped entitling (paused=%s)", tenant.id, paused)


def _resolve_double_billing(tenant) -> None:
    """Apple has charged; if Stripe also bills, end Stripe at period end and tell MJ.
    Never refuses or reverses the Apple purchase — that money is already taken."""
    if not tenant.stripe_subscription_id:
        return
    message = f"NBHD double billing: tenant {str(tenant.id)[:8]} subscribed in the App Store while a Stripe subscription is live — setting Stripe to cancel at period end."
    try:
        import stripe

        stripe.api_key = (
            settings.STRIPE_LIVE_SECRET_KEY
            if getattr(settings, "STRIPE_LIVE_MODE", False)
            else settings.STRIPE_TEST_SECRET_KEY
        )
        stripe.Subscription.modify(tenant.stripe_subscription_id, cancel_at_period_end=True)
    except Exception:  # noqa: BLE001
        logger.exception("apple_iap: could not set Stripe cancel_at_period_end for %s", tenant.id)
        message += " (Stripe update FAILED — fix by hand.)"
    logger.warning(message)
    try:
        from apps.cron.views import _send_alert_via_pushover

        _send_alert_via_pushover(message)
    except Exception:  # noqa: BLE001
        logger.warning("apple_iap: double-billing alert not sent", exc_info=True)


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
    for its current state (a renewal notification may simply be late). On an Apple
    outage, err towards not pausing someone whose last known state was paying."""
    subs = list(AppStoreSubscription.objects.filter(tenant_id=tenant.pk))
    if not subs:
        return False
    if not is_configured():
        return any(s.entitles for s in subs)
    for sub in subs:
        try:
            refreshed = refresh(sub.original_transaction_id, sub.environment or PRODUCTION)
        except AppleIAPError:
            if sub.status in {*_ENTITLING, AppStoreSubscription.Status.BILLING_RETRY}:
                return True
            continue
        if refreshed.entitles:
            return True
    return False


def subscription_summary(tenant) -> dict:
    """What the app shows: who bills, whether it's live, renewal and trial dates."""
    from .entitlement import billing_source

    source = billing_source(tenant)
    apple = (
        AppStoreSubscription.objects.filter(tenant_id=tenant.pk).order_by("-entitles", "-expires_at").first()
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
