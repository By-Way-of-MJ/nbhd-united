"""Fenced A2UI card extraction: stripping, validation and content-free logs."""

from __future__ import annotations

import json

from django.test import SimpleTestCase

from apps.router.panels import extract_panels, strip_streaming_panels
from apps.router.surfaces.extract import extract_card

CARD = {
    "version": "1.0",
    "createSurface": {
        "surfaceId": "card",
        "catalogId": "nbhd/v1",
        "components": [{"id": "root", "component": "Divider"}],
    },
}

PANEL_BLOCK = "```nbhd-panels\n" + json.dumps([{"kind": "sleep", "params": {"range": "last_night"}}]) + "\n```"


def card_block(payload=CARD):
    return "```nbhd-card\n" + json.dumps(payload) + "\n```"


def url_card():
    body = {
        "surfaceId": "unsafe",
        "catalogId": "nbhd/v1",
        "components": [
            {"id": "root", "component": "Column", "children": ["title"]},
            {"id": "title", "component": "Text", "text": "https://private-sentinel.example"},
        ],
    }
    return {"version": "1.0", "createSurface": body}


class ExtractCardTests(SimpleTestCase):
    def test_valid_block_is_stripped_and_returned(self):
        text, surfaces = extract_card("Before\n" + card_block() + "\nAfter")

        self.assertEqual(text, "Before\nAfter")
        self.assertEqual(len(surfaces), 1)
        self.assertEqual(surfaces[0]["createSurface"]["surfaceId"], "card")

    def test_malformed_json_is_stripped_and_logged_without_content(self):
        source = "Before\n```nbhd-card\nprivate-sentinel\n```\nAfter"

        with self.assertLogs("apps.router.surfaces.extract", level="WARNING") as logs:
            text, surfaces = extract_card(source)

        self.assertEqual((text, surfaces), ("Before\nAfter", []))
        self.assertIn("card_dropped reason=malformed_json", "\n".join(logs.output))
        self.assertNotIn("private-sentinel", "\n".join(logs.output))

    def test_schema_invalid_envelope_is_stripped_and_logged_with_code(self):
        invalid = {
            "version": "1.0",
            "createSurface": {
                "surfaceId": "bad",
                "catalogId": "nbhd/v1",
                "components": [{"id": "root", "component": "Text"}],
            },
        }
        source = "Before\n" + card_block(invalid) + "\nAfter"

        with self.assertLogs("apps.router.surfaces.extract", level="WARNING") as logs:
            text, surfaces = extract_card(source)

        self.assertEqual((text, surfaces), ("Before\nAfter", []))
        self.assertIn("card_dropped reason=schema", "\n".join(logs.output))

    def test_url_in_text_is_stripped_and_logged_without_content(self):
        source = "Before\n" + card_block(url_card()) + "\nAfter"

        with self.assertLogs("apps.router.surfaces.extract", level="WARNING") as logs:
            text, surfaces = extract_card(source)

        self.assertEqual((text, surfaces), ("Before\nAfter", []))
        self.assertIn("card_dropped reason=schema", "\n".join(logs.output))
        self.assertNotIn("private-sentinel.example", "\n".join(logs.output))

    def test_two_blocks_keep_first_and_strip_both(self):
        second = {
            "version": "1.0",
            "createSurface": {
                "surfaceId": "second",
                "catalogId": "nbhd/v1",
                "components": [{"id": "root", "component": "Divider"}],
            },
        }
        source = "Before\n" + card_block() + "\n" + card_block(second) + "\nAfter"

        with self.assertLogs("apps.router.surfaces.extract", level="WARNING") as logs:
            text, surfaces = extract_card(source)

        self.assertEqual(text, "Before\nAfter")
        self.assertEqual([surface["createSurface"]["surfaceId"] for surface in surfaces], ["card"])
        self.assertIn("card_dropped reason=extra", "\n".join(logs.output))

    def test_unclosed_block_strips_to_end_and_logs(self):
        source = "Before\n```nbhd-card\n" + json.dumps(CARD)

        with self.assertLogs("apps.router.surfaces.extract", level="WARNING") as logs:
            text, surfaces = extract_card(source)

        self.assertEqual((text, surfaces), ("Before", []))
        self.assertIn("card_dropped reason=unclosed", "\n".join(logs.output))

    def test_no_block_is_unchanged(self):
        source = "  Ordinary text\nwith trailing space \n"
        self.assertEqual(extract_card(source), (source, []))

    def test_card_and_panels_extract_independently(self):
        source = "Before\n" + PANEL_BLOCK + "\n" + card_block() + "\nAfter"

        card_text, surfaces = extract_card(source)
        panel_text, panels = extract_panels(source)

        self.assertEqual(len(surfaces), 1)
        self.assertEqual([panel["kind"] for panel in panels], ["sleep"])
        self.assertIn("nbhd-panels", card_text)
        self.assertIn("nbhd-card", panel_text)
        self.assertEqual(extract_panels(card_text)[0], "Before\nAfter")
        self.assertEqual(extract_card(panel_text)[0], "Before\nAfter")

    def test_streaming_hides_full_card_and_partial_opener(self):
        self.assertEqual(strip_streaming_panels("Before\n" + card_block() + "\nAfter"), "Before\nAfter")
        for partial in ("```nbhd-", "```nbhd-ca", "```nbhd-card"):
            with self.subTest(partial=partial):
                self.assertEqual(strip_streaming_panels("Before\n" + partial), "Before")
