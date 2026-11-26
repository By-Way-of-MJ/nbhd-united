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
    REF_KINDS,
    TASK_LISTS,
    Component,
)
from apps.router.surfaces.instruction import CARD_INSTRUCTION
from apps.router.surfaces.validate import validate_surface

MAX_INSTRUCTION_CHARS = 3500


def embedded_example() -> dict:
    opener = "```nbhd-card\n"
    start = CARD_INSTRUCTION.index(opener) + len(opener)
    end = CARD_INSTRUCTION.index("\n```", start)
    return json.loads(CARD_INSTRUCTION[start:end])


class CardInstructionTests(SimpleTestCase):
    def test_every_action_is_documented(self):
        for name in ALLOWED_ACTION_NAMES:
            with self.subTest(action=name):
                self.assertIn(name, CARD_INSTRUCTION)

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

    def test_embedded_example_is_a_valid_surface(self):
        surface = embedded_example()
        validated = validate_surface(surface)
        self.assertEqual(
            validated["createSurface"]["surfaceId"],
            "gym-tonight",
        )

    def test_instruction_fits_prompt_budget(self):
        self.assertLessEqual(len(CARD_INSTRUCTION), MAX_INSTRUCTION_CHARS)


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
