"""Offline GeoNames-derived place timezone lookups and table builder."""

from __future__ import annotations

import gzip
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase

from apps.pii.redactor import annotate_model_context
from apps.tenants.place_tz_offline import (
    _load_place_tz_table,
    offline_place_timezone,
)
from apps.tenants.situation import (
    PlaceTz,
    cached_place_timezone,
    resolve_place_timezone,
)


class OfflinePlaceTimezoneTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        _load_place_tz_table.cache_clear()
        self.addCleanup(cache.clear)
        self.addCleanup(_load_place_tz_table.cache_clear)

    @staticmethod
    def _zone(tz: str) -> PlaceTz:
        return PlaceTz(kind="zone", tz=tz)

    def _offline_without_network(self, label: str) -> PlaceTz | None:
        with patch("apps.tenants.situation.httpx.Client", side_effect=AssertionError("network used")) as client:
            result = offline_place_timezone(label)
        client.assert_not_called()
        return result

    def test_cities_and_qualifiers(self):
        self.assertEqual(self._offline_without_network("Chicago"), self._zone("America/Chicago"))
        self.assertEqual(self._offline_without_network("Tokyo"), self._zone("Asia/Tokyo"))
        self.assertEqual(
            self._offline_without_network("Chicago, Illinois"),
            self._zone("America/Chicago"),
        )
        self.assertEqual(self._offline_without_network("Chicago, US"), self._zone("America/Chicago"))

    def test_states_and_country_qualifiers_disambiguate(self):
        self.assertEqual(self._offline_without_network("Georgia"), PlaceTz(kind="region"))
        self.assertEqual(self._offline_without_network("Georgia, US"), self._zone("America/New_York"))
        self.assertEqual(self._offline_without_network("Georgia, Georgia"), self._zone("Asia/Tbilisi"))

    def test_states_resolve_or_ask(self):
        self.assertEqual(self._offline_without_network("Washington"), PlaceTz(kind="region"))
        self.assertEqual(self._offline_without_network("New York"), self._zone("America/New_York"))
        self.assertEqual(self._offline_without_network("California"), self._zone("America/Los_Angeles"))
        self.assertEqual(self._offline_without_network("Texas"), PlaceTz(kind="region"))

    def test_unchanged_city_and_country_cases(self):
        self.assertEqual(self._offline_without_network("Chicago"), self._zone("America/Chicago"))
        self.assertEqual(
            self._offline_without_network("Chicago, Illinois"),
            self._zone("America/Chicago"),
        )
        self.assertEqual(self._offline_without_network("Chicago, US"), self._zone("America/Chicago"))
        self.assertEqual(self._offline_without_network("Tokyo"), self._zone("Asia/Tokyo"))
        self.assertEqual(self._offline_without_network("Japan"), self._zone("Asia/Tokyo"))
        self.assertEqual(self._offline_without_network("United States"), PlaceTz(kind="region"))
        self.assertEqual(self._offline_without_network("Sao Paulo"), self._zone("America/Sao_Paulo"))
        self.assertEqual(self._offline_without_network("São Paulo"), self._zone("America/Sao_Paulo"))
        self.assertEqual(self._offline_without_network("Paris"), self._zone("Europe/Paris"))
        self.assertEqual(self._offline_without_network("Springfield"), PlaceTz(kind="unknown"))
        self.assertIsNone(self._offline_without_network("Chicago, Japan"))
        self.assertIsNone(self._offline_without_network("Zzyzxville"))

    def test_countries_resolve_to_zone_or_region(self):
        self.assertEqual(self._offline_without_network("Japan"), self._zone("Asia/Tokyo"))
        self.assertEqual(self._offline_without_network("United States"), PlaceTz(kind="region"))
        self.assertEqual(self._offline_without_network("Australia"), PlaceTz(kind="region"))

    def test_accented_and_ascii_names_match(self):
        self.assertEqual(self._offline_without_network("São Paulo"), self._zone("America/Sao_Paulo"))
        self.assertEqual(self._offline_without_network("Sao Paulo"), self._zone("America/Sao_Paulo"))

    def test_ambiguous_name_is_unknown_without_network(self):
        # Real GeoNames data: Springfield MO (~170k, America/Chicago) and
        # Springfield MA (~154k, America/New_York) share the name, so neither
        # is a clear winner and the offline table reports unknown.
        self.assertEqual(self._offline_without_network("Springfield"), PlaceTz(kind="unknown"))

    def test_qualifier_can_remove_every_city_row(self):
        with patch(
            "apps.tenants.situation._lookup_place_timezone",
            return_value=PlaceTz(kind="unknown"),
        ) as lookup:
            self.assertIsNone(offline_place_timezone("Chicago, Japan"))
            result = resolve_place_timezone("Chicago, Japan")

        self.assertEqual(result, PlaceTz(kind="unknown"))
        lookup.assert_called_once_with("Chicago, Japan")

    def test_unknown_name_falls_through_to_network(self):
        with patch(
            "apps.tenants.situation._lookup_place_timezone",
            return_value=PlaceTz(kind="unknown"),
        ) as lookup:
            self.assertIsNone(offline_place_timezone("Zzyzxville"))
            result = resolve_place_timezone("Zzyzxville")

        self.assertEqual(result, PlaceTz(kind="unknown"))
        lookup.assert_called_once_with("Zzyzxville")

    def test_offline_hit_is_cached_and_skips_network(self):
        with patch("apps.tenants.situation.httpx.Client", side_effect=AssertionError("network used")) as client:
            result = resolve_place_timezone("Chicago")

        self.assertEqual(result, self._zone("America/Chicago"))
        self.assertEqual(cached_place_timezone("Chicago"), self._zone("America/Chicago"))
        client.assert_not_called()

    def test_loader_failure_logs_warning_and_returns_none(self):
        missing = Path(tempfile.gettempdir()) / "nbhd-missing-place-tz.json.gz"
        if missing.exists():
            missing.unlink()
        _load_place_tz_table.cache_clear()
        with (
            patch("apps.tenants.place_tz_offline.DATA_PATH", missing),
            self.assertLogs("apps.tenants.place_tz_offline", level="WARNING"),
        ):
            self.assertIsNone(offline_place_timezone("Chicago"))


