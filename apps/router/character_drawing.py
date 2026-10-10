"""Detect a diagram drawn with characters in an assistant reply.

Phones wrap box-drawing timelines / calendars / flowcharts into noise. AGENTS.md
tells the assistant never to draw them; this is the deterministic check behind
that rule — it measures how often the rule is broken (reply log line + behaviour
eval). It never rewrites or re-runs a turn: re-running an agent turn can repeat
its tool side effects.
"""

from __future__ import annotations

import re

# U+2500–U+257F is the Unicode "Box Drawing" block (─ │ ┌ ├ ┼ ╔ …). A couple can
# appear in honest prose or a tree listing; a drawing uses many.
_BOX_DRAWING = re.compile("[\u2500-\u257f]")
_BOX_DRAWING_MIN = 8
# ASCII frames: +-----+  or  |-----|  rules, at least two of them.
_ASCII_FRAME = re.compile(r"^[ \t]*[+|][-=+]{4,}[+|][ \t]*$", re.MULTILINE)
_ASCII_FRAME_MIN = 2
# A Markdown table delimiter row (| --- | :-: |) is not a drawing.
_TABLE_DELIMITER = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$", re.MULTILINE)


def has_character_drawing(text: str | None) -> bool:
    """True when ``text`` contains a diagram drawn with box-drawing or ASCII frame
    characters. Markdown tables and horizontal rules do not count."""
    if not text:
        return False
    if len(_BOX_DRAWING.findall(text)) >= _BOX_DRAWING_MIN:
        return True
    without_tables = _TABLE_DELIMITER.sub("", text)
    return len(_ASCII_FRAME.findall(without_tables)) >= _ASCII_FRAME_MIN
