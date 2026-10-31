"""The sole write path for structured, decaying tenant situation signals."""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Tenant, UserSituation

logger = logging.getLogger(__name__)

OBSERVATION_WRITE_THROTTLE = timedelta(minutes=10)
PLACE_TZ_TIMEOUT_SECONDS = 3.0
_MARKDOWN_SIGNIFICANT = frozenset("#*`[]()>|_~")


@dataclass(frozen=True)
class PlaceTz:
    """What a stated place label resolves to for timezone guidance."""

    kind: Literal["zone", "region", "unknown"]
    tz: str = ""


def _capture_enabled(tenant: Tenant) -> bool:
    return bool(getattr(tenant, "situational_context_enabled", False)) and not getattr(tenant, "is_eval_sink", False)


def clean_place_label(label: object) -> str:
    if not isinstance(label, str):
        return ""
    clean = label.strip()
    if not clean or len(clean) > 64:
        return ""
    if len(clean.splitlines()) != 1:
        return ""
    if any(ch in _MARKDOWN_SIGNIFICANT or unicodedata.category(ch) == "Cc" for ch in clean):
        return ""
    return clean


def is_assignable_profile_timezone(tz_name: str) -> bool:
    """Whether ``tz_name`` is a value the runtime profile endpoint accepts.

    Mirrors ``RuntimeProfileUpdateView``'s accept rules: ``"UTC"`` or an
    Area/Location IANA name that resolves via ``ZoneInfo``, never the ``Etc/*``
    family (whose POSIX sign is inverted) and never a bare abbreviation.
    """
    if not isinstance(tz_name, str):
        return False
    if tz_name == "UTC":
        return True
    if "/" not in tz_name or tz_name.startswith("Etc/"):
        return False
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def resolve_place_timezone(label: str) -> PlaceTz:
    """Resolve a stated place label to a zone, a multi-zone region, or unknown.

    One Open-Meteo geocoding lookup, cached by label. Never raises: any lookup,
    cache, or shape failure is ``kind="unknown"`` so the caller can fall back
    to the model's own judgement.
    """
    if not isinstance(label, str) or not label.strip():
        return PlaceTz(kind="unknown")
    try:
        return _lookup_place_timezone(label)
    except Exception:
        return PlaceTz(kind="unknown")


def _lookup_place_timezone(label: str) -> PlaceTz:
    from apps.orchestrator.briefing_weather import GEOCODE_CACHE_SECONDS, GEOCODE_URL

    key = f"situation_place_tz:v1:{label.casefold()}"
    cached = cache.get(key)
    if isinstance(cached, PlaceTz):
        return cached

    url = f"{GEOCODE_URL}?{urlencode({'name': label, 'count': 1, 'language': 'en', 'format': 'json'})}"
    with httpx.Client(timeout=PLACE_TZ_TIMEOUT_SECONDS) as client:
        response = client.get(url)
        response.raise_for_status()
        data = response.json()

    results = data.get("results") if isinstance(data, dict) else None
    first = results[0] if isinstance(results, list) and results else None
    if not isinstance(first, dict):
        result = PlaceTz(kind="unknown")
    else:
        feature_code = str(first.get("feature_code") or "")
        if feature_code.startswith("PCL") or feature_code == "ADM1":
            result = PlaceTz(kind="region")
        else:
            tz_name = str(first.get("timezone") or "")
            result = (
                PlaceTz(kind="zone", tz=tz_name) if is_assignable_profile_timezone(tz_name) else PlaceTz(kind="unknown")
            )

    cache.set(key, result, GEOCODE_CACHE_SECONDS)
    return result


def _is_throttled(last_observed_at, observed_at) -> bool:
    return bool(last_observed_at and observed_at - last_observed_at < OBSERVATION_WRITE_THROTTLE)


def record_place_observation(
    tenant: Tenant,
    label: object,
    source: str,
    observed_at=None,
) -> bool:
    """Record one labeled place observation; return whether the label changed."""
    if not _capture_enabled(tenant):
        return False

    clean_label = clean_place_label(label)
    if not clean_label:
        return False

    observed_at = observed_at or timezone.now()
    clean_source = str(source or "").strip()[:16]

    with transaction.atomic():
        situation, _ = UserSituation.objects.select_for_update().get_or_create(tenant=tenant)
        changed = situation.current_place_label != clean_label
        if not changed and _is_throttled(situation.current_place_last_observed_at, observed_at):
            return False

        update_fields = ["current_place_last_observed_at", "updated_at"]
        situation.current_place_last_observed_at = observed_at
        if changed:
            situation.current_place_label = clean_label
            situation.current_place_source = clean_source
            situation.current_place_since = observed_at
            update_fields.extend(
                [
                    "current_place_label",
                    "current_place_source",
                    "current_place_since",
                ]
            )
        situation.save(update_fields=update_fields)

    home = str(getattr(getattr(tenant, "user", None), "location_city", "") or "").strip()
    differs_home = clean_label.casefold() != home.casefold()
    logger.info(
        "situation_updated tenant=%s source=%s labeled=1 changed=%d differs_home=%d",
        tenant.id,
        clean_source,
        int(changed),
        int(differs_home),
    )
    return changed


def record_device_tz(
    tenant: Tenant,
    tz_name: object,
    source_device: str,
    observed_at=None,
) -> bool:
    """Record a validated IANA device timezone; return whether its value changed."""
    if not _capture_enabled(tenant):
        return False

    if not isinstance(tz_name, str):
        return False
    clean_tz = tz_name.strip()
    if not clean_tz or len(clean_tz) > 64:
        logger.debug("situation_device_tz_invalid tenant=%s", tenant.id)
        return False
    try:
        ZoneInfo(clean_tz)
    except (ZoneInfoNotFoundError, ValueError):
        logger.debug("situation_device_tz_invalid tenant=%s", tenant.id)
        return False

    observed_at = observed_at or timezone.now()
    clean_source_device = str(source_device or "").strip()[:64]

    with transaction.atomic():
        situation, _ = UserSituation.objects.select_for_update().get_or_create(tenant=tenant)
        if situation.device_tz_last_observed_at is not None and observed_at < situation.device_tz_last_observed_at:
            return False

        changed = situation.device_tz != clean_tz
        if not changed and _is_throttled(situation.device_tz_last_observed_at, observed_at):
            return False

        update_fields = ["device_tz_last_observed_at", "updated_at"]
        situation.device_tz_last_observed_at = observed_at
        if changed:
            situation.device_tz = clean_tz
            situation.device_tz_since = observed_at
            situation.device_tz_source_device = clean_source_device
            situation.tz_offer_dismissed_for = ""
            update_fields.extend(
                [
                    "device_tz",
                    "device_tz_since",
                    "device_tz_source_device",
                    "tz_offer_dismissed_for",
                ]
            )
        situation.save(update_fields=update_fields)

    return changed
