"""Model-bound place tokens carry a reliable time zone without the place name."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from apps.pii.redactor import annotate_model_context
from apps.tenants.envelope import _PRIVACY_PLACEHOLDERS_BODY
from apps.tenants.situation import (
    PlaceTz,
    _place_tz_cache_key,
    cached_place_timezone,
    resolve_place_timezone,
)


class PlaceTimezoneAnnotationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @staticmethod
    def _zone(tz: str = "America/Chicago") -> PlaceTz:
        return PlaceTz(kind="zone", tz=tz)

    def test_location_dict_entry_carries_zone(self):
        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch(
                "apps.tenants.situation._lookup_place_timezone",
                return_value=self._zone(),
            ) as lookup,
        ):
            result = annotate_model_context(
                "[LOCATION_1]",
                {"[LOCATION_1]": {"name": "Chicago"}},
            )

        self.assertEqual(result, "[LOCATION_1|tz America/Chicago]")
        lookup.assert_called_once_with("Chicago")

    def test_legacy_string_entry_carries_zone(self):
        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch(
                "apps.tenants.situation._lookup_place_timezone",
                return_value=self._zone(),
            ),
        ):
            result = annotate_model_context("[LOCATION_1]", {"[LOCATION_1]": "Chicago"})

        self.assertEqual(result, "[LOCATION_1|tz America/Chicago]")

    def test_relationship_and_zone_are_joined(self):
        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch(
                "apps.tenants.situation._lookup_place_timezone",
                return_value=self._zone(),
            ),
        ):
            result = annotate_model_context(
                "[LOCATION_1]",
                {"[LOCATION_1]": {"name": "Chicago", "relationship": "home"}},
            )

        self.assertEqual(result, "[LOCATION_1|home; tz America/Chicago]")

    def test_region_and_unknown_descriptors(self):
        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch(
                "apps.tenants.situation._lookup_place_timezone",
                return_value=PlaceTz(kind="region"),
            ),
        ):
            region = annotate_model_context(
                "[LOCATION_1]",
                {"[LOCATION_1]": {"name": "Chicago"}},
            )
        self.assertEqual(region, "[LOCATION_1|region with several time zones]")

        cache.clear()
        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch(
                "apps.tenants.situation._lookup_place_timezone",
                return_value=PlaceTz(kind="unknown"),
            ),
        ):
            unknown = annotate_model_context(
                "[LOCATION_1]",
                {"[LOCATION_1]": {"name": "Chicago"}},
            )
        self.assertEqual(unknown, "[LOCATION_1|unresolved]")

    def test_person_token_does_not_use_place_lookup(self):
        with patch("apps.tenants.situation._lookup_place_timezone") as lookup:
            result = annotate_model_context(
                "[PERSON_1]",
                {"[PERSON_1]": {"name": "Chicago"}},
            )

        self.assertEqual(result, "[PERSON_1|unresolved]")
        lookup.assert_not_called()

    def test_address_like_and_long_names_skip_lookup(self):
        names = ["123 Main St", "1-2-3 Shibuya", "One Two Three Four Five"]
        entity_map = {f"[LOCATION_{index}]": {"name": name} for index, name in enumerate(names, start=1)}
        text = " ".join(entity_map)

        with patch("apps.tenants.situation._lookup_place_timezone") as lookup:
            result = annotate_model_context(text, entity_map)

        lookup.assert_not_called()
        self.assertEqual(
            result,
            "[LOCATION_1|unresolved] [LOCATION_2|unresolved] [LOCATION_3|unresolved]",
        )

    def test_network_lookup_budget_is_two_distinct_names_per_call(self):
        names = ["Chicago", "Boston", "Denver", "Seattle"]
        entity_map = {f"[LOCATION_{index}]": {"name": name} for index, name in enumerate(names, start=1)}
        text = " ".join(entity_map)

        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch(
                "apps.tenants.situation._lookup_place_timezone",
                return_value=self._zone(),
            ) as lookup,
        ):
            result = annotate_model_context(text, entity_map)

        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(
            result,
            "[LOCATION_1|tz America/Chicago] [LOCATION_2|tz America/Chicago] "
            "[LOCATION_3|unresolved] [LOCATION_4|unresolved]",
        )

        # Warm the cache directly, then a second annotation makes no network calls.
        for name in names:
            cache.set(_place_tz_cache_key(name), self._zone(), 300)
        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch("apps.tenants.situation._lookup_place_timezone") as warm_lookup,
        ):
            warm = annotate_model_context(text, entity_map)

        warm_lookup.assert_not_called()
        self.assertNotIn("unresolved", warm)

    @override_settings(PII_PLACE_TZ_ANNOTATION_ENABLED=False)
    def test_setting_disables_zone_annotation(self):
        with patch("apps.tenants.situation._lookup_place_timezone") as lookup:
            result = annotate_model_context(
                "[LOCATION_1]",
                {"[LOCATION_1]": {"name": "Chicago"}},
            )

        self.assertEqual(result, "[LOCATION_1|unresolved]")
        lookup.assert_not_called()

    def test_lookup_exception_leaves_token_unresolved(self):
        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch(
                "apps.tenants.situation._lookup_place_timezone",
                side_effect=RuntimeError("geocoder down"),
            ),
        ):
            result = annotate_model_context(
                "[LOCATION_1]",
                {"[LOCATION_1]": {"name": "Chicago"}},
            )

        self.assertEqual(result, "[LOCATION_1|unresolved]")

    def test_cached_place_timezone_is_cache_only(self):
        self.assertIsNone(cached_place_timezone("Chicago"))

        client = MagicMock()
        client.__enter__.return_value = client
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "results": [
                {
                    "name": "Chicago",
                    "feature_code": "PPL",
                    "timezone": "America/Chicago",
                    "population": 2_700_000,
                }
            ]
        }
        client.get.return_value = response
        with (
            patch("apps.tenants.place_tz_offline.offline_place_timezone", return_value=None),
            patch("apps.tenants.situation.httpx.Client", return_value=client),
        ):
            resolved = resolve_place_timezone("Chicago")

        self.assertEqual(resolved, self._zone())
        with patch("apps.tenants.situation._lookup_place_timezone") as lookup:
            cached = cached_place_timezone("CHICAGO")
            self.assertEqual(cached, self._zone())
            lookup.assert_not_called()

    def test_envelope_rule_mentions_place_zone(self):
        self.assertIn("tz America/Chicago", _PRIVACY_PLACEHOLDERS_BODY)
        self.assertIn("region with several time zones", _PRIVACY_PLACEHOLDERS_BODY)
