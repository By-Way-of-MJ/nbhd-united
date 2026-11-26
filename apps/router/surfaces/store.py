"""Storage helpers for the A2UI surfaces attached to assistant messages.

Surfaces are stored validated (``validate_surface``) and, like panel titles, in
PII-placeholder space: every user-visible string is rehydrated to real values
only at the owner-facing read seams. Nothing here touches the database.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .validate import SurfaceError, validate_surface

logger = logging.getLogger(__name__)

MAX_SURFACES = 3

# Single user-visible string prop per component kind. ``ChoicePicker`` also
# carries a list of label strings in ``options`` (handled below).
_TEXT_PROPS = {
    "Text": "text",
    "Button": "label",
    "CheckBox": "label",
    "ChoicePicker": "label",
    "DateTimeInput": "label",
    "Slider": "label",
    "TextField": "label",
}


def _is_binding(value: Any) -> bool:
    return isinstance(value, dict) and "@path" in value


def _rewrite_leaf(value: Any, finalize) -> Any:
    """Rewrite a user-visible string; bindings and non-strings pass through."""
    if isinstance(value, str):
        return finalize(value)
    return value


def _rewrite_tree(value: Any, finalize) -> Any:
    """Rebuild ``value`` with every string leaf rewritten (keys untouched)."""
    if isinstance(value, str):
        return finalize(value)
    if isinstance(value, dict):
        return {key: _rewrite_tree(item, finalize) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_tree(item, finalize) for item in value]
    return value


def _rewrite_component(component: dict, finalize) -> None:
    prop = _TEXT_PROPS.get(component.get("component"))
    if prop:
        component[prop] = _rewrite_leaf(component.get(prop), finalize)
    if component.get("component") == "ChoicePicker":
        options = component.get("options")
        if isinstance(options, list):
            component["options"] = [_rewrite_leaf(item, finalize) for item in options]


def clean_surfaces(value) -> list[dict]:
    """Validate stored surfaces, dropping invalid ones (content-free logs)."""
    if value is None:
        return []
    if not isinstance(value, list):
        logger.info("a2ui_surface_dropped code=%s", "not_list")
        return []
    surfaces = []
    for candidate in value:
        try:
            surface = validate_surface(candidate)
        except SurfaceError as err:
            logger.info("a2ui_surface_dropped code=%s", err.code)
            continue
        if len(surfaces) == MAX_SURFACES:
            logger.info("a2ui_surface_dropped code=%s", "limit")
            break
        surfaces.append(surface)
    return surfaces


def rehydrate_surfaces(value, entity_map, *, tenant_id) -> list[dict]:
    """Rehydrate every user-visible string for an owner-facing read seam.

    Mirrors ``apps.router.panels.rehydrate_panels``: stored surfaces rest in
    PII-placeholder space, so labels, option strings and every ``dataModel``
    string leaf resolve through the ordinary outbound seam on the way out. The
    input is never mutated.
    """
    from apps.router.reply_text import finalize_outbound_text

    surfaces = copy.deepcopy(clean_surfaces(value))

    def finalize(text: str) -> str:
        return finalize_outbound_text(text, entity_map, tenant_id=tenant_id, channel="a2ui")

    for envelope in surfaces:
        body = envelope.get("createSurface") or {}
        for component in body.get("components", ()):
            _rewrite_component(component, finalize)
        if isinstance(body.get("dataModel"), dict):
            body["dataModel"] = _rewrite_tree(body["dataModel"], finalize)
    return surfaces
