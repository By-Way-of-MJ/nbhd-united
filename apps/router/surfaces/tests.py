"""A2UI surface allowlist and validator tests (SimpleTestCase: no database)."""

from __future__ import annotations

import uuid
from typing import get_args
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
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


REF_ID = "123e4567-e89b-42d3-a456-426614174000"
VALID_REFS = (
    "tasks:due_today",
    "tasks:overdue",
    "tasks:this_week",
    "tasks:open",
    f"task:{REF_ID}",
    "workout:today",
    f"workout:{REF_ID}",
    "sleep:last_night",
    "weight:latest",
    f"mission:{REF_ID}",
)


def ref_surface(data_model):
    return surface([{"id": "root", "component": "Divider"}], data_model=data_model)


class SurfaceDataModelTests(SimpleTestCase):
    def assert_code(self, payload, code):
        with self.assertRaises(SurfaceError) as caught:
            validate_surface(payload)
        self.assertEqual(caught.exception.code, code)

    def test_every_ref_kind_is_accepted_and_preserved(self):
        for ref in VALID_REFS:
            with self.subTest(ref=ref):
                data_model = {"items": {"$ref": ref}}
                self.assertEqual(validate_surface(ref_surface(data_model))["createSurface"]["dataModel"], data_model)

    def test_ref_and_plain_values_round_trip(self):
        data_model = {"title": "Today", "tasks": {"$ref": "tasks:due_today"}, "count": 2, "flag": True}
        self.assertEqual(validate_surface(ref_surface(data_model))["createSurface"]["dataModel"], data_model)

    def test_data_model_must_be_a_dict(self):
        self.assert_code(ref_surface(["not", "a", "dict"]), "bad_data")

    def test_ref_dict_must_have_only_ref(self):
        self.assert_code(ref_surface({"x": {"$ref": "tasks:open", "fallback": "none"}}), "bad_data")

    def test_ref_value_must_be_a_string(self):
        self.assert_code(ref_surface({"x": {"$ref": 1}}), "bad_data")

    def test_ref_must_match_the_pattern(self):
        for ref in (
            "tasks:someday",
            "tasks:",
            "tasks",
            "task:not-a-uuid",
            "task:" + REF_ID.upper(),
            "task:" + REF_ID.replace("-", ""),
            "workout:tomorrow",
            "sleep:last_night ",
            "weight:latest\n",
            "mission:",
            "unknown:latest",
            "https://evil.example",
        ):
            with self.subTest(ref=ref):
                self.assert_code(ref_surface({"x": {"$ref": ref}}), "bad_data")

    def test_six_levels_of_nesting_are_allowed_seven_are_not(self):
        def nested(depth):
            node = "leaf"
            for _ in range(depth):
                node = {"a": node}
            return node

        self.assertEqual(validate_surface(ref_surface(nested(6)))["createSurface"]["dataModel"], nested(6))
        self.assert_code(ref_surface(nested(7)), "bad_data")

    def test_nesting_limit_applies_to_lists(self):
        node = "leaf"
        for _ in range(7):
            node = [node]
        self.assert_code(ref_surface({"a": node}), "bad_data")

    def test_at_most_200_leaves(self):
        self.assertEqual(
            len(validate_surface(ref_surface({"items": [0] * 200}))["createSurface"]["dataModel"]["items"]), 200
        )
        self.assert_code(ref_surface({"items": [0] * 201}), "bad_data")

    def test_refs_count_as_leaves(self):
        self.assert_code(ref_surface({"items": [{"$ref": "tasks:open"}] * 201}), "bad_data")

    def test_plain_string_leaf_length(self):
        validate_surface(ref_surface({"text": "a" * 500}))
        self.assert_code(ref_surface({"text": "a" * 501}), "bad_data")

    def test_plain_string_leaf_must_not_be_a_url(self):
        self.assert_code(ref_surface({"text": "see https://evil.example"}), "bad_data")

    def test_data_model_keys_must_be_bounded_token_names(self):
        # Keys are rendered/looked up by the app; they get the same token
        # discipline as component ids, not arbitrary JSON object names.
        validate_surface(ref_surface({"a": 1, "A-b_9": 2, "x" * 64: 3}))
        for key in ("bad key", "", "x" * 65, "$refx", "café", "a/b"):
            with self.subTest(key=key):
                self.assert_code(ref_surface({key: 1}), "bad_data")
        self.assert_code(ref_surface({1: "int keys are not model keys"}), "bad_data")

    def test_nested_data_model_keys_are_checked(self):
        self.assert_code(ref_surface({"outer": {"bad key": 1}}), "bad_data")

    def test_at_most_20_refs(self):
        refs = {f"k{i}": {"$ref": "tasks:open"} for i in range(20)}
        validate_surface(ref_surface(refs))
        refs["k20"] = {"$ref": "tasks:open"}
        self.assert_code(ref_surface(refs), "bad_data")

    def test_non_v4_canonical_uuid_refs_are_accepted(self):
        # UUIDs minted elsewhere (v1/v6/v7) are still canonical lowercase ids;
        # only "not a UUID at all" should be rejected.
        for value in (
            "123e4567-e89b-12d3-a456-426614174000",  # v1
            "1e0a1d6e-3b7c-6d4a-9f2b-0a1c2d3e4f50",  # v6
            "018f4c6e-7b1a-7c3d-8e2f-9a0b1c2d3e4f",  # v7
        ):
            with self.subTest(value=value):
                for prefix in ("task", "workout", "mission"):
                    data_model = {"x": {"$ref": f"{prefix}:{value}"}}
                    self.assertEqual(
                        validate_surface(ref_surface(data_model))["createSurface"]["dataModel"], data_model
                    )