class BuildPlaceTzTableTests(SimpleTestCase):
    @staticmethod
    def _write_geonames(src: Path) -> None:
        (src / "admin1CodesASCII.txt").write_text(
            "ZZ.01\tNorth Region\tNorth Region\t10\n"
            "YY.02\tSouth Region\tSouth Region\t20\n"
            "QQ.03\tTwin Region\tTwin Region\t30\n",
            encoding="utf-8",
        )
        (src / "countryInfo.txt").write_text(
            "#ISO\tISO3\tISO-Numeric\tfips\tCountry\n"
            "ZZ\tZZZ\t999\tZZ\tTestland\tTest City\t1\t1000\tEU\t.zz\tZZD\tDollar\t1\t\t\tzz\t1\t\t\n"
            "YY\tYYY\t998\tYY\tOtherland\tOther City\t1\t1000\tEU\t.yy\tYYD\tDollar\t1\t\t\tyy\t2\t\t\n",
            encoding="utf-8",
        )
        (src / "timeZones.txt").write_text(
            "CountryCode\tTimeZoneId\tGMT offset 1. Jan 2026\n"
            "ZZ\tEurope/Paris\t0.0\n"
            "ZZ\tEurope/London\t0.0\n"
            "YY\tAmerica/New_York\t-5.0\n"
            "YY\tAmerica/Chicago\t-6.0\n",
            encoding="utf-8",
        )
        (src / "cities15000.txt").write_text(
            "1\tAlpha\tAlpha\tIgnoredAlternate\t1\t2\tP\tPPL\tZZ\t\t01\t\t\t\t200000\t\t\tEurope/Paris\t2026-01-01\n"
            "2\tAlpha\tAlpha\t\t1\t2\tP\tPPL\tZZ\t\t01\t\t\t\t50000\t\t\tEurope/Paris\t2026-01-01\n"
            "3\tBeta\tBeta\t\t1\t2\tP\tPPLX\tYY\t\t02\t\t\t\t100000\t\t\tAmerica/New_York\t2026-01-01\n"
            "4\tGamma\tGamma\t\t1\t2\tA\tADM1\tZZ\t\t01\t\t\t\t9\t\t\tEurope/Paris\t2026-01-01\n"
            "5\tKöln\tCologne\t\t1\t2\tP\tPPL\tZZ\t\t01\t\t\t\t1000\t\t\tEurope/Paris\t2026-01-01\n"
            "6\tDelta\tDelta\t\t1\t2\tP\tPPL\tQQ\t\t03\t\t\t\t100000\t\t\tEurope/Paris\t2026-01-01\n"
            "7\tEpsilon\tEpsilon\t\t1\t2\tP\tPPL\tQQ\t\t03\t\t\t\t100000\t\t\tEurope/London\t2026-01-01\n",
            encoding="utf-8",
        )

    def test_builds_expected_json_and_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            self._write_geonames(src)
            out = Path(tmp) / "place_tz.json.gz"
            stdout = StringIO()

            call_command("build_place_tz_table", src=str(src), out=str(out), date="2026-11-01", stdout=stdout)
            first = out.read_bytes()
            call_command("build_place_tz_table", src=str(src), out=str(out), date="2026-11-01", stdout=stdout)
            second = out.read_bytes()

            self.assertEqual(first, second)
            table = json.loads(gzip.decompress(first))

        self.assertEqual(
            table,
            {
                "admins": {
                    "north region": [["ZZ", ["Europe/Paris"]]],
                    "south region": [["YY", ["America/New_York"]]],
                    "twin region": [["QQ", ["Europe/London", "Europe/Paris"]]],
                },
                "cities": {
                    "alpha": [
                        ["ZZ", "North Region", 200000, "Europe/Paris"],
                        ["ZZ", "North Region", 50000, "Europe/Paris"],
                    ],
                    "beta": [["YY", "South Region", 100000, "America/New_York"]],
                    "cologne": [["ZZ", "North Region", 1000, "Europe/Paris"]],
                    "delta": [["QQ", "Twin Region", 100000, "Europe/Paris"]],
                    "epsilon": [["QQ", "Twin Region", 100000, "Europe/London"]],
                    "koln": [["ZZ", "North Region", 1000, "Europe/Paris"]],
                },
                "countries": {
                    "otherland": ["YY", ["America/Chicago", "America/New_York"]],
                    "testland": ["ZZ", ["Europe/London", "Europe/Paris"]],
                },
                "country_names": {"YY": "otherland", "ZZ": "testland"},
                "source": "GeoNames cities15000 + countryInfo + timeZones, CC BY 4.0, built 2026-11-01",
            },
        )


class RedactorOfflineBudgetTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        _load_place_tz_table.cache_clear()
        self.addCleanup(cache.clear)
        self.addCleanup(_load_place_tz_table.cache_clear)

    def test_four_offline_places_need_no_network_budget(self):
        names = ["Chicago", "Tokyo", "São Paulo", "Paris"]
        entity_map = {f"[LOCATION_{index}]": {"name": name} for index, name in enumerate(names, start=1)}
        text = " ".join(entity_map)

        with patch("apps.tenants.situation.httpx.Client", side_effect=AssertionError("network used")) as client:
            result = annotate_model_context(text, entity_map)

        client.assert_not_called()
        self.assertEqual(
            result,
            "[LOCATION_1|tz America/Chicago] [LOCATION_2|tz Asia/Tokyo] "
            "[LOCATION_3|tz America/Sao_Paulo] [LOCATION_4|tz Europe/Paris]",
        )

    def test_offline_names_do_not_consume_network_budget(self):
        # Two offline hits followed by two absent names still leave the whole
        # per-call budget available for the names that need the network.
        names = ["Chicago", "Tokyo", "Zzyzxville", "Qqqville"]
        entity_map = {f"[LOCATION_{index}]": {"name": name} for index, name in enumerate(names, start=1)}
        text = " ".join(entity_map)

        with patch(
            "apps.tenants.situation._lookup_place_timezone",
            return_value=PlaceTz(kind="zone", tz="America/Chicago"),
        ) as lookup:
            result = annotate_model_context(text, entity_map)

        self.assertEqual(lookup.call_count, 2)
        self.assertIn("[LOCATION_1|tz America/Chicago]", result)
        self.assertIn("[LOCATION_2|tz Asia/Tokyo]", result)
        self.assertIn("[LOCATION_3|tz America/Chicago]", result)
        self.assertIn("[LOCATION_4|tz America/Chicago]", result)

    def test_known_ambiguous_place_is_unresolved_without_network(self):
        with patch("apps.tenants.situation.httpx.Client", side_effect=AssertionError("network used")) as client:
            result = annotate_model_context("[LOCATION_1]", {"[LOCATION_1]": {"name": "Springfield"}})

        self.assertEqual(result, "[LOCATION_1|unresolved]")
        client.assert_not_called()

    def test_lookup_exception_leaves_token_unresolved(self):
        with patch(
            "apps.tenants.situation._lookup_place_timezone",
            side_effect=RuntimeError("geocoder down"),
        ):
            result = annotate_model_context("[LOCATION_1]", {"[LOCATION_1]": {"name": "Zzyzxville"}})

        self.assertEqual(result, "[LOCATION_1|unresolved]")
