"""A2UI surface allowlist and validator tests (SimpleTestCase: no database)."""

from __future__ import annotations

from typing import get_args

from django.test import SimpleTestCase

from apps.router.panels import PANEL_KINDS
from apps.router.surfaces import ALLOWED_ACTIONS, CATALOG_ID, SurfaceError, validate_surface
from apps.router.surfaces.catalog import ALLOWED_ACTION_NAMES, PanelKind

EXAMPLE = {
    "version": "1.0",
    "createSurface": {
        "surfaceId": "tonight",
        "catalogId": "nbhd/v1",
        "components": [
            {"id": "root", "component": "Column", "children": ["title", "why", "lifts", "start", "energy"]},
            {"id": "title", "component": "Text", "text": "Tonight, go easier"},
            {"id": "why", "component": "Text", "text": {"@path": "/sleep/summary"}},
            {"id": "lifts", "component": "Panel", "kind": "workout", "ref": {"@path": "/workout/ref"}},
            {
                "id": "start",
                "component": "Button",
                "label": "Start workout",
                "action": {"event": {"name": "workout.start", "context": {"workout": {"@path": "/workout/ref"}}}},
            },
            {
                "id": "energy",
                "component": "Slider",
                "label": "Energy right now",
                "min": 1,
                "max": 10,
                "value": {"@path": "/checkin/energy"},
            },
        ],
        "dataModel": {
            "sleep": {"summary": "You slept 5h 6m."},
            "workout": {"ref": "fuel:workout/abc"},
            "checkin": {"energy": 4},
        },
    },
}


def surface(components, *, surface_id="tonight", catalog_id=CATALOG_ID, data_model=None, version="1.0"):
    body = {"surfaceId": surface_id, "catalogId": catalog_id, "components": components}
    if data_model is not None:
        body["dataModel"] = data_model
    return {"version": version, "createSurface": body}


class SurfaceValidationTests(SimpleTestCase):
    def assert_code(self, payload, code):
        with self.assertRaises(SurfaceError) as caught:
            validate_surface(payload)
        self.assertEqual(caught.exception.code, code)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(str(caught.exception), code)

    def test_example_passes_and_round_trips(self):
        result = validate_surface(EXAMPLE)
        self.assertEqual(result["version"], "1.0")
        body = result["createSurface"]
        self.assertEqual(body["surfaceId"], "tonight")
        self.assertEqual(body["catalogId"], CATALOG_ID)
        self.assertEqual(body["dataModel"], EXAMPLE["createSurface"]["dataModel"])
        by_id = {component["id"]: component for component in body["components"]}
        self.assertEqual(by_id["why"]["text"], {"@path": "/sleep/summary"})
        self.assertEqual(by_id["why"]["style"], "body")
        self.assertEqual(by_id["start"]["variant"], "secondary")
        self.assertEqual(by_id["start"]["action"]["event"]["context"], {"workout": {"@path": "/workout/ref"}})
        self.assertEqual(by_id["energy"]["value"], {"@path": "/checkin/energy"})
        self.assertEqual(validate_surface(result), result)

    def test_bad_version(self):
        self.assert_code({**EXAMPLE, "version": "1.1"}, "bad_version")

    def test_bad_envelope(self):
        self.assert_code({"version": "1.0", "updateSurface": EXAMPLE["createSurface"]}, "bad_envelope")

    def test_bad_envelope_surface_id(self):
        self.assert_code(surface([{"id": "root", "component": "Divider"}], surface_id="Tonight!"), "bad_envelope")

    def test_bad_catalog(self):
        self.assert_code(surface([{"id": "root", "component": "Divider"}], catalog_id="nbhd/v2"), "bad_catalog")

    def test_schema(self):
        self.assert_code(surface([{"id": "root", "component": "Text"}]), "schema")

    def test_duplicate_id(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["item"]},
                {"id": "item", "component": "Divider"},
                {"id": "item", "component": "Divider"},
            ]
        )
        self.assert_code(payload, "duplicate_id")

    def test_missing_root(self):
        self.assert_code(surface([{"id": "title", "component": "Text", "text": "Hi"}]), "missing_root")

    def test_dangling_child(self):
        payload = surface([{"id": "root", "component": "Column", "children": ["ghost"]}])
        self.assert_code(payload, "dangling_child")

    def test_dangling_template(self):
        payload = surface([{"id": "root", "component": "List", "children": {"template": "ghost", "path": "/tasks"}}])
        self.assert_code(payload, "dangling_child")

    def test_cycle(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["child"]},
                {"id": "child", "component": "Column", "children": ["root"]},
            ]
        )
        self.assert_code(payload, "cycle")

    def test_too_deep(self):
        components = [{"id": "root", "component": "Column", "children": ["n1"]}]
        for level in range(1, 8):
            components.append({"id": f"n{level}", "component": "Column", "children": [f"n{level + 1}"]})
        components.append({"id": "n8", "component": "Divider"})
        self.assert_code(surface(components), "too_deep")

    def test_too_many(self):
        components = [{"id": "root", "component": "Column", "children": ["c0"]}]
        components.extend({"id": f"c{index}", "component": "Divider"} for index in range(60))
        self.assert_code(surface(components), "too_many")

    def test_too_large(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["text"]},
                {"id": "text", "component": "Text", "text": "a" * 33000},
            ]
        )
        self.assert_code(payload, "too_large")

    def test_bad_template(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["title"]},
                {"id": "title", "component": "Text", "text": {"@path": "title"}},
            ]
        )
        self.assert_code(payload, "bad_template")

    def test_url_string_is_rejected(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["text"]},
                {"id": "text", "component": "Text", "text": "https://evil.example"},
            ]
        )
        self.assert_code(payload, "schema")

    def test_unknown_action_is_rejected(self):
        payload = surface(
            [
                {
                    "id": "root",
                    "component": "Button",
                    "label": "Go",
                    "action": {"event": {"name": "workout.stop"}},
                }
            ]
        )
        self.assert_code(payload, "schema")

    def test_unknown_panel_kind_is_rejected(self):
        self.assert_code(surface([{"id": "root", "component": "Panel", "kind": "unknown"}]), "schema")

    def test_binding_with_extra_key_is_rejected(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["text"]},
                {"id": "text", "component": "Text", "text": {"@path": "/x", "fallback": "y"}},
            ]
        )
        self.assert_code(payload, "schema")

    def test_relative_path_is_accepted_inside_list_template(self):
        payload = surface(
            [
                {"id": "root", "component": "List", "children": {"template": "item", "path": "/tasks"}},
                {"id": "item", "component": "Text", "text": {"@path": "title"}},
            ]
        )
        result = validate_surface(payload)
        by_id = {component["id"]: component for component in result["createSurface"]["components"]}
        self.assertEqual(by_id["item"]["text"], {"@path": "title"})

    def test_relative_path_is_rejected_as_list_path(self):
        payload = surface([{"id": "root", "component": "List", "children": {"template": "item", "path": "tasks"}}])
        self.assert_code(payload, "schema")

    def test_action_vocabulary_matches_literal(self):
        self.assertEqual(get_args(ALLOWED_ACTIONS), ALLOWED_ACTION_NAMES)
        self.assertEqual(get_args(PanelKind), PANEL_KINDS)
        for name in ALLOWED_ACTION_NAMES:
            with self.subTest(action=name):
                result = validate_surface(
                    surface([{"id": "root", "component": "Button", "label": "Go", "action": {"event": {"name": name}}}])
                )
                self.assertEqual(
                    result["createSurface"]["components"][0]["action"]["event"]["name"],
                    name,
                )
