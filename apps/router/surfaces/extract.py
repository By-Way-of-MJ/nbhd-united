"""Extract the optional fenced A2UI card from an assistant reply.

A card fence is stripped on every channel; only the first block is a
candidate, and it is stored only after ``validate_surface`` accepts it. Logs
are content-free in the same way as panel extraction.
"""

from __future__ import annotations

import json
import logging

from .validate import SurfaceError, validate_surface

logger = logging.getLogger(__name__)

CARD_FENCE = "```nbhd-card"


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
        marker = line.strip()
        if not inside and marker == CARD_FENCE:
            inside = found = True
            payload = []
            blocks += 1
            if blocks > 1:
                logger.warning("card_dropped reason=%s", "extra")
        elif inside and marker == "```":
            inside = False
            if blocks == 1:
                body = "".join(payload)
                closed_first = True
        elif inside:
            payload.append(line)
        else:
            kept.append(line)
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
