"""Extract the optional fenced A2UI card from an assistant reply.

A card fence is stripped on every channel; only the first block is a
candidate, and it is stored only after ``validate_surface`` accepts it. Logs
are content-free in the same way as panel extraction.

The opener is deliberately tolerant of the shapes a model actually emits:
case-insensitive, three or more backticks, optional spaces/``json``, prose
before the fence and JSON on the opener line. The matching helpers are shared
with streaming partial stripping so the final and in-progress views agree.
"""

from __future__ import annotations

import json
import logging
import re

from .validate import SurfaceError, validate_surface

logger = logging.getLogger(__name__)

CARD_FENCE = "```nbhd-card"

_BACKTICKS = re.compile(r"`{3,}")
_CARD_OPENER_TAIL = re.compile(r"[ \t]*(?:json\b[ \t]*)?.*?nbhd-card", re.IGNORECASE)


def _is_partial_card_opener(tail: str) -> bool:
    """Whether ``tail`` could still grow into a card opener."""
    candidate = tail.lstrip(" \t").rstrip(" \t").casefold()
    if not candidate:
        return True
    if "json".startswith(candidate) or "nbhd-card".startswith(candidate):
        return True
    if candidate.startswith("json"):
        return "nbhd-card".startswith(candidate[4:].lstrip(" \t"))
    return False


def find_card_fence_opener(line: str, *, partial: bool = False) -> tuple[int, int] | None:
    """Return ``(fence_start, after_token)`` for a card opener on ``line``.

    ``fence_start`` is the first backtick of the opener, so prose before it can
    be kept. With ``partial=True`` a trailing, still-incomplete opener also
    matches; its end is the end of the line.
    """
    for fence in _BACKTICKS.finditer(line):
        tail = line[fence.end() :]
        match = _CARD_OPENER_TAIL.match(tail)
        if match:
            return fence.start(), fence.end() + match.end()
        if partial and _is_partial_card_opener(tail):
            return fence.start(), len(line)
    return None


def split_card_fence_close(line: str) -> tuple[str, str] | None:
    """Split a closing fence into (JSON before it, text after it).

    A close is either a line containing only three or more backticks, or a
    fence glued directly after the JSON object. The caller keeps the after-fence
    text and discards the fence.
    """
    marker = line.strip()
    if len(marker) >= 3 and set(marker) == {"`"}:
        return "", ""
    for fence in _BACKTICKS.finditer(line):
        before = line[: fence.start()]
        if before.rstrip().endswith("}"):
            return before, line[fence.end() :]
    return None


def _append_after(after: str, kept: list[str]) -> None:
    """Keep visible text following a closing fence without a stray blank line."""
    if not after.strip():
        return
    after = after.lstrip("\r\n")
    if after[:1] in (" ", "\t"):
        after = after.lstrip(" \t")
    kept.append(after)


def extract_card(text: str) -> tuple[str, list[dict]]:
    """Strip every ``nbhd-card`` block and return the first valid surface.

    The first block is the only candidate; later blocks are stripped and
    logged with ``reason=extra``. An unclosed block strips from its opener to
    the end. Text without a block is returned unchanged.
    """
    kept: list[str] = []
    payload: list[str] = []
    inside = found = closed_first = False
    blocks = 0
    body = ""
    for line in text.splitlines(keepends=True):
        if not inside:
            opener = find_card_fence_opener(line)
            if opener is None:
                kept.append(line)
                continue
            start, token_end = opener
            prefix = line[:start]
            if prefix.strip():
                kept.append(prefix.rstrip(" \t") + "\n")
            inside = found = True
            payload = []
            blocks += 1
            if blocks > 1:
                logger.warning("card_dropped reason=%s", "extra")
            rest = line[token_end:].lstrip(" \t")
            close = split_card_fence_close(rest)
            if close is None:
                if rest:
                    payload.append(rest)
                continue
            before, after = close
            if before.strip():
                payload.append(before)
            inside = False
            if blocks == 1:
                body = "".join(payload)
                closed_first = True
            _append_after(after, kept)
            continue
        close = split_card_fence_close(line)
        if close is None:
            payload.append(line)
            continue
        before, after = close
        if before.strip():
            payload.append(before)
        inside = False
        if blocks == 1:
            body = "".join(payload)
            closed_first = True
        _append_after(after, kept)
    if inside:
        logger.warning("card_dropped reason=%s", "unclosed")
    if not found:
        return text, []
    clean = "".join(kept).strip()
    if not closed_first:
        return clean, []
    try:
        candidate = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("card_dropped reason=%s", "malformed_json")
        return clean, []
    try:
        return clean, [validate_surface(candidate)]
    except SurfaceError as err:
        logger.warning("card_dropped reason=%s", err.code)
        return clean, []
