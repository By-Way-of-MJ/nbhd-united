"""The per-turn A2UI card instruction is complete and stays within budget."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.router.panels import CHAT_PANEL_INSTRUCTION
from apps.router.services import _panel_chat_context, build_chat_context_marker
from apps.router.surfaces.catalog import (
    ALLOWED_ACTION_NAMES,
    CATALOG_ID,
    COMPONENT_ID_PATTERN,
    REF_KINDS,
    TASK_LISTS,
    Button,
    ChoicePicker,
    Component,
    DateTimeInput,
    Panel,
    Slider,
    Text,
)
from apps.router.surfaces.instruction import (
    _ACTION_CONTEXTS,
    CARD_INSTRUCTION,
    SMALL_EXAMPLE,
    build_small_example,
)
from apps.router.surfaces.validate import SurfaceError, validate_surface

MAX_INSTRUCTION_CHARS = 2600
MAX_EXAMPLE_CHARS = 700


def embedded_example() -> dict:
    opener = "```nbhd-card\n"
    start = CARD_INSTRUCTION.index(opener) + len(opener)
    end = CARD_INSTRUCTION.index("\n```", start)
    return json.loads(CARD_INSTRUCTION[start:end])


def literal_values(model, field_name: str) -> tuple[str, ...]:
    return get_args(model.model_fields[field_name].annotation)


def bounds(model, field_name: str) -> tuple[int, int]:
    ge = le = None
    for constraint in model.model_fields[field_name].metadata:
        if hasattr(constraint, "ge"):
            ge = constraint.ge
        if hasattr(constraint, "le"):
            le = constraint.le
    return ge, le


def minimal_surface(component: dict) -> dict:
    return {
        "version": "1.0",
        "createSurface": {
            "surfaceId": "values",
            "catalogId": CATALOG_ID,
            "components": [{"id": "root", **component}],
        },
    }


class CardInstructionTests(SimpleTestCase):
    def assert_rendered_and_valid(self, value: str, component: dict) -> None:
        self.assertIn(value, CARD_INSTRUCTION)
        validate_surface(minimal_surface(component))

    def test_every_action_is_documented(self):
        for name in ALLOWED_ACTION_NAMES:
            with self.subTest(action=name):
                self.assertIn(name, CARD_INSTRUCTION)

    def test_action_context_map_covers_every_allowed_action(self):
        self.assertEqual(set(_ACTION_CONTEXTS), set(ALLOWED_ACTION_NAMES))

    def test_every_component_class_is_documented(self):
        for model in get_args(get_args(Component)[0]):
            with self.subTest(component=model.__name__):
                self.assertIn(model.__name__, CARD_INSTRUCTION)

    def test_every_ref_kind_and_task_list_is_documented(self):
        for kind in REF_KINDS:
            with self.subTest(ref_kind=kind):
                self.assertIn(f"{kind}:", CARD_INSTRUCTION)
        for task_list in TASK_LISTS:
            with self.subTest(task_list=task_list):
                self.assertIn(task_list, CARD_INSTRUCTION)

    def test_reference_forms_are_plain_alternatives_with_an_explicit_wrapper(self):
        self.assertIn('{"$ref":"<form>"}', CARD_INSTRUCTION)
        self.assertIn("tasks:due_today | tasks:overdue", CARD_INSTRUCTION)
        self.assertNotIn("tasks:{", CARD_INSTRUCTION)

    def test_component_id_pattern_is_rendered(self):
        self.assertIn(COMPONENT_ID_PATTERN, CARD_INSTRUCTION)
        self.assertIn("lowercase", CARD_INSTRUCTION)

    def test_list_children_shape_is_rendered(self):
        self.assertIn('{template:<id>,path:"/abs"}', CARD_INSTRUCTION)

    def test_every_text_style_renders_and_validates(self):
        for style in literal_values(Text, "style"):
            with self.subTest(style=style):
                self.assert_rendered_and_valid(style, {"component": "Text", "text": "Hi", "style": style})

    def test_every_button_variant_renders_and_validates(self):
        for variant in literal_values(Button, "variant"):
            with self.subTest(variant=variant):
                self.assert_rendered_and_valid(
                    variant,
                    {
                        "component": "Button",
                        "label": "Go",
                        "variant": variant,
                        "action": {"event": {"name": "card.dismiss"}},
                    },
                )

    def test_every_datetime_input_mode_renders_and_validates(self):
        for mode in literal_values(DateTimeInput, "mode"):
            with self.subTest(mode=mode):
                self.assert_rendered_and_valid(
                    mode,
                    {
                        "component": "DateTimeInput",
                        "value": {"@path": "/when"},
                        "mode": mode,
                    },
                )

    def test_every_panel_kind_renders_and_validates_with_no_params(self):
        for kind in literal_values(Panel, "kind"):
            with self.subTest(kind=kind):
                self.assert_rendered_and_valid(kind, {"component": "Panel", "kind": kind})

    def test_slider_bounds_and_order_are_rendered(self):
        for field_name in ("min", "max"):
            lo, hi = bounds(Slider, field_name)
            with self.subTest(field=field_name):
                self.assertIn(f"{field_name}={lo}..{hi}", CARD_INSTRUCTION)
        self.assertIn("min<max", CARD_INSTRUCTION)

    def test_choice_picker_single_select_meaning_is_rendered(self):
        lo, hi = bounds(ChoicePicker, "max")
        self.assertIn(f"max?={lo}..{hi}", CARD_INSTRUCTION)
        self.assertIn(f"{lo}=single select", CARD_INSTRUCTION)

    def test_action_context_types_are_rendered(self):
        self.assertIn("weight.log{kg:number}", CARD_INSTRUCTION)
        self.assertIn('{"picked":{"@path":"/focus/picked"}}', CARD_INSTRUCTION)

    def test_small_example_is_built_and_validates(self):
        self.assertEqual(build_small_example(), SMALL_EXAMPLE)
        self.assertEqual(embedded_example(), SMALL_EXAMPLE)
        validate_surface(SMALL_EXAMPLE)

    def test_small_example_fits_its_budget(self):
        self.assertLessEqual(len(json.dumps(SMALL_EXAMPLE, separators=(",", ":"))), MAX_EXAMPLE_CHARS)

    def test_brace_copied_ref_is_rejected(self):
        payload = minimal_surface({"component": "Divider"})
        payload["createSurface"]["dataModel"] = {"tasks": {"$ref": "tasks:{due_today|overdue}"}}
        with self.assertRaises(SurfaceError) as caught:
            validate_surface(payload)
        self.assertEqual(caught.exception.code, "bad_data")

    def test_instruction_fits_prompt_budget(self):
        self.assertLessEqual(len(CARD_INSTRUCTION), MAX_INSTRUCTION_CHARS)

    def test_instruction_ends_with_a_newline(self):
        self.assertTrue(CARD_INSTRUCTION.endswith("\n"))


class CardInstructionGateTests(SimpleTestCase):
    def setUp(self):
        self.tenant = SimpleNamespace(id="11111111-1111-1111-1111-111111111111")

    @patch("apps.router.chat_gates.chat_shape_enabled", return_value=False)
    @patch("apps.router.chat_gates.chat_surfaces_enabled", return_value=True)
    def test_gated_app_tenant_gets_card_without_shape(self, _surfaces, _shape):
        self.assertEqual(_panel_chat_context("app", self.tenant), CARD_INSTRUCTION)
        self.assertIn(CARD_INSTRUCTION, build_chat_context_marker("ios", tenant=self.tenant))
        self.assertNotIn(CHAT_PANEL_INSTRUCTION, _panel_chat_context("app", self.tenant))

    @patch("apps.router.chat_gates.chat_shape_enabled", return_value=True)
    @patch("apps.router.chat_gates.chat_surfaces_enabled", return_value=True)
    def test_both_gates_append_both_instructions(self, _surfaces, _shape):
        context = _panel_chat_context("iOS", self.tenant)
        self.assertEqual(context, CHAT_PANEL_INSTRUCTION + CARD_INSTRUCTION)

    @patch("apps.router.chat_gates.chat_shape_enabled", return_value=True)
    @patch("apps.router.chat_gates.chat_surfaces_enabled", return_value=False)
    def test_shape_only_gate_keeps_card_out(self, _surfaces, _shape):
        context = _panel_chat_context("app", self.tenant)
        self.assertEqual(context, CHAT_PANEL_INSTRUCTION)
        self.assertNotIn(CARD_INSTRUCTION, context)

    @patch("apps.router.chat_gates.chat_shape_enabled", return_value=False)
    @patch("apps.router.chat_gates.chat_surfaces_enabled", return_value=False)
    def test_non_gated_app_tenant_gets_no_card(self, _surfaces, _shape):
        self.assertEqual(_panel_chat_context("app", self.tenant), "")

    @patch("apps.router.chat_gates.chat_shape_enabled", return_value=True)
    @patch("apps.router.chat_gates.chat_surfaces_enabled", return_value=True)
    def test_other_channels_never_get_card(self, _surfaces, _shape):
        for channel in ("telegram", "line"):
            with self.subTest(channel=channel):
                self.assertEqual(_panel_chat_context(channel, self.tenant), "")
                self.assertNotIn(CARD_INSTRUCTION, build_chat_context_marker(channel, tenant=self.tenant))
