"""Programmatic Morning Briefing weather (Open-Meteo), no LLM guessing.

The morning briefing used to ask the model to ``web_search`` the weather and
narrate whatever came back; when the search failed the model invented a
forecast ("heavy, wet and windy"). This module makes the weather step a
deterministic function of data we actually hold:

    location  →  Open-Meteo forecast  →  ``WeatherReport``  →  fixed strings

``fetch_briefing_weather`` NEVER raises to its caller: every failure is a
``status`` the renderers turn into an honest sentence, and the runtime
endpoint (``RuntimeWeatherBriefingView``) writes that sentence into the daily
note's ``weather`` section and hands the model a ``message_line`` to relay
VERBATIM. The model no longer composes weather from anything.

Location order (owner decision — no timezone-coordinate fallback, a wrong city
is worse than an honest "no location"):

1. fresh ``UserSituation.current_place_label`` (same rule as USER.md
   ``## Right now`` — ``apps.tenants.envelope.is_place_fresh``), geocoded;
2. ``User.location_lat`` / ``User.location_lon``;
3. ``User.location_city``, geocoded;
4. otherwise ``status="no_location"``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

import httpx
from django.core.cache import cache
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.common.tenant_tz import safe_zoneinfo, tenant_tz_name
from apps.orchestrator.weather import build_weather_url_from_coords

logger = logging.getLogger(__name__)

# ── Vocabulary (one source for the Literals, the prompt, and the tests) ──

WEATHER_STATUSES: tuple[str, ...] = ("ok", "no_location", "unavailable")
WEATHER_SOURCES: tuple[str, ...] = ("current_place", "profile_coords", "profile_city", "none")
TEMP_UNITS: tuple[str, ...] = ("C", "F")
INTRADAY_KINDS: tuple[str, ...] = ("rain", "temp_drop")

WeatherStatus = Literal["ok", "no_location", "unavailable"]
WeatherSource = Literal["current_place", "profile_coords", "profile_city", "none"]
TempUnit = Literal["C", "F"]
IntradayKind = Literal["rain", "temp_drop"]

# Fixed user-facing sentences for the non-ok statuses. The runtime endpoint
# returns ``message_line`` and the prompt tells the model to relay it VERBATIM,
# so these strings are the whole "what the user hears when weather is missing"
# contract. Tests pin them.
NO_LOCATION_MESSAGE_LINE = "Add your city in Settings to get weather in your briefing."
UNAVAILABLE_MESSAGE_LINE = "Weather couldn't be fetched this morning."
NO_LOCATION_SECTION_LINE = (
    "**Today:** Weather unavailable — no location on file. Add your city in Settings to get weather here."
)
UNAVAILABLE_SECTION_LINE = "**Today:** Weather couldn't be fetched this morning."

FORECAST_TIMEOUT_SECONDS = 4.0
FORECAST_CACHE_SECONDS = 30 * 60
GEOCODE_CACHE_SECONDS = 24 * 60 * 60
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Intraday thresholds — the same ones the old prompt asked the model to apply
# by eye, now applied by code.
RAIN_PROBABILITY_THRESHOLD = 50
TEMP_DROP_THRESHOLD = {"C": 8.0, "F": 15.0}
MAX_INTRADAY_WINDOWS = 2

# IANA zones whose users expect Fahrenheit. Anything else gets Celsius.
_FAHRENHEIT_TZ_PREFIXES: tuple[str, ...] = ("US/",)
_FAHRENHEIT_TZS: frozenset[str] = frozenset(
    {
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Phoenix",
        "America/Anchorage",
        "America/Detroit",
        "America/Boise",
        "America/Juneau",
        "America/Adak",
        "America/Sitka",
        "America/Nome",
        "America/Yakutat",
        "America/Metlakatla",
        "America/Menominee",
        "America/Indiana/Indianapolis",
        "America/Indiana/Knox",
        "America/Indiana/Marengo",
        "America/Indiana/Petersburg",
        "America/Indiana/Tell_City",
        "America/Indiana/Vevay",
        "America/Indiana/Vincennes",
        "America/Indiana/Winamac",
        "America/Kentucky/Louisville",
        "America/Kentucky/Monticello",
        "America/North_Dakota/Beulah",
        "America/North_Dakota/Center",
        "America/North_Dakota/New_Salem",
        "Pacific/Honolulu",
    }
)

# WMO weather interpretation codes → short words. Unknown codes fall through
# to the honest "mixed" rather than a guess.
_WMO_CONDITIONS: tuple[tuple[frozenset[int], str], ...] = (
    (frozenset({0}), "clear"),
    (frozenset({1}), "mostly clear"),
    (frozenset({2}), "partly cloudy"),
    (frozenset({3}), "cloudy"),
    (frozenset({45, 48}), "fog"),
    (frozenset({51, 53, 55, 56, 57}), "drizzle"),
    (frozenset({61, 63, 65, 66, 67}), "rain"),
    (frozenset({71, 73, 75, 77}), "snow"),
    (frozenset({80, 81, 82}), "showers"),
    (frozenset({85, 86}), "snow showers"),
    (frozenset({95, 96, 99}), "thunderstorms"),
)
UNKNOWN_CONDITION = "mixed"


def condition_for_wmo_code(code: int | None) -> str:
    if code is None:
        return UNKNOWN_CONDITION
    for codes, label in _WMO_CONDITIONS:
        if code in codes:
            return label
    return UNKNOWN_CONDITION


def temperature_unit_for_timezone(tz: str | None) -> TempUnit:
    """°F for US zones, °C everywhere else (no per-user unit preference exists yet)."""
    name = (tz or "").strip()
    if name in _FAHRENHEIT_TZS or name.startswith(_FAHRENHEIT_TZ_PREFIXES):
        return "F"
    return "C"


# ── Models ────────────────────────────────────────────────────────────


class DayForecast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    temp_min: float
    temp_max: float
    condition: str
    precip_prob_max: int | None = None


class IntradayWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IntradayKind
    start_hour: int = Field(ge=0, le=24)
    end_hour: int = Field(ge=0, le=24)
    detail: str


class WeatherReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: WeatherStatus
    source: WeatherSource = "none"
    location_label: str | None = None
    unit: TempUnit = "C"
    today: DayForecast | None = None
    tomorrow: DayForecast | None = None
    intraday: list[IntradayWindow] = Field(default_factory=list, max_length=MAX_INTRADAY_WINDOWS)
    error: str | None = None

    @model_validator(mode="after")
    def _shape_matches_status(self) -> WeatherReport:
        # Shape is not truth: an "ok" report must actually carry a forecast and
        # a place, and a non-ok report must not smuggle stale numbers through.
        if self.status == "ok":
            if self.today is None or not self.location_label:
                raise ValueError("ok report requires today + location_label")
            if self.source == "none":
                raise ValueError("ok report requires a real source")
        else:
            if self.today is not None or self.tomorrow is not None or self.intraday:
                raise ValueError("non-ok report must not carry forecast data")
        return self


class _Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float
    lon: float
    label: str
    source: WeatherSource


# ── Fetch ─────────────────────────────────────────────────────────────


def fetch_briefing_weather(tenant, *, on_date: date | None = None) -> WeatherReport:
    """Resolve the tenant's location, fetch Open-Meteo, return a ``WeatherReport``.

    Never raises: any failure is ``status="unavailable"`` with a short reason
    in ``error`` (never a URL with coordinates, never model output).
    """
    tz_name = tenant_tz_name(tenant)
    unit = temperature_unit_for_timezone(tz_name)
    now_local = timezone.now().astimezone(safe_zoneinfo(tz_name))
    target = on_date or now_local.date()

    try:
        with httpx.Client(timeout=FORECAST_TIMEOUT_SECONDS) as client:
            location = _resolve_location(tenant, client)
            if location is None:
                return WeatherReport(status="no_location", source="none", unit=unit)
            payload = _fetch_forecast(client, location, tz_name=tz_name, unit=unit)
            return _report_from_payload(
                payload,
                location=location,
                unit=unit,
                target=target,
                now_local=now_local,
            )
    except _Unavailable as exc:
        logger.warning(
            "briefing_weather_unavailable tenant=%s source=%s reason=%s",
            str(getattr(tenant, "id", ""))[:8],
            exc.source,
            exc.reason,
        )
        return WeatherReport(status="unavailable", source=exc.source, unit=unit, error=exc.reason)
    except Exception as exc:  # pragma: no cover - belt and braces, never raise to the cron path
        logger.warning(
            "briefing_weather_unavailable tenant=%s source=unknown reason=%s",
            str(getattr(tenant, "id", ""))[:8],
            type(exc).__name__,
        )
        return WeatherReport(status="unavailable", source="none", unit=unit, error=type(exc).__name__)


class _Unavailable(Exception):
    def __init__(self, reason: str, *, source: str = "none") -> None:
        super().__init__(reason)
        self.reason = reason
        self.source = source


def _resolve_location(tenant, client: httpx.Client) -> _Location | None:
    from apps.tenants.envelope import is_place_fresh
    from apps.tenants.models import UserSituation

    user = getattr(tenant, "user", None)

    # 1. Fresh current place (same freshness rule as USER.md "## Right now").
    try:
        situation = tenant.situation
    except UserSituation.DoesNotExist:
        situation = None
    if is_place_fresh(situation):
        label = situation.current_place_label.strip()
        hit = _geocode(client, label, source="current_place")
        if hit is not None:
            return _Location(lat=hit[0], lon=hit[1], label=label, source="current_place")

    # 2. Profile coordinates.
    lat = getattr(user, "location_lat", None)
    lon = getattr(user, "location_lon", None)
    city = str(getattr(user, "location_city", "") or "").strip()
    if lat is not None and lon is not None:
        return _Location(lat=float(lat), lon=float(lon), label=city or "your location", source="profile_coords")

    # 3. Profile city, geocoded.
    if city:
        hit = _geocode(client, city, source="profile_city")
        if hit is not None:
            return _Location(lat=hit[0], lon=hit[1], label=city, source="profile_city")

    return None


def _geocode(client: httpx.Client, label: str, *, source: str) -> tuple[float, float] | None:
    """Open-Meteo geocoding. ``None`` = no match (falls through); errors → unavailable."""
    key = f"briefing_geocode:v1:{label.casefold()}"
    cached = cache.get(key)
    if cached is not None:
        return None if cached == "miss" else (float(cached[0]), float(cached[1]))

    url = f"{GEOCODE_URL}?{urlencode({'name': label, 'count': 1, 'language': 'en', 'format': 'json'})}"
    try:
        response = client.get(url)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise _Unavailable(f"geocode_{type(exc).__name__}", source=source) from exc
    except ValueError as exc:
        raise _Unavailable("geocode_bad_json", source=source) from exc

    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        cache.set(key, "miss", GEOCODE_CACHE_SECONDS)
        return None
    first = results[0]
    try:
        hit = (float(first["latitude"]), float(first["longitude"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise _Unavailable("geocode_bad_shape", source=source) from exc
    cache.set(key, hit, GEOCODE_CACHE_SECONDS)
    return hit


def _fetch_forecast(client: httpx.Client, location: _Location, *, tz_name: str, unit: TempUnit) -> dict:
    key = f"briefing_weather:v1:{location.lat:.2f}:{location.lon:.2f}:{unit}:{tz_name}"
    cached = cache.get(key)
    if isinstance(cached, dict):
        return cached

    url = build_weather_url_from_coords(
        round(location.lat, 2),
        round(location.lon, 2),
        tz_name,
        forecast_days=2,
        temperature_unit="fahrenheit" if unit == "F" else None,
    )
    try:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise _Unavailable(f"forecast_{type(exc).__name__}", source=location.source) from exc
    except ValueError as exc:
        raise _Unavailable("forecast_bad_json", source=location.source) from exc
    if not isinstance(payload, dict):
        raise _Unavailable("forecast_bad_shape", source=location.source)
    cache.set(key, payload, FORECAST_CACHE_SECONDS)
    return payload


def _report_from_payload(
    payload: dict,
    *,
    location: _Location,
    unit: TempUnit,
    target: date,
    now_local: datetime,
) -> WeatherReport:
    try:
        daily = payload["daily"]
        days = {
            str(day): DayForecast(
                date=str(day),
                temp_min=float(daily["temperature_2m_min"][i]),
                temp_max=float(daily["temperature_2m_max"][i]),
                condition=condition_for_wmo_code(_opt_int(daily["weather_code"][i])),
                precip_prob_max=_opt_int(daily.get("precipitation_probability_max", [None] * len(daily["time"]))[i]),
            )
            for i, day in enumerate(daily["time"])
        }
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise _Unavailable(f"forecast_daily_{type(exc).__name__}", source=location.source) from exc

    today = days.get(target.isoformat())
    if today is None:
        raise _Unavailable("forecast_missing_target_day", source=location.source)
    tomorrow = days.get(_next_day(target))

    # Only hours AHEAD of now count for today's windows — rain that already
    # ended must never be flagged as coming. A non-today target keeps every hour.
    from_hour = now_local.hour if target == now_local.date() else 0
    intraday = _intraday_windows(payload.get("hourly") or {}, target=target, from_hour=from_hour, unit=unit)

    return WeatherReport(
        status="ok",
        source=location.source,
        location_label=location.label,
        unit=unit,
        today=today,
        tomorrow=tomorrow,
        intraday=intraday,
    )


def _next_day(d: date) -> str:
    return (d + timedelta(days=1)).isoformat()


def _opt_int(value) -> int | None:
    if value is None:
        return None
    return int(round(float(value)))


def _intraday_windows(hourly: dict, *, target: date, from_hour: int, unit: TempUnit) -> list[IntradayWindow]:
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    probs = hourly.get("precipitation_probability") or []
    prefix = target.isoformat()

    hours: list[tuple[int, float | None, int | None]] = []
    for i, stamp in enumerate(times):
        stamp = str(stamp)
        if not stamp.startswith(prefix):
            continue
        try:
            hour = int(stamp[11:13])
        except ValueError:
            continue
        if hour < from_hour:
            continue
        temp = temps[i] if i < len(temps) else None
        prob = probs[i] if i < len(probs) else None
        hours.append((hour, None if temp is None else float(temp), _opt_int(prob)))

    windows: list[IntradayWindow] = []
    rain = _rain_window(hours)
    if rain is not None:
        windows.append(rain)
    drop = _temp_drop_window(hours, unit=unit)
    if drop is not None:
        windows.append(drop)
    return windows[:MAX_INTRADAY_WINDOWS]


def _rain_window(hours: list[tuple[int, float | None, int | None]]) -> IntradayWindow | None:
    """First run of consecutive hours with precipitation probability ≥ threshold."""
    run: list[tuple[int, int]] = []
    for hour, _temp, prob in hours:
        if prob is not None and prob >= RAIN_PROBABILITY_THRESHOLD:
            run.append((hour, prob))
        elif run:
            break
    if not run:
        return None
    peak_hour, peak_prob = max(run, key=lambda pair: (pair[1], -pair[0]))
    start, end = run[0][0], run[-1][0] + 1
    return IntradayWindow(
        kind="rain",
        start_hour=start,
        end_hour=end,
        detail=f"peak {peak_prob}% at {peak_hour:02d}:00",
    )


def _temp_drop_window(hours: list[tuple[int, float | None, int | None]], *, unit: TempUnit) -> IntradayWindow | None:
    """A drop of ≥ threshold from the running high to a later hour."""
    threshold = TEMP_DROP_THRESHOLD[unit]
    high: tuple[int, float] | None = None
    for hour, temp, _prob in hours:
        if temp is None:
            continue
        if high is None or temp > high[1]:
            high = (hour, temp)
            continue
        if high[1] - temp >= threshold:
            return IntradayWindow(
                kind="temp_drop",
                start_hour=high[0],
                end_hour=hour,
                detail=f"{_fmt_temp(high[1])}°{unit}→{_fmt_temp(temp)}°{unit} by {hour:02d}:00",
            )
    return None


# ── Render (pure, no LLM) ─────────────────────────────────────────────


def _fmt_temp(value: float) -> str:
    return str(int(round(value)))


def _temp_range(day: DayForecast, unit: TempUnit) -> str:
    return f"{_fmt_temp(day.temp_min)}–{_fmt_temp(day.temp_max)}°{unit}"


def _hour_range(window: IntradayWindow) -> str:
    return f"~{window.start_hour:02d}:00–{window.end_hour:02d}:00"


def _window(report: WeatherReport, kind: IntradayKind) -> IntradayWindow | None:
    return next((w for w in report.intraday if w.kind == kind), None)


def clothing_hint(report: WeatherReport) -> str:
    """Deterministic what-to-wear from temperature + rain rules."""
    if report.status != "ok" or report.today is None:
        return ""
    today = report.today
    is_f = report.unit == "F"
    coat_below, jacket_below, layers_below = (50.0, 64.0, 79.0) if is_f else (10.0, 18.0, 26.0)

    if _window(report, "temp_drop") is not None:
        layer = "jacket for later"
    elif today.temp_max < coat_below:
        layer = "warm coat"
    elif today.temp_max < jacket_below:
        layer = "jacket"
    elif today.temp_max < layers_below:
        layer = "light layers"
    else:
        layer = "stay cool, hydrate"

    rainy = _window(report, "rain") is not None or (
        today.precip_prob_max is not None and today.precip_prob_max >= RAIN_PROBABILITY_THRESHOLD
    )
    if rainy:
        return f"umbrella, {layer}"
    return layer


def weather_section_markdown(report: WeatherReport) -> str:
    """The daily note ``weather`` section body — same shape the prompt used to describe."""
    if report.status == "no_location":
        return NO_LOCATION_SECTION_LINE
    if report.status != "ok" or report.today is None:
        return UNAVAILABLE_SECTION_LINE

    hint = clothing_hint(report)
    lines = [
        f"**Today:** {_temp_range(report.today, report.unit)}, {report.today.condition}. {hint[:1].upper()}{hint[1:]}."
    ]
    if report.intraday:
        lines.append("**Intraday:**")
        for window in report.intraday:
            if window.kind == "rain":
                lines.append(f"- Rain {_hour_range(window)} ({window.detail})")
            else:
                lines.append(f"- Temp drops {window.detail}")
    if report.tomorrow is not None:
        lines.append(f"**Tomorrow:** {_temp_range(report.tomorrow, report.unit)}, {report.tomorrow.condition}.")
    else:
        lines.append("**Tomorrow:** not available.")
    return "\n".join(lines)


def weather_message_line(report: WeatherReport) -> str:
    """The ONE weather line of the user message; the prompt relays it verbatim."""
    if report.status == "no_location":
        return NO_LOCATION_MESSAGE_LINE
    if report.status != "ok" or report.today is None:
        return UNAVAILABLE_MESSAGE_LINE

    parts = [_temp_range(report.today, report.unit)]
    rain = _window(report, "rain")
    parts.append(f"rain {_hour_range(rain)}" if rain is not None else report.today.condition)
    drop = _window(report, "temp_drop")
    if drop is not None:
        parts.append(f"drops to {drop.detail.split('→', 1)[1]}")
    return f"{report.location_label}: {', '.join(parts)} — {clothing_hint(report)}."
