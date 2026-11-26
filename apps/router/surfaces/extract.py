"""Extract the optional fenced A2UI card from an assistant reply.

A card fence is stripped on every channel; only the first block is a
candidate, and it is stored only after ``validate_surface`` accepts it. Logs
are content-free in the same way as panel extraction.

The opener is deliberately tolerant of the shapes a model actually emits:
case-insensitive, three or more backticks, optional spaces/``json``, prose
before the fence and JSON on the opener line, but the token must start the
fence: a backtick run followed later by the word in prose is not an opener.
Unlabelled ``` (or ```json) fences whose body is a ``createSurface`` envelope
are treated as cards too; every other fence is left alone. The matching
helpers are shared with streaming partial stripping so the final and
in-progress views agree.
"""

from __future__ import annotations

import json
import logging
import re

from .validate import SurfaceError, validate_surface

logger = logging.getLogger(__name__)

CARD_FENCE = "```nbhd-card"

_BACKTICKS = re.compile(r"`{3,}")
_CARD_OPENER_TAIL = re.compile(r"[ \t]*(?:json[ \t]+)?nbhd-card\b", re.IGNORECASE)
_UNLABELLED_OPENER = re.compile(r"`{3,}(?:[ \t]*json)?[ \t]*", re.IGNORECASE)


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


def _is_card_opener_rest(rest: str) -> bool:
    """The opener may only be followed by whitespace or its JSON body."""
    remainder = rest.lstrip(" \t")
    return not remainder.strip() or remainder.startswith("{")


def find_card_fence_opener(line: str, *, partial: bool = False) -> tuple[int, int] | None:
    """Return ``(fence_start, after_token)`` for a card opener on ``line``.

    ``fence_start`` is the first backtick of the opener, so prose before it can
    be kept. With ``partial=True`` a trailing, still-incomplete opener also
    matches; its end is the end of the line. A token followed by prose is not
    an opener, so an inline mention of ``nbhd-card`` cannot hide a reply.
    """
    for fence in _BACKTICKS.finditer(line):
        tail = line[fence.end() :]
        match = _CARD_OPENER_TAIL.match(tail)
        if match:
            token_end = fence.end() + match.end()
            if _is_card_opener_rest(line[token_end:]):
                return fence.start(), token_end
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


def _is_unlabelled_opener(line: str) -> bool:
    """A plain ```/```json fence line whose body may be a card envelope."""
    return _UNLABELLED_OPENER.fullmatch(line.strip()) is not None


def _is_opaque_opener(line: str) -> bool:
    """A language-tagged fence that owns its closing backticks."""
    marker = line.strip()
    match = _BACKTICKS.match(marker)
    return match is not None and "`" not in marker[match.end() :]


def _envelope_body(text: str) -> dict | None:
    """Return the parsed object only when it carries a ``createSurface`` key."""
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict) and "createSurface" in value:
        return value
    return None


def extract_card(text: str) -> tuple[str, list[dict]]:
    """Strip every ``nbhd-card`` block and return the first valid surface.

    The first labelled block is the only candidate. An unlabelled fence is a
    fallback candidate only when its body is a ``createSurface`` envelope and
    no labelled block exists; every other fence is returned unchanged. Later
    blocks are stripped and logged with ``reason=extra``. An unclosed labelled
    block strips from its opener to the end. Text without a block is returned
    unchanged.
    """
    kept: list[str] = []
    payload: list[str] = []
    inside = found = closed_first = False
    blocks = 0
    body = ""
    opaque = False
    ordinary: list[str] | None = None
    ordinary_payload: list[str] = []
    unlabelled_candidate: dict | None = None
    for line in text.splitlines(keepends=True):
        if inside:
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
            continue
        if opaque:
            kept.append(line)
            marker = line.strip()
            if len(marker) >= 3 and set(marker) == {"`"}:
                opaque = False
            continue
        if ordinary is not None:
            close = split_card_fence_close(line)
            if close is None:
                ordinary.append(line)
                ordinary_payload.append(line)
                continue
            before, after = close
            if before.strip():
                ordinary_payload.append(before)
            candidate = _envelope_body("".join(ordinary_payload))
            if candidate is None:
                kept.extend(ordinary)
                kept.append(line)
            else:
                if unlabelled_candidate is None:
                    unlabelled_candidate = candidate
                _append_after(after, kept)
            ordinary = None
            ordinary_payload = []
            continue
        opener = find_card_fence_opener(line)
        if opener is not None:
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
        if _is_unlabelled_opener(line):
            ordinary = [line]
            ordinary_payload = []
            continue
        kept.append(line)
        if _is_opaque_opener(line):
            # Any other fence owns its closing backticks, so they cannot be
            # mistaken for an unlabelled card opener (e.g. after nbhd-panels).
            opaque = True
    if inside:
        logger.warning("card_dropped reason=%s", "unclosed")
    if ordinary is not None:
        # An unclosed unlabelled fence is not a complete card block: keep it.
        kept.extend(ordinary)
    if found:
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
    if unlabelled_candidate is None:
        return text, []
    clean = "".join(kept).strip()
    try:
        return clean, [validate_surface(unlabelled_candidate)]
    except SurfaceError as err:
        logger.warning("card_dropped reason=%s", err.code)
        return clean, []