def _button_context(context):
    return surface(
        [
            {
                "id": "root",
                "component": "Button",
                "label": "Go",
                "action": {"event": {"name": "task.move", "context": context}},
            }
        ]
    )


def _panel(kind="tasks", params=None):
    body = {"id": "root", "component": "Panel", "kind": kind}
    if params is not None:
        body["params"] = params
    return surface([body])


class SurfaceStringBoundTests(SimpleTestCase):
    """Every plain string in action context and panel params is a SafeStr."""

    def assert_code(self, payload, code):
        with self.assertRaises(SurfaceError) as caught:
            validate_surface(payload)
        self.assertEqual(caught.exception.code, code)

    def test_action_context_values_are_url_free(self):
        self.assert_code(_button_context({"to": "https://evil.example"}), "schema")

    def test_action_context_values_are_bounded(self):
        validate_surface(_button_context({"to": "a" * 500}))
        self.assert_code(_button_context({"to": "a" * 501}), "schema")

    def test_panel_params_values_are_url_free(self):
        self.assert_code(_panel(params={"filter": "https://evil.example"}), "schema")

    def test_panel_params_values_are_bounded(self):
        validate_surface(_panel(params={"filter": "open"}))
        self.assert_code(_panel(params={"filter": "a" * 501}), "schema")


class SurfacePanelParamsTests(SimpleTestCase):
    """Panel kind+params reuse the panels.py vocabulary, not an open dict."""

    def assert_code(self, payload, code):
        with self.assertRaises(SurfaceError) as caught:
            validate_surface(payload)
        self.assertEqual(caught.exception.code, code)

    def test_valid_panel_params_pass_and_round_trip(self):
        result = validate_surface(_panel(params={"filter": "open"}))
        component = result["createSurface"]["components"][0]
        self.assertEqual(component["kind"], "tasks")
        self.assertEqual(component["params"], {"filter": "open"})
        validate_surface(_panel(kind="timer", params={"duration_seconds": 600}))

    def test_unknown_param_key_is_rejected(self):
        self.assert_code(_panel(params={"bogus": "x"}), "schema")

    def test_param_value_must_match_the_panel_vocabulary(self):
        self.assert_code(_panel(params={"filter": "someday"}), "schema")

    def test_param_scoped_to_another_kind_is_rejected(self):
        self.assert_code(_panel(kind="workout", params={"filter": "open"}), "schema")


class SurfaceGraphScopeTests(SimpleTestCase):
    """Relative bindings are template-only; the graph under root is closed."""

    def assert_code(self, payload, code):
        with self.assertRaises(SurfaceError) as caught:
            validate_surface(payload)
        self.assertEqual(caught.exception.code, code)

    def test_orphan_list_with_template_root_cannot_unlock_relative_bindings(self):
        # Exact bypass: an unreachable List used to add root (and everything
        # under it) to the template set, so a relative pointer on a normal node
        # passed. The List-template edge must never point into the direct graph.
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["title"]},
                {"id": "title", "component": "Text", "text": {"@path": "title"}},
                {"id": "ghost-list", "component": "List", "children": {"template": "root", "path": "/x"}},
            ]
        )
        self.assert_code(payload, "bad_template")

    def test_list_template_must_not_be_in_the_direct_graph(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["lst", "title"]},
                {"id": "lst", "component": "List", "children": {"template": "title", "path": "/x"}},
                {"id": "title", "component": "Text", "text": "Hi"},
            ]
        )
        self.assert_code(payload, "bad_template")

    def test_relative_binding_on_a_direct_node_is_rejected(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["title"]},
                {"id": "title", "component": "Text", "text": {"@path": "title"}},
            ]
        )
        self.assert_code(payload, "bad_template")

    def test_unreachable_component_is_an_orphan(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["title"]},
                {"id": "title", "component": "Divider"},
                {"id": "extra", "component": "Text", "text": "never rendered"},
            ]
        )
        self.assert_code(payload, "orphan")

    def test_duplicate_child_id_is_rejected(self):
        payload = surface(
            [
                {"id": "root", "component": "Column", "children": ["item", "item"]},
                {"id": "item", "component": "Divider"},
            ]
        )
        self.assert_code(payload, "duplicate_child")


class PostDemoSurfaceCommandTests(SimpleTestCase):
    """The operator command reports expected failures without a traceback."""

    def _call(self):
        return call_command(
            "post_demo_surface",
            tenant=str(uuid.uuid4()),
            scenario="gym",
        )

    def test_surface_error_is_a_clean_command_error(self):
        with (
            patch(
                "apps.router.management.commands.post_demo_surface.post_demo_surface",
                side_effect=SurfaceError("schema"),
            ),
            self.assertRaisesMessage(CommandError, "post_demo_surface failed: schema"),
        ):
            self._call()

    def test_value_error_is_a_clean_command_error(self):
        with (
            patch(
                "apps.router.management.commands.post_demo_surface.post_demo_surface",
                side_effect=ValueError("mission_id required"),
            ),
            self.assertRaisesMessage(CommandError, "post_demo_surface failed: mission_id required"),
        ):
            self._call()
