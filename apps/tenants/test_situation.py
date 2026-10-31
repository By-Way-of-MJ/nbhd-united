"""Tests for the sole structured UserSituation write service."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.orchestrator.envelope_registry import suppress_refresh
from apps.tenants.models import Tenant, User, UserSituation
from apps.tenants.situation import PlaceTz, record_device_tz, record_place_observation, resolve_place_timezone


class SituationServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="situation-service", password="pw")
        self.user.location_city = " Tokyo "
        self.user.save(update_fields=["location_city"])
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            situational_context_enabled=True,
        )
        self.now = timezone.now().replace(microsecond=0)

    def _place(self, label, source="ios_chat", observed_at=None):
        with suppress_refresh():
            return record_place_observation(self.tenant, label, source, observed_at)

    def _device_tz(self, tz_name, source_device="healthkit", observed_at=None):
        with suppress_refresh():
            return record_device_tz(self.tenant, tz_name, source_device, observed_at)

    def test_label_validation_rejects_unsafe_values_and_accepts_clean_label(self):
        rejected = [
            "",
            "   ",
            "a" * 65,
            "Fukuoka\nJapan",
            "Fukuoka\u2028Japan",
            "Fukuoka\x00Japan",
            "#Fukuoka",
            "Fu*kuoka",
            "Fu_kuoka",
            "Fu[kuoka",
            "Fu~kuoka",
        ]
        for label in rejected:
            with self.subTest(label=repr(label)):
                self.assertFalse(self._place(label, observed_at=self.now))
                self.assertFalse(UserSituation.objects.exists())

        self.assertTrue(self._place("  Fukuoka  ", observed_at=self.now))
        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.current_place_label, "Fukuoka")
        self.assertEqual(situation.current_place_source, "ios_chat")

    def test_same_label_updates_last_observed_only_and_throttles_writes(self):
        self.assertTrue(self._place("Fukuoka", observed_at=self.now))
        situation = UserSituation.objects.get(tenant=self.tenant)
        original_since = situation.current_place_since
        original_source = situation.current_place_source

        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(self._place("Fukuoka", source="other", observed_at=self.now + timedelta(minutes=5)))
        situation_writes = [
            q["sql"]
            for q in queries.captured_queries
            if "user_situations" in q["sql"] and q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT"))
        ]
        self.assertEqual(situation_writes, [])

        observed_later = self.now + timedelta(minutes=11)
        self.assertFalse(self._place("Fukuoka", source="other", observed_at=observed_later))
        situation.refresh_from_db()
        self.assertEqual(situation.current_place_last_observed_at, observed_later)
        self.assertEqual(situation.current_place_since, original_since)
        self.assertEqual(situation.current_place_source, original_source)

    def test_new_label_resets_since_and_source(self):
        self._place("Fukuoka", observed_at=self.now)
        moved_at = self.now + timedelta(hours=3)
        self.assertTrue(self._place("Osaka", source="ios_chat", observed_at=moved_at))
        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.current_place_label, "Osaka")
        self.assertEqual(situation.current_place_since, moved_at)
        self.assertEqual(situation.current_place_last_observed_at, moved_at)

    def test_device_timezone_rejects_out_of_order_observation(self):
        self.assertTrue(self._device_tz("Asia/Tokyo", observed_at=self.now))
        self.assertFalse(self._device_tz("America/New_York", observed_at=self.now - timedelta(seconds=1)))
        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.device_tz, "Asia/Tokyo")
        self.assertEqual(situation.device_tz_last_observed_at, self.now)

    def test_device_timezone_change_clears_dismissed_offer(self):
        self.assertTrue(self._device_tz("America/New_York", observed_at=self.now))
        situation = UserSituation.objects.get(tenant=self.tenant)
        situation.tz_offer_dismissed_for = "America/New_York"
        situation.save(update_fields=["tz_offer_dismissed_for"])

        self.assertTrue(self._device_tz("Asia/Tokyo", observed_at=self.now + timedelta(hours=1)))
        situation.refresh_from_db()
        self.assertEqual(situation.device_tz, "Asia/Tokyo")
        self.assertEqual(situation.tz_offer_dismissed_for, "")

    def test_same_device_timezone_reobservation_keeps_dismissed_offer(self):
        self.assertTrue(self._device_tz("America/New_York", observed_at=self.now))
        situation = UserSituation.objects.get(tenant=self.tenant)
        situation.tz_offer_dismissed_for = "America/New_York"
        situation.save(update_fields=["tz_offer_dismissed_for"])

        observed_again = self.now + timedelta(minutes=11)
        self.assertFalse(self._device_tz("America/New_York", observed_at=observed_again))
        situation.refresh_from_db()
        self.assertEqual(situation.device_tz_last_observed_at, observed_again)
        self.assertEqual(situation.tz_offer_dismissed_for, "America/New_York")

    def test_invalid_iana_timezone_is_debug_noop(self):
        with self.assertLogs("apps.tenants.situation", level="DEBUG") as logs:
            self.assertFalse(self._device_tz("Not/AZone", observed_at=self.now))
        self.assertIn("situation_device_tz_invalid", "\n".join(logs.output))
        self.assertFalse(UserSituation.objects.exists())

    def test_flag_off_and_eval_sink_do_not_write(self):
        self.tenant.situational_context_enabled = False
        self.assertFalse(self._place("Fukuoka", observed_at=self.now))
        self.assertFalse(self._device_tz("Asia/Tokyo", observed_at=self.now))
        self.assertFalse(UserSituation.objects.exists())

        self.tenant.situational_context_enabled = True
        self.tenant.is_eval_sink = True
        self.assertFalse(self._place("Fukuoka", observed_at=self.now))
        self.assertFalse(self._device_tz("Asia/Tokyo", observed_at=self.now))
        self.assertFalse(UserSituation.objects.exists())

    def test_place_log_reports_differs_home_without_label(self):
        with self.assertLogs("apps.tenants.situation", level="INFO") as same_logs:
            self.assertTrue(self._place("tokyo", observed_at=self.now))
        same_line = "\n".join(same_logs.output)
        self.assertIn("differs_home=0", same_line)
        self.assertNotIn("tokyo", same_line.lower())

        with self.assertLogs("apps.tenants.situation", level="INFO") as away_logs:
            self.assertTrue(self._place("Fukuoka", observed_at=self.now + timedelta(hours=1)))
        away_line = "\n".join(away_logs.output)
        self.assertIn("differs_home=1", away_line)
        self.assertNotIn("fukuoka", away_line.lower())


class PlaceTimezoneResolveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @staticmethod
    def _geocode_client(results: list[dict]) -> MagicMock:
        client = MagicMock()
        client.__enter__.return_value = client
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"results": results}
        client.get.return_value = response
        return client

    def test_non_assignable_timezone_is_unknown(self):
        client = self._geocode_client(
            [
                {
                    "name": "Nowhere",
                    "feature_code": "PPL",
                    "timezone": "Etc/GMT+5",
                    "country_code": "XX",
                    "population": 200000,
                }
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("Nowhere")

        self.assertEqual(result, PlaceTz(kind="unknown"))
        self.assertEqual(result.tz, "")

    def test_empty_results_are_unknown(self):
        client = self._geocode_client([])
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("Atlantis")

        self.assertEqual(result, PlaceTz(kind="unknown"))
        self.assertEqual(result.tz, "")

    def test_ambiguous_country_label_is_unknown(self):
        client = self._geocode_client(
            [
                {
                    "name": "Americana",
                    "feature_code": "PPL",
                    "timezone": "America/Sao_Paulo",
                    "country_code": "BR",
                    "population": 246655,
                },
                {
                    "name": "America",
                    "feature_code": "PPL",
                    "timezone": "Europe/Amsterdam",
                    "country_code": "NL",
                    "population": 1175,
                },
                {
                    "name": "América",
                    "feature_code": "PPL",
                    "timezone": "America/Argentina/Buenos_Aires",
                    "country_code": "AR",
                    "population": 13619,
                },
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("America")

        self.assertEqual(result, PlaceTz(kind="unknown"))

    def test_country_code_label_without_a_populous_exact_match_is_unknown(self):
        client = self._geocode_client(
            [
                {
                    "name": "Uşak",
                    "feature_code": "PPLA",
                    "timezone": "Europe/Istanbul",
                    "country_code": "TR",
                    "population": 369433,
                },
                {
                    "name": "Usa",
                    "feature_code": "PPLA2",
                    "timezone": "Asia/Tokyo",
                    "country_code": "JP",
                    "population": None,
                },
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("USA")

        self.assertEqual(result, PlaceTz(kind="unknown"))

    def test_small_state_name_matches_are_unknown(self):
        client = self._geocode_client(
            [
                {
                    "name": "California",
                    "feature_code": "PPLA2",
                    "timezone": "America/Chicago",
                    "country_code": "US",
                    "admin1": "Missouri",
                    "population": 4396,
                },
                {
                    "name": "California",
                    "feature_code": "PPL",
                    "timezone": "America/New_York",
                    "country_code": "US",
                    "admin1": "Pennsylvania",
                    "population": 11857,
                },
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("California")

        self.assertEqual(result, PlaceTz(kind="unknown"))

    def test_two_populous_same_name_cities_are_unknown(self):
        client = self._geocode_client(
            [
                {
                    "name": "Springfield",
                    "feature_code": "PPL",
                    "timezone": "America/Chicago",
                    "country_code": "US",
                    "population": 170188,
                },
                {
                    "name": "Springfield",
                    "feature_code": "PPL",
                    "timezone": "America/New_York",
                    "country_code": "US",
                    "population": 154341,
                },
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("Springfield")

        self.assertEqual(result, PlaceTz(kind="unknown"))

    def test_clear_populous_exact_match_wins(self):
        client = self._geocode_client(
            [
                {
                    "name": "Portland",
                    "feature_code": "PPL",
                    "timezone": "America/Los_Angeles",
                    "country_code": "US",
                    "population": 652503,
                },
                {
                    "name": "Portland",
                    "feature_code": "PPL",
                    "timezone": "America/New_York",
                    "country_code": "US",
                    "population": 66881,
                },
                {
                    "name": "Portland",
                    "feature_code": "PPL",
                    "timezone": "America/Chicago",
                    "country_code": "US",
                    "population": 16116,
                },
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("Portland")

        self.assertEqual(result, PlaceTz(kind="zone", tz="America/Los_Angeles"))

    def test_qualified_match_below_the_population_floor_is_unknown(self):
        client = self._geocode_client(
            [
                {
                    "name": "Portland",
                    "feature_code": "PPL",
                    "timezone": "America/Los_Angeles",
                    "country_code": "US",
                    "admin1": "Oregon",
                    "population": 652503,
                },
                {
                    "name": "Portland",
                    "feature_code": "PPL",
                    "timezone": "America/New_York",
                    "country_code": "US",
                    "admin1": "Maine",
                    "population": 66881,
                },
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("Portland, Maine")

        self.assertEqual(result, PlaceTz(kind="unknown"))

    def test_qualified_country_match_wins_with_its_city(self):
        client = self._geocode_client(
            [
                {
                    "name": "Paris",
                    "feature_code": "PPLC",
                    "timezone": "Europe/Paris",
                    "country_code": "FR",
                    "admin1": "Île-de-France",
                    "country": "France",
                    "population": 2138551,
                },
                {
                    "name": "Paris",
                    "feature_code": "PPL",
                    "timezone": "America/Chicago",
                    "country_code": "US",
                    "admin1": "Texas",
                    "country": "United States",
                    "population": 24782,
                },
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("Paris, France")

        self.assertEqual(result, PlaceTz(kind="zone", tz="Europe/Paris"))

    def test_country_is_a_region(self):
        client = self._geocode_client(
            [{"name": "United States", "feature_code": "PCLI", "timezone": None, "country_code": "US"}]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("United States")

        self.assertEqual(result, PlaceTz(kind="region"))

    def test_single_zone_country_resolves_to_its_zone(self):
        client = self._geocode_client(
            [{"name": "Japan", "feature_code": "PCLI", "timezone": "Asia/Tokyo", "country_code": "JP"}]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("Japan")

        self.assertEqual(result, PlaceTz(kind="zone", tz="Asia/Tokyo"))

    def test_country_with_null_timezone_resolves_to_region(self):
        client = self._geocode_client(
            [{"name": "Canada", "feature_code": "PCLI", "timezone": None, "country_code": "CA"}]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("Canada")

        self.assertEqual(result, PlaceTz(kind="region"))

    def test_exact_name_match_beats_a_smaller_partial_match(self):
        client = self._geocode_client(
            [
                {
                    "name": "New York",
                    "feature_code": "PPL",
                    "timezone": "America/New_York",
                    "country_code": "US",
                    "population": 8804190,
                },
                {
                    "name": "York",
                    "feature_code": "PPL",
                    "timezone": "America/New_York",
                    "country_code": "US",
                    "population": 7864,
                },
            ]
        )
        with patch("apps.tenants.situation.httpx.Client", return_value=client):
            result = resolve_place_timezone("New York")

        self.assertEqual(result, PlaceTz(kind="zone", tz="America/New_York"))
