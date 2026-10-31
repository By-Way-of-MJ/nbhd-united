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


def _normalize_place_text(value: object) -> str:
    """Casefold ``value`` and strip accents so ``Uşak`` matches ``usak``."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _lookup_place_timezone(label: str) -> PlaceTz:
    from apps.orchestrator.briefing_weather import GEOCODE_CACHE_SECONDS, GEOCODE_URL

    key = f"situation_place_tz:v2:{label.casefold()}"
    cached = cache.get(key)
    if isinstance(cached, PlaceTz):
        return cached

    name, _, qualifier = label.partition(",")
    name = name.strip()
    qualifier = qualifier.strip()
    if not name:
        return PlaceTz(kind="unknown")

    url = f"{GEOCODE_URL}?{urlencode({'name': name, 'count': 10, 'language': 'en', 'format': 'json'})}"
    with httpx.Client(timeout=PLACE_TZ_TIMEOUT_SECONDS) as client:
        response = client.get(url)
        response.raise_for_status()
        data = response.json()

    raw_results = data.get("results") if isinstance(data, dict) else None
    results = raw_results if isinstance(raw_results, list) else []

    normalized_name = _normalize_place_text(name)
    normalized_qualifier = _normalize_place_text(qualifier)
    exact: list[dict] = []
    for item in results:
        if not isinstance(item, dict) or _normalize_place_text(item.get("name")) != normalized_name:
            continue
        if normalized_qualifier:
            qualifiers = {
                _normalize_place_text(value)
                for value in (item.get("admin1"), item.get("country"), item.get("country_code"))
            }
            if normalized_qualifier not in qualifiers:
                continue
        exact.append(item)

    if any(
        str(item.get("feature_code") or "").startswith("PCL") or item.get("feature_code") == "ADM1" for item in exact
    ):
        result = PlaceTz(kind="region")
    else:
        kept: list[tuple[int, str]] = []
        for item in exact:
            feature_code = str(item.get("feature_code") or "")
            if not feature_code.startswith("PPL"):
                continue
            tz_name = str(item.get("timezone") or "")
            if not is_assignable_profile_timezone(tz_name):
                continue
            population = item.get("population")
            if not isinstance(population, int) or isinstance(population, bool):
                continue
            kept.append((population, tz_name))

        if not kept:
            result = PlaceTz(kind="unknown")
        else:
            top_population, top_tz = max(kept, key=lambda place: place[0])
            clear_winner = top_population >= 100_000 and all(
                tz_name == top_tz or population <= top_population / 5 for population, tz_name in kept
            )
            result = PlaceTz(kind="zone", tz=top_tz) if clear_winner else PlaceTz(kind="unknown")

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


def self_heal_profile_timezone(tenant: Tenant, device_tz: str) -> None:
    """Adopt the device's IANA zone when the user still has the unset
    'UTC' default — iOS is the only channel that knows ground truth, and
    the auto-complete matcher buckets days by tenant tz, so an evening
    workout under a wrong UTC default would land on tomorrow's date and
    never match (the known 17/27-users-on-UTC latent bug)."""
    if not device_tz or device_tz == "UTC":
        return
    user = getattr(tenant, "user", None)
    if user is None or user.timezone != "UTC":
        return
    try:
        ZoneInfo(device_tz)
    except Exception:
        return
    user.timezone = device_tz
    user.save(update_fields=["timezone"])
    logger.info("Self-healed timezone for tenant %s from device_tz=%s", str(tenant.id)[:8], device_tz)


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
