"""One door for operator (MJ) alerts: Pushover first, email as the fallback.

Pushover was the only channel, and its trial lapsed. That made every health
alert ``undeliverable`` with nothing reaching MJ. A single alert channel can
rot unnoticed (the gateway route did too, 2026-09). So every operator alert
now tries Pushover, and if that doesn't deliver, sends a plain email to
``PLATFORM_OWNER_EMAIL`` through the platform's Mailgun SMTP.

The return value keeps the Pushover caller's vocabulary
(``delivered | undeliverable | timeout | transient``), so existing cooldown
logic is unchanged:
- either channel delivers → ``delivered``;
- neither delivers → Pushover's ``timeout`` / ``transient`` as-is, and
  ``undeliverable`` becomes ``transient``, so the caller retries soon.

Callers pass content-free text: counts, feature names, tenant display names
as today. Never message content.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def _send_email(subject: str, message: str) -> bool:
    to = str(getattr(settings, "PLATFORM_OWNER_EMAIL", "") or "").strip()
    if not to:
        logger.warning("operator_alert email skipped: PLATFORM_OWNER_EMAIL not set")
        return False
    try:
        send_mail(subject, message, None, [to], fail_silently=False)
        return True
    except Exception as exc:
        logger.warning("operator_alert email failed reason=%s", type(exc).__name__)
        return False


def send_operator_alert(message: str, *, subject: str = "NBHD Health Alert") -> str:
    """Deliver ``message`` to MJ. Never raises."""
    from apps.cron.views import _send_alert_via_pushover

    try:
        status = _send_alert_via_pushover(message)
    except Exception as exc:
        logger.warning("operator_alert pushover raised reason=%s", type(exc).__name__)
        status = "transient"
    if status == "delivered":
        return status
    if _send_email(subject, message):
        logger.info("operator_alert delivered by email (pushover status=%s)", status)
        return "delivered"
    # Nothing reached MJ. Keep Pushover's "timeout" (short backoff) and
    # "transient" (retry next tick), but never report "undeliverable": a lapsed
    # Pushover account must not buy a long "retrying won't help" cooldown while
    # the email fallback is what actually failed.
    return status if status in ("timeout", "transient") else "transient"


def send_test_operator_alert_task() -> dict:
    """QStash entry point (``TASK_MAP['send_test_operator_alert']``).

    Proves the alert path end to end (Pushover, then email) without the
    recipient address ever leaving the server: the address comes from
    ``PLATFORM_OWNER_EMAIL`` here, never from the request body.
    """
    status = send_operator_alert(
        "NBHD test alert: if you can read this, operator alerts reach you.",
        subject="NBHD test alert",
    )
    logger.info("operator_alert test status=%s", status)
    return {"status": status}
