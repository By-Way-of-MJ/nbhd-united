"""A2UI surface storage helpers: cleaning, cap and rehydration (no database)."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.router.surfaces.store import clean_surfaces, rehydrate_surfaces
from apps.router.surfaces.validate import validate_surface

SENTINEL = "private-sentinel"


def surface(*, surface_id="tonight", text="You slept well", options=("Easy", "Hard"), data_model=None):
    body = {
        "surfaceId": surface_id,
        "catalogId": "nbhd/v1",
        "components": [
            {
                "id": "root",
                "component": "Column",
                "children": ["title", "go", "check", "pick", "when", "slider", "field"],
            },
            {"id": "title", "component": "Text", "text": text},
            {
                "id": "go",
                "component": "Button",
                "label": "Start workout",
                "action": {"event": {"name": "workout.start"}},
            },
            {"id": "check", "component": "CheckBox", "label": "Done?", "value": {"@path": "/done"}},
            {
                "id": "pick",
                "component": "ChoicePicker",
                "label": "How hard",
                "options": list(options),
                "value": {"@path": "/pick"},
            },
            {"id": "when", "component": "DateTimeInput", "label": "When", "value": {"@path": "/when"}},
            {
                "id": "slider",
                "component": "Slider",
                "label": "Energy",
                "min": 0,
                "max": 10,
                "value": {"@path": "/energy"},
            },
            {"id": "field", "component": "TextField", "label": "Note", "value": {"@path": "/note"}},
        ],
    }
    if data_model is not None:
        body["dataModel"] = data_model
    return {"version": "1.0", "createSurface": body}


def bound_surface():
    """Every user-visible string is a binding except the dataModel leaves."""
    body = {
        "surfaceId": "bound",
        "catalogId": "nbhd/v1",
        "components": [
            {"id": "root", "component": "Column", "children": ["title", "pick"]},
            {"id": "title", "component": "Text", "text": {"@path": "/heading"}},
            {
                "id": "pick",
                "component": "ChoicePicker",
                "options": {"@path": "/choices"},
                "value": {"@path": "/choice"},
            },
        ],
        "dataModel": {"heading": "hello there", "choices": ["one", "two"]},
    }
    return {"version": "1.0", "createSurface": body}


def _upper(text, entity_map, *, tenant_id=None, channel=None):
    return text.upper()


class CleanSurfacesTests(SimpleTestCase):
    def test_none_is_empty(self):
        self.assertEqual(clean_surfaces(None), [])

    def test_non_list_is_dropped_with_one_content_free_log(self):
        with self.assertLogs("apps.router.surfaces.store", level="INFO") as logs:
            self.assertEqual(clean_surfaces({SENTINEL: "x"}), [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("a2ui_surface_dropped code=not_list", logs.output[0])
        self.assertNotIn(SENTINEL, logs.output[0])

    def test_valid_surfaces_are_kept_and_normalized(self):
        payload = surface()
        self.assertEqual(clean_surfaces([payload]), [validate_surface(payload)])

    def test_invalid_surface_is_dropped_with_one_content_free_log(self):
        good, bad = surface(), surface(surface_id="Bad Id", text=SENTINEL)
        with self.assertLogs("apps.router.surfaces.store", level="INFO") as logs:
            result = clean_surfaces([good, bad])
        self.assertEqual(result, [validate_surface(good)])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("a2ui_surface_dropped code=bad_envelope", logs.output[0])
        self.assertNotIn(SENTINEL, logs.output[0])

    def test_capped_at_three(self):
        payloads = [surface(surface_id=f"surface{i}") for i in range(5)]
        with self.assertLogs("apps.router.surfaces.store", level="INFO") as logs:
            result = clean_surfaces(payloads)
        self.assertEqual(len(result), 3)
        self.assertEqual([item["createSurface"]["surfaceId"] for item in result], ["surface0", "surface1", "surface2"])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("a2ui_surface_dropped code=limit", logs.output[0])


class RehydrateSurfacesTests(SimpleTestCase):
    def test_rewrites_every_visible_string_and_data_model_leaves(self):
        data_model = {
            "sleep": {"summary": "you slept 5h"},
            "tasks": [{"title": "wash up"}, "loose", 4],
            "count": 2,
            "flag": True,
        }
        payloads = [surface(data_model=data_model)]
        with patch("apps.router.reply_text.finalize_outbound_text", side_effect=_upper) as finalize:
            result = rehydrate_surfaces(payloads, {}, tenant_id="tenant-1")
        body = result[0]["createSurface"]
        by_id = {component["id"]: component for component in body["components"]}
        self.assertEqual(by_id["title"]["text"], "YOU SLEPT WELL")
        self.assertEqual(by_id["go"]["label"], "START WORKOUT")
        self.assertEqual(by_id["check"]["label"], "DONE?")
        self.assertEqual(by_id["pick"]["label"], "HOW HARD")
        self.assertEqual(by_id["pick"]["options"], ["EASY", "HARD"])
        self.assertEqual(by_id["when"]["label"], "WHEN")
        self.assertEqual(by_id["slider"]["label"], "ENERGY")
        self.assertEqual(by_id["field"]["label"], "NOTE")
        self.assertEqual(
            body["dataModel"],
            {
                "sleep": {"summary": "YOU SLEPT 5H"},
                "tasks": [{"title": "WASH UP"}, "LOOSE", 4],
                "count": 2,
                "flag": True,
            },
        )
        self.assertTrue(finalize.call_args_list)
        for call in finalize.call_args_list:
            self.assertEqual(call.kwargs["channel"], "a2ui")
            self.assertEqual(call.kwargs["tenant_id"], "tenant-1")

    def test_bindings_are_skipped(self):
        payload = bound_surface()
        with patch("apps.router.reply_text.finalize_outbound_text", side_effect=_upper) as finalize:
            result = rehydrate_surfaces([payload], {}, tenant_id=None)
        body = result[0]["createSurface"]
        by_id = {component["id"]: component for component in body["components"]}
        self.assertEqual(by_id["title"]["text"], {"@path": "/heading"})
        self.assertEqual(by_id["pick"]["options"], {"@path": "/choices"})
        self.assertEqual(by_id["pick"]["value"], {"@path": "/choice"})
        self.assertEqual(body["dataModel"], {"heading": "HELLO THERE", "choices": ["ONE", "TWO"]})
        # Only the dataModel leaves (heading + two options) go through the seam.
        self.assertEqual(finalize.call_count, 3)

    def test_ref_dicts_survive_rehydration_untouched(self):
        payload = surface(data_model={"tasks": {"$ref": "tasks:due_today"}, "heading": "today"})
        with patch("apps.router.reply_text.finalize_outbound_text", side_effect=_upper):
            result = rehydrate_surfaces([payload], {}, tenant_id=None)
        self.assertEqual(
            result[0]["createSurface"]["dataModel"],
            {"tasks": {"$ref": "tasks:due_today"}, "heading": "TODAY"},
        )

    def test_input_is_not_mutated(self):
        payload = surface(data_model={"sleep": {"summary": "you slept"}})
        payloads = [payload]
        original = deepcopy(payloads)
        with patch("apps.router.reply_text.finalize_outbound_text", side_effect=_upper):
            result = rehydrate_surfaces(payloads, {}, tenant_id=None)
        self.assertEqual(payloads, original)
        self.assertNotEqual(result, original)

    def test_empty_and_invalid_input(self):
        with patch("apps.router.reply_text.finalize_outbound_text", side_effect=_upper):
            self.assertEqual(rehydrate_surfaces(None, {}, tenant_id=None), [])
            self.assertEqual(rehydrate_surfaces("nope", {}, tenant_id=None), [])
            with self.assertLogs("apps.router.surfaces.store", level="INFO"):
                self.assertEqual(rehydrate_surfaces([{SENTINEL: "x"}], {}, tenant_id=None), [])
