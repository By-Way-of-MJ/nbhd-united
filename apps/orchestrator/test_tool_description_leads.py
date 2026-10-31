"""Assistant tool descriptions must lead with a plain first sentence.

OpenClaw's tool directory cuts each description to roughly its first 64
characters, so the opening words have to say what the tool does. Each pinned
tool below must start with an exact lead sentence followed by one space, and the
text after that lead must still begin with the original description's first 40
characters (nothing dropped, reordered or rewritten).
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]

# (file, tool name, lead sentence, original description's first 40 characters)
LEADS = [
    (
        "runtime/openclaw/plugins/nbhd-journal-tools/index.js",
        "nbhd_update_profile",
        "Change the user's time zone, home city, name, or language.",
        "Update the user's profile settings. Use ",
    ),
    (
        "runtime/openclaw/plugins/nbhd-journal-tools/index.js",
        "nbhd_update_situation",
        "Record the city the user is in right now (trip or home).",
        "Call this THAT TURN when the user states",
    ),
    (
        "runtime/openclaw/plugins/nbhd-journal-tools/index.js",
        "nbhd_reconcile_scan",
        "Find goals, tasks, money, fitness rows a report changes.",
        "Call this BEFORE replying on a conversat",
    ),
    (
        "runtime/openclaw/plugins/nbhd-automation-tools/index.js",
        "nbhd_cron_create_pure_reminder",
        "Schedule a reminder that sends fixed text at a set time.",
        "May require approval; never claim the sc",
    ),
    (
        "runtime/openclaw/plugins/nbhd-automation-tools/index.js",
        "nbhd_cron_create_quote_user_intent",
        "Schedule a message that repeats the user's own words later.",
        "May require approval; never claim the sc",
    ),
    (
        "runtime/openclaw/plugins/nbhd-automation-tools/index.js",
        "nbhd_cron_create_domain_summary",
        "Schedule a recurring summary of tasks, goals or calendar.",
        "May require approval; never claim the sc",
    ),
    (
        "runtime/openclaw/plugins/nbhd-datebook-tools/index.js",
        "nbhd_datebook_read",
        "Read the user's calendar events and Apple reminders.",
        "Calendar entries are plans, even after t",
    ),
    (
        "runtime/openclaw/plugins/nbhd-document-keep/index.js",
        "nbhd_document_keep",
        "Tag saved items with their source so they can be undone.",
        "AFTER the user agrees and you've saved e",
    ),
    (
        "runtime/openclaw/plugins/nbhd-journal-shaping/index.js",
        "nbhd_journal_template_update",
        "Replace the user's daily-note template once they approve.",
        "After reading the current template, show",
    ),
]

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _read_js_string(source, pos):
    """Return (value, next_pos) for the JS string literal starting at pos."""
    quote = source[pos]
    if quote not in "\"'":
        raise AssertionError(f"expected a string literal at offset {pos}, found {source[pos : pos + 20]!r}")
    out = []
    pos += 1
    while pos < len(source):
        char = source[pos]
        if char == "\\":
            escape = source[pos + 1]
            out.append(_ESCAPES.get(escape, escape))
            pos += 2
            continue
        if char == quote:
            return "".join(out), pos + 1
        out.append(char)
        pos += 1
    raise AssertionError("unterminated string literal")


def tool_description(source, tool):
    """Rebuild a tool's description from its inline adjacent string literals."""
    name = re.search(r'name\s*:\s*(["\'])' + re.escape(tool) + r"\1", source)
    if not name:
        raise AssertionError(f"no name entry for {tool}")
    description = re.compile(r"\bdescription\s*:").search(source, name.end())
    if not description:
        raise AssertionError(f"no inline description after name entry for {tool}")
    pos = description.end()
    parts = []
    while True:
        while source[pos].isspace():
            pos += 1
        value, pos = _read_js_string(source, pos)
        parts.append(value)
        while source[pos].isspace():
            pos += 1
        if source[pos] != "+":
            break
        pos += 1
    return " ".join("".join(parts).split())


class ToolDescriptionLeadTests(SimpleTestCase):
    def test_each_tool_description_leads_with_plain_sentence(self):
        for path, tool, lead, original_head in LEADS:
            with self.subTest(tool=tool):
                description = tool_description((ROOT / path).read_text(), tool)
                self.assertTrue(
                    description.startswith(lead + " "),
                    f"{tool} does not open with {lead!r}: {description[:64]!r}",
                )
                self.assertLessEqual(len(lead), 60, f"{tool} lead is {len(lead)} characters")
                remainder = description[len(lead) + 1 :]
                self.assertTrue(
                    remainder.startswith(original_head),
                    f"{tool} original description lost or reordered: {remainder[:40]!r}",
                )
