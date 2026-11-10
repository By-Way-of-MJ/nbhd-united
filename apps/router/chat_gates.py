"""Independent Living Chat / Talk rollout gates.

Each setting is a comma list of tenant UUIDs. Fail-closed: empty or unset means
nobody. A lone ``*`` token (exact, after trimming) means every tenant — anything
else that merely contains ``*`` is ignored. ``CHAT_PANELS_TOOL_TENANT_IDS=*`` is
only safe once EVERY running tenant image includes the #1638 plugin manifest.
"""

import logging
import re
from uuid import UUID

from django.conf import settings

from apps.billing.entitlement import ai_paused

logger = logging.getLogger(__name__)


def _tenant_allowed(tenant, setting: str) -> bool:
    raw = str(getattr(settings, setting, "") or "")
    tokens = {part.strip() for part in raw.split(",") if part.strip()}
    try:
        tenant_id = UUID(str(tenant.id)) if tenant is not None else None
    except (AttributeError, ValueError):
        return False
    if tenant_id is None:
        return False
    if "*" in tokens:
        return True
    allowed = set()
    for token in tokens:
        try:
            allowed.add(UUID(token))
        except ValueError:
            continue
    return tenant_id in allowed


def chat_shape_enabled(tenant) -> bool:
    """Image-independent shape endpoint, chat instruction and fenced panels."""
    return _tenant_allowed(tenant, "CHAT_SHAPE_TENANT_IDS")


def chat_panels_tool_enabled(tenant) -> bool:
    """Tool panels: allowlist only after verifying the running plugin manifest."""
    return _tenant_allowed(tenant, "CHAT_PANELS_TOOL_TENANT_IDS")


def talk_route_enabled(tenant) -> bool:
    """Talk-mode ack/quick-read router (Django-only; image-independent)."""
    return _tenant_allowed(tenant, "TALK_ROUTE_TENANT_IDS")


def briefing_weather_tool_enabled(tenant) -> bool:
    """Morning Briefing weather via the ``nbhd_weather_briefing`` plugin tool.

    Only safe for a tenant whose RUNNING OpenClaw image ships the tool — the
    prompt refresh is fleet-wide but the image rolls per tenant. Ungated
    tenants keep the web_search weather step.
    """
    return _tenant_allowed(tenant, "BRIEFING_WEATHER_TOOL_TENANT_IDS")


def brave_llm_context_enabled(tenant) -> bool:
    """``web_search`` returns page excerpts (Brave "LLM Context") instead of one-line
    descriptions. A config key on the image's vendored brave plugin, so it only
    takes effect inside the 9.4 config shape (see ``generate_openclaw_config``).
    """
    return _tenant_allowed(tenant, "BRAVE_LLM_CONTEXT_TENANT_IDS")


def web_redesign_enabled(tenant) -> bool:
    """Open Sky web console (read by the frontend via /tenants/me/)."""
    return _tenant_allowed(tenant, "WEB_REDESIGN_TENANT_IDS")


def keep_chatting_enabled(tenant) -> bool:
    """Allow concurrent app conversation while a longer job runs."""
    return _tenant_allowed(tenant, "KEEP_CHATTING_TENANT_IDS")


# ---------------------------------------------------------------------------
# iOS paywall contract (Phase D free tier)
# ---------------------------------------------------------------------------

# ``X-NBHD-Client: ios/<version>/<build>; paywall=<n>``. Every shipped build since
# 2.2.2 already sends ``paywall=1`` while its plan screen is compiled out of Release,
# so refusing on ``1`` would strand those users. Only builds that carry the free-tier
# flow send ``2``; the server refuses the assistant from that contract upward.
IOS_PAYWALL_REFUSE_FROM = 2
_IOS_CLIENT_HEADER_MAX = 256
_PAYWALL_VALUE = re.compile(r"[0-9]{1,6}")


def ios_paywall_version(request) -> int:
    """The paywall contract an iOS client declares, or 0. Never raises.

    Requires the ``ios/`` prefix; reads the first ``paywall=<n>`` among the
    ``;``-separated params. Missing, non-iOS, malformed, non-ASCII-digit or absurdly
    large values all read as 0, i.e. "an old client": served exactly as today.
    """
    try:
        raw = request.META.get("HTTP_X_NBHD_CLIENT", "")
        if not isinstance(raw, str) or len(raw) > _IOS_CLIENT_HEADER_MAX:
            return 0
        product, *params = raw.split(";")
        if not product.strip().startswith("ios/"):
            return 0
        for param in params:
            key, sep, value = param.partition("=")
            if not sep or key.strip().lower() != "paywall":
                continue
            value = value.strip()
            return int(value) if _PAYWALL_VALUE.fullmatch(value) else 0
    except Exception:  # noqa: BLE001 — a header must never break a request
        return 0
    return 0


def ios_paywall_refuses(request, tenant, *, endpoint: str | None = None) -> bool:
    """True when this iOS request must be refused the server assistant.

    All three must hold: the tenant is opened by ``IOS_PAYWALL_ENFORCE_TENANT_IDS``
    (dark by default), the client declares paywall contract >= 2, and the tenant is
    ``ai_paused`` (the Telegram/LINE paused predicate, budget-exempt excluded).

    ``endpoint`` marks an AI turn endpoint: a turn from a paused tenant is then logged
    (ids and booleans only, never message text) whether or not it is refused, so
    adoption of the new client can be measured before and after the flag flips.
    """
    if tenant is None:
        return False
    if endpoint is None:
        return (
            _tenant_allowed(tenant, "IOS_PAYWALL_ENFORCE_TENANT_IDS")
            and ios_paywall_version(request) >= IOS_PAYWALL_REFUSE_FROM
            and ai_paused(tenant)
        )
    if not ai_paused(tenant):
        return False
    version = ios_paywall_version(request)
    flag_allowed = _tenant_allowed(tenant, "IOS_PAYWALL_ENFORCE_TENANT_IDS")
    refused = flag_allowed and version >= IOS_PAYWALL_REFUSE_FROM
    logger.info(
        "ios_paywall_turn tenant_id=%s endpoint=%s paywall=%d flag_allowed=%s refused=%s",
        tenant.id,
        endpoint,
        version,
        flag_allowed,
        refused,
        extra={
            "tenant_id": str(tenant.id),
            "endpoint": endpoint,
            "paywall": version,
            "flag_allowed": flag_allowed,
            "refused": refused,
        },
    )
    return refused
