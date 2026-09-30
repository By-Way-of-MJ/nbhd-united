"""Programmatic briefing weather: location ladder, Open-Meteo parsing, fixed renderers.

No real network: ``httpx.Client`` is replaced with a fake that routes by URL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from apps.orchestrator.briefing_weather import (
    INTRADAY_KINDS,
    NO_LOCATION_MESSAGE_LINE,
    NO_LOCATION_SECTION_LINE,
    TEMP_UNITS,
    UNAVAILABLE_MESSAGE_LINE,
    UNAVAILABLE_SECTION_LINE,
    WEATHER_SOURCES,
    WEATHER_STATUSES,
    DayForecast,
    IntradayWindow,
    WeatherReport,
    clothing_hint,
    condition_for_wmo_code,
    fetch_briefing_weather,
    temperature_unit_for_timezone,
    weather_message_line,
    weather_section_markdown,
)
from apps.tenants.models import UserSituation
from apps.tenants.services import create_tenant

# 08:00 Tokyo on 2026-09-30 == 23:00Z on 2026-09-29. Everything below that
# treats "today" as 2026-09-30 and "now" as hour 8.
_FIXED_NOW_UTC = datetime(2026, 9, 29, 23, 0, tzinfo=UTC)
_TODAY = "2026-09-30"
_TOMORROW = "2026-10-01"

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, *, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.invalid/")
            raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(self.status_code))

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._payload


class _FakeClient:
    """Stands in for ``httpx.Client``; ``route(url)`` returns a response or raises."""

    def __init__(self, route):
        self._route = route
        self.calls: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        self.calls.append(url)
        result = self._route(url)
        if isinstance(result, Exception):
            raise result
        return result


def _forecast_payload(*, hourly_probs=None, hourly_temps=None, today_code=2, tomorrow_code=61):
    hours = list(range(24))
    probs = hourly_probs or [10] * 24
    temps = hourly_temps or [18 + (h % 6) for h in hours]
    return {
        "daily": {
            "time": [_TODAY, _TOMORROW],
            "temperature_2m_min": [18.2, 16.0],
            "temperature_2m_max": [24.4, 22.0],
            "weather_code": [today_code, tomorrow_code],
            "precipitation_probability_max": [max(probs), 80],
        },
        "hourly": {
            "time": [f"{_TODAY}T{h:02d}:00" for h in hours] + [f"{_TOMORROW}T{h:02d}:00" for h in hours],
            "temperature_2m": temps + temps,
            "precipitation_probability": probs + probs,
        },
    }


def _geocode_payload(lat=35.0, lon=135.7):
    return {"results": [{"latitude": lat, "longitude": lon, "name": "Kyoto", "country_code": "JP"}]}


def _route(*, geocode=None, forecast=None):
    def route(url: str):
        if "geocoding-api" in url:
            return geocode if geocode is not None else _FakeResponse(200, {"results": []})
        return forecast if forecast is not None else _FakeResponse(200, _forecast_payload())

    return route


@override_settings(CACHES=_LOCMEM)
class FetchBriefingWeatherTest(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = create_tenant(display_name="Weather Tenant", telegram_chat_id=86300001)
        self.user = self.tenant.user
        self.user.timezone = "Asia/Tokyo"
        self.user.save()
        self.now_patch = patch("apps.orchestrator.briefing_weather.timezone.now", return_value=_FIXED_NOW_UTC)
        self.now_patch.start()
        self.addCleanup(self.now_patch.stop)

    def _fetch(self, route):
        client = _FakeClient(route)
        with patch("apps.orchestrator.briefing_weather.httpx.Client", client):
            report = fetch_briefing_weather(self.tenant)
        return report, client

    def _set_place(self, label: str, *, age: timedelta):
        UserSituation.objects.update_or_create(
            tenant=self.tenant,
            defaults={
                "current_place_label": label,
                "current_place_last_observed_at": _FIXED_NOW_UTC - age,
            },
        )

    # ── location ladder ──────────────────────────────────────────────

    def test_fresh_current_place_is_geocoded_and_wins(self):
        self._set_place("Kyoto", age=timedelta(hours=2))
        self.user.location_lat, self.user.location_lon, self.user.location_city = 34.69, 135.50, "Osaka"
        self.user.save()

        report, client = self._fetch(_route(geocode=_FakeResponse(200, _geocode_payload())))

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.source, "current_place")
        self.assertEqual(report.location_label, "Kyoto")
        self.assertIn("geocoding-api", client.calls[0])
        self.assertIn("latitude=35.0", client.calls[1])

    def test_stale_current_place_is_skipped_for_profile_coords(self):
        self._set_place("Kyoto", age=timedelta(hours=49))
        self.user.location_lat, self.user.location_lon, self.user.location_city = 34.69, 135.50, "Osaka"
        self.user.save()

        report, client = self._fetch(_route())

        self.assertEqual(report.source, "profile_coords")
        self.assertEqual(report.location_label, "Osaka")
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("geocoding-api", client.calls[0])
        self.assertIn("latitude=34.69", client.calls[0])

    def test_profile_coords_without_city_get_generic_label(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()

        report, _client = self._fetch(_route())

        self.assertEqual(report.source, "profile_coords")
        self.assertEqual(report.location_label, "your location")

    def test_profile_city_is_geocoded(self):
        self.user.location_city = "Osaka"
        self.user.save()

        report, client = self._fetch(_route(geocode=_FakeResponse(200, _geocode_payload(34.69, 135.50))))

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.source, "profile_city")
        self.assertEqual(report.location_label, "Osaka")
        self.assertIn("name=Osaka", client.calls[0])

    def test_profile_city_geocode_miss_is_no_location(self):
        self.user.location_city = "Nowhereville"
        self.user.save()

        report, client = self._fetch(_route(geocode=_FakeResponse(200, {"results": []})))

        self.assertEqual(report.status, "no_location")
        self.assertEqual(report.source, "none")
        self.assertEqual(len(client.calls), 1)

    def test_nothing_on_file_is_no_location_without_any_http(self):
        report, client = self._fetch(_route())

        self.assertEqual(report.status, "no_location")
        self.assertEqual(client.calls, [])
        self.assertIsNone(report.today)

    def test_no_timezone_coordinate_fallback(self):
        # Asia/Tokyo has an entry in TIMEZONE_COORDS; it must NOT be used.
        report, client = self._fetch(_route())
        self.assertEqual(report.status, "no_location")
        self.assertEqual(client.calls, [])

    # ── failure → unavailable, never raise ───────────────────────────

    def test_forecast_timeout_is_unavailable(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()

        report, _client = self._fetch(_route(forecast=httpx.ReadTimeout("slow")))

        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.source, "profile_coords")
        self.assertEqual(report.error, "forecast_ReadTimeout")

    def test_forecast_http_500_is_unavailable(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()

        report, _client = self._fetch(_route(forecast=_FakeResponse(500, {})))

        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.error, "forecast_HTTPStatusError")

    def test_forecast_bad_json_is_unavailable(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()

        report, _client = self._fetch(_route(forecast=_FakeResponse(200, None, bad_json=True)))

        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.error, "forecast_bad_json")

    def test_forecast_missing_daily_block_is_unavailable(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()

        report, _client = self._fetch(_route(forecast=_FakeResponse(200, {"hourly": {}})))

        self.assertEqual(report.status, "unavailable")
        self.assertTrue(report.error.startswith("forecast_daily_"))

    def test_geocode_error_is_unavailable(self):
        self.user.location_city = "Osaka"
        self.user.save()

        report, _client = self._fetch(_route(geocode=httpx.ConnectError("down")))

        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.source, "profile_city")
        self.assertEqual(report.error, "geocode_ConnectError")

    # ── cache ────────────────────────────────────────────────────────

    def test_forecast_is_cached_for_the_same_rounded_coords(self):
        self.user.location_lat, self.user.location_lon = 34.6937, 135.5023
        self.user.save()

        first, client_a = self._fetch(_route())
        second, client_b = self._fetch(_route())

        self.assertEqual(first.status, "ok")
        self.assertEqual(second.model_dump(), first.model_dump())
        self.assertEqual(len(client_a.calls), 1)
        self.assertEqual(client_b.calls, [])

    def test_geocode_hit_is_cached(self):
        self.user.location_city = "Osaka"
        self.user.save()

        _first, client_a = self._fetch(_route(geocode=_FakeResponse(200, _geocode_payload())))
        _second, client_b = self._fetch(_route(geocode=_FakeResponse(200, _geocode_payload())))

        self.assertEqual(len(client_a.calls), 2)
        self.assertEqual(client_b.calls, [])

    # ── units ────────────────────────────────────────────────────────

    def test_us_timezone_requests_fahrenheit(self):
        self.user.timezone = "America/New_York"
        self.user.location_lat, self.user.location_lon = 40.71, -74.01
        self.user.save()
        # 23:00Z is 19:00 New York on 09-29, so pin the forecast to that day.
        payload = _forecast_payload()
        payload["daily"]["time"] = ["2026-09-29", "2026-09-30"]

        report, client = self._fetch(_route(forecast=_FakeResponse(200, payload)))

        self.assertEqual(report.status, "ok", report.error)
        self.assertEqual(report.unit, "F")
        self.assertIn("temperature_unit=fahrenheit", client.calls[0])
        self.assertIn("timezone=America%2FNew_York", client.calls[0])

    # ── parsing / intraday ───────────────────────────────────────────

    def test_ok_report_maps_days_and_conditions(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()

        report, _client = self._fetch(_route())

        self.assertEqual(report.today.date, _TODAY)
        self.assertEqual(report.today.temp_min, 18.2)
        self.assertEqual(report.today.temp_max, 24.4)
        self.assertEqual(report.today.condition, "partly cloudy")
        self.assertEqual(report.tomorrow.date, _TOMORROW)
        self.assertEqual(report.tomorrow.condition, "rain")
        self.assertEqual(report.tomorrow.precip_prob_max, 80)
        self.assertEqual(report.intraday, [])

    def test_rain_window_merges_the_run_and_reports_the_peak(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()
        probs = [10] * 24
        probs[13], probs[14], probs[15] = 60, 70, 55

        report, _client = self._fetch(_route(forecast=_FakeResponse(200, _forecast_payload(hourly_probs=probs))))

        self.assertEqual(len(report.intraday), 1)
        window = report.intraday[0]
        self.assertEqual((window.kind, window.start_hour, window.end_hour), ("rain", 13, 16))
        self.assertEqual(window.detail, "peak 70% at 14:00")

    def test_rain_before_now_is_not_flagged(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()
        probs = [10] * 24
        probs[3], probs[4], probs[5] = 90, 90, 90  # rained overnight; now is 08:00

        report, _client = self._fetch(_route(forecast=_FakeResponse(200, _forecast_payload(hourly_probs=probs))))

        self.assertEqual(report.intraday, [])

    def test_temp_drop_window_uses_running_high(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()
        temps = [20.0] * 24
        temps[12], temps[18] = 26.0, 16.0  # 10°C drop between noon and 18:00

        report, _client = self._fetch(_route(forecast=_FakeResponse(200, _forecast_payload(hourly_temps=temps))))

        self.assertEqual(len(report.intraday), 1)
        window = report.intraday[0]
        self.assertEqual((window.kind, window.start_hour, window.end_hour), ("temp_drop", 12, 18))
        self.assertEqual(window.detail, "26°C→16°C by 18:00")

    def test_small_temp_drop_is_ignored(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()
        temps = [20.0] * 24
        temps[12], temps[18] = 26.0, 19.0  # 7°C: under the 8°C threshold

        report, _client = self._fetch(_route(forecast=_FakeResponse(200, _forecast_payload(hourly_temps=temps))))

        self.assertEqual(report.intraday, [])

    def test_rain_and_drop_together_cap_at_two_rain_first(self):
        self.user.location_lat, self.user.location_lon = 34.69, 135.50
        self.user.save()
        probs = [10] * 24
        probs[13], probs[14] = 60, 80
        temps = [20.0] * 24
        temps[12], temps[18] = 26.0, 16.0

        report, _client = self._fetch(
            _route(forecast=_FakeResponse(200, _forecast_payload(hourly_probs=probs, hourly_temps=temps)))
        )

        self.assertEqual([w.kind for w in report.intraday], ["rain", "temp_drop"])


class VocabularyAndModelTest(SimpleTestCase):
    def test_literals_come_from_the_constants(self):
        self.assertEqual(WEATHER_STATUSES, ("ok", "no_location", "unavailable"))
        self.assertEqual(WEATHER_SOURCES, ("current_place", "profile_coords", "profile_city", "none"))
        self.assertEqual(TEMP_UNITS, ("C", "F"))
        self.assertEqual(INTRADAY_KINDS, ("rain", "temp_drop"))
        schema = WeatherReport.model_json_schema()
        self.assertEqual(schema["properties"]["status"]["enum"], list(WEATHER_STATUSES))
        self.assertEqual(schema["properties"]["source"]["enum"], list(WEATHER_SOURCES))
        self.assertFalse(schema.get("additionalProperties", False))

    def test_ok_report_requires_forecast_and_place(self):
        with self.assertRaises(ValueError):
            WeatherReport(status="ok", source="profile_city", location_label="Osaka")
        with self.assertRaises(ValueError):
            WeatherReport(status="ok", source="none", location_label="Osaka", today=_day())

    def test_non_ok_report_cannot_carry_forecast(self):
        with self.assertRaises(ValueError):
            WeatherReport(status="unavailable", today=_day())

    def test_wmo_mapping_has_an_honest_exit(self):
        self.assertEqual(condition_for_wmo_code(0), "clear")
        self.assertEqual(condition_for_wmo_code(3), "cloudy")
        self.assertEqual(condition_for_wmo_code(63), "rain")
        self.assertEqual(condition_for_wmo_code(95), "thunderstorms")
        self.assertEqual(condition_for_wmo_code(42), "mixed")
        self.assertEqual(condition_for_wmo_code(None), "mixed")

    def test_unit_rule(self):
        self.assertEqual(temperature_unit_for_timezone("America/New_York"), "F")
        self.assertEqual(temperature_unit_for_timezone("Pacific/Honolulu"), "F")
        self.assertEqual(temperature_unit_for_timezone("US/Eastern"), "F")
        self.assertEqual(temperature_unit_for_timezone("America/Toronto"), "C")
        self.assertEqual(temperature_unit_for_timezone("Asia/Tokyo"), "C")
        self.assertEqual(temperature_unit_for_timezone(""), "C")
        self.assertEqual(temperature_unit_for_timezone(None), "C")


def _day(*, date=_TODAY, lo=18.2, hi=24.4, condition="partly cloudy", prob=None):
    return DayForecast(date=date, temp_min=lo, temp_max=hi, condition=condition, precip_prob_max=prob)


def _ok(**overrides):
    fields = {
        "status": "ok",
        "source": "profile_city",
        "location_label": "Osaka",
        "unit": "C",
        "today": _day(),
        "tomorrow": _day(date=_TOMORROW, lo=16.0, hi=22.0, condition="rain", prob=80),
    }
    fields.update(overrides)
    return WeatherReport(**fields)


class RendererTest(SimpleTestCase):
    def test_no_location_strings(self):
        report = WeatherReport(status="no_location")
        self.assertEqual(weather_message_line(report), NO_LOCATION_MESSAGE_LINE)
        self.assertEqual(weather_section_markdown(report), NO_LOCATION_SECTION_LINE)
        self.assertEqual(NO_LOCATION_MESSAGE_LINE, "Add your city in Settings to get weather in your briefing.")

    def test_unavailable_strings(self):
        report = WeatherReport(status="unavailable", source="profile_coords", error="forecast_ReadTimeout")
        self.assertEqual(weather_message_line(report), UNAVAILABLE_MESSAGE_LINE)
        self.assertEqual(weather_section_markdown(report), UNAVAILABLE_SECTION_LINE)
        self.assertEqual(UNAVAILABLE_MESSAGE_LINE, "Weather couldn't be fetched this morning.")
        self.assertNotIn("ReadTimeout", weather_section_markdown(report))

    def test_stable_day(self):
        report = _ok()
        self.assertEqual(weather_message_line(report), "Osaka: 18–24°C, partly cloudy — light layers.")
        self.assertEqual(
            weather_section_markdown(report),
            "**Today:** 18–24°C, partly cloudy. Light layers.\n**Tomorrow:** 16–22°C, rain.",
        )

    def test_rain_window_day(self):
        report = _ok(
            today=_day(condition="rain", prob=70),
            intraday=[IntradayWindow(kind="rain", start_hour=13, end_hour=16, detail="peak 70% at 14:00")],
        )
        self.assertEqual(weather_message_line(report), "Osaka: 18–24°C, rain ~13:00–16:00 — umbrella, light layers.")
        self.assertEqual(
            weather_section_markdown(report),
            "**Today:** 18–24°C, rain. Umbrella, light layers.\n"
            "**Intraday:**\n"
            "- Rain ~13:00–16:00 (peak 70% at 14:00)\n"
            "**Tomorrow:** 16–22°C, rain.",
        )

    def test_temp_drop_day_in_fahrenheit(self):
        report = _ok(
            unit="F",
            location_label="Denver",
            today=_day(lo=48.0, hi=68.0, condition="clear"),
            tomorrow=None,
            intraday=[IntradayWindow(kind="temp_drop", start_hour=12, end_hour=18, detail="68°F→48°F by 18:00")],
        )
        self.assertEqual(
            weather_message_line(report), "Denver: 48–68°F, clear, drops to 48°F by 18:00 — jacket for later."
        )
        self.assertEqual(
            weather_section_markdown(report),
            "**Today:** 48–68°F, clear. Jacket for later.\n"
            "**Intraday:**\n"
            "- Temp drops 68°F→48°F by 18:00\n"
            "**Tomorrow:** not available.",
        )

    def test_clothing_hint_rules(self):
        self.assertEqual(clothing_hint(_ok(today=_day(lo=2.0, hi=8.0))), "warm coat")
        self.assertEqual(clothing_hint(_ok(today=_day(lo=8.0, hi=15.0))), "jacket")
        self.assertEqual(clothing_hint(_ok(today=_day(lo=18.0, hi=25.0))), "light layers")
        self.assertEqual(clothing_hint(_ok(today=_day(lo=24.0, hi=33.0))), "stay cool, hydrate")
        self.assertEqual(clothing_hint(_ok(today=_day(lo=24.0, hi=33.0, prob=60))), "umbrella, stay cool, hydrate")
        self.assertEqual(clothing_hint(_ok(unit="F", today=_day(lo=30.0, hi=45.0))), "warm coat")
        self.assertEqual(clothing_hint(_ok(unit="F", today=_day(lo=60.0, hi=75.0))), "light layers")
        self.assertEqual(clothing_hint(WeatherReport(status="unavailable")), "")
