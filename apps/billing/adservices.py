"""Apple Ads (AdServices) attribution lookup.

The iOS app gets an opaque, short-lived token from ``AAAttribution`` and POSTs
it to us; Apple's AdServices API resolves it to the campaign that drove the
install. Apple answers 404 until the token is ready ("retry later"), 400 when
the token is invalid, and 5xx when the service is unavailable.

All network access goes through :func:`fetch_attribution` so tests (and only
tests) can patch ``httpx.post`` without touching the resolution logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any

import httpx
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import AdAttribution

ADSERVICES_URL = "https://api-adservices.apple.com/api/v1/"
TIMEOUT_SECONDS = 5


class AdTokenInvalid(Exception):
    """Apple rejected the attribution token (400)."""


class AdServicesUnavailable(Exception):
    """Apple's AdServices API could not be reached or returned a 5xx."""


def fetch_attribution(token: str) -> dict | None:
    """Resolve one token. ``None`` means Apple says "not ready yet, retry"."""
    try:
        response = httpx.post(
            ADSERVICES_URL,
            content=token,
            headers={"Content-Type": "text/plain"},
            timeout=TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, OSError) as exc:
        raise AdServicesUnavailable("adservices_request_failed") from exc

    if response.status_code == 200:
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise AdServicesUnavailable("adservices_malformed_json") from exc
        if not isinstance(body, dict):
            raise AdServicesUnavailable("adservices_malformed_json")
        return body
    if response.status_code == 404:
        return None
    if response.status_code == 400:
        raise AdTokenInvalid("adservices_token_invalid")
    if response.status_code >= 500:
        raise AdServicesUnavailable("adservices_server_error")
    if 400 <= response.status_code < 500:
        raise AdTokenInvalid("adservices_token_rejected")
    raise AdServicesUnavailable("adservices_unexpected_status")


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any, limit: int = 16) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _parse_click_date(value: Any) -> datetime | None:
    """Parse Apple's ISO-ish ``clickDate``; unknown shapes become ``None``."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = parse_datetime(text)
            if parsed is None:
                day = parse_date(text)
                if day is not None:
                    parsed = datetime.combine(day, time.min)
                else:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def resolve(row: AdAttribution) -> AdAttribution:
    """Fetch and apply attribution for ``row``, persisting the outcome.

    A transient answer (404 or an unavailable API) increments ``attempts`` and
    leaves the row pending for the retry command. A terminal answer clears the
    token, which is no longer needed.
    """
    try:
        payload = fetch_attribution(row.token)
    except AdTokenInvalid:
        row.status = AdAttribution.Status.ERROR
        row.token = ""
        row.resolved_at = timezone.now()
        row.save(update_fields=["status", "token", "resolved_at"])
        return row
    except AdServicesUnavailable:
        row.attempts += 1
        row.save(update_fields=["attempts"])
        return row

    if payload is None:
        row.attempts += 1
        row.save(update_fields=["attempts"])
        return row

    row.raw = payload
    if payload.get("attribution"):
        row.status = AdAttribution.Status.ATTRIBUTED
        row.org_id = _as_int(payload.get("orgId"))
        row.campaign_id = _as_int(payload.get("campaignId"))
        row.ad_group_id = _as_int(payload.get("adGroupId"))
        row.keyword_id = _as_int(payload.get("keywordId"))
        row.ad_id = _as_int(payload.get("adId"))
        row.country_or_region = _as_text(payload.get("countryOrRegion"))
        row.click_date = _parse_click_date(payload.get("clickDate"))
    else:
        row.status = AdAttribution.Status.ORGANIC

    row.token = ""
    row.resolved_at = timezone.now()
    row.save(
        update_fields=[
            "status",
            "raw",
            "token",
            "resolved_at",
            "org_id",
            "campaign_id",
            "ad_group_id",
            "keyword_id",
            "ad_id",
            "country_or_region",
            "click_date",
        ]
    )
    return row
