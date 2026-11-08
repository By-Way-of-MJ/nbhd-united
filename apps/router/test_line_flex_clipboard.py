"""Tests for the LINE Flex clipboard footer limit."""

from django.test import SimpleTestCase

from apps.router.line_flex import build_flex_bubble


def _find_footer(node):
    """Return the first ``footer`` component found in a Flex message tree."""
    if isinstance(node, dict):
        if "footer" in node:
            return node["footer"]
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None

    for child in children:
        footer = _find_footer(child)
        if footer is not None:
            return footer
    return None


class LineFlexClipboardTest(SimpleTestCase):
    def test_clipboard_footer_is_capped_at_line_limit(self):
        footer = _find_footer(build_flex_bubble("x" * 3000))

        self.assertIsNotNone(footer)
        clipboard_text = footer["action"]["clipboardText"]
        self.assertLessEqual(len(clipboard_text), 1000)
        self.assertTrue(clipboard_text.endswith("\u2026"))
