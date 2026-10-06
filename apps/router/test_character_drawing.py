from django.test import SimpleTestCase

from apps.router.character_drawing import has_character_drawing


class CharacterDrawingTests(SimpleTestCase):
    def test_box_drawing_timeline_is_a_drawing(self):
        reply = "Here is the plan:\n```\nWEEK 1      WEEK 2\n├───────────┼───────────┤\n│ Audit     │ Fixes     │\n```"
        self.assertTrue(has_character_drawing(reply))

    def test_ascii_frame_is_a_drawing(self):
        self.assertTrue(has_character_drawing("+--------+\n| Audit  |\n+--------+"))

    def test_markdown_table_rule_and_prose_are_not(self):
        table = "| Phase | Dates |\n| --- | --- |\n| **Phase 1** | Oct 6 → 13 |\n\n---\n\n- one\n- two"
        self.assertFalse(has_character_drawing(table))
        self.assertFalse(has_character_drawing("Pick A │ B, your call."))
        self.assertFalse(has_character_drawing(""))
        self.assertFalse(has_character_drawing(None))
