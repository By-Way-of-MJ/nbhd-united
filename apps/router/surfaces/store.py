"""Storage helpers for the A2UI surfaces attached to assistant messages.

Surfaces are stored validated (``validate_surface``) and, like panel titles, in
PII-placeholder space: every user-visible string is rehydrated to real values
only at the owner-facing read seams. ``dataModel`` may hold live ``$ref``
entries, which are resolved fresh per request — after rehydration — and never
stored. Nothing here touches the database.
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


def _rewrite_leaf(value: Any, finalize) -> Any:
    """Rewrite a user-visible string; bindings and non-strings pass through."""
    if isinstance(value, str):
        return finalize(value)
    return value


def _rewrite_tree(value: Any, finalize) -> Any:
    """Rebuild ``value`` with every string leaf rewritten (keys untouched).

    ``{"$ref": ...}`` and ``{"@path": ...}`` dicts are code, not content: they
    survive intact so live refs and bindings keep working.
    """
    if isinstance(value, str):
        return finalize(value)
    if isinstance(value, dict):
        if set(value) in ({"$ref"}, {"@path"}):
            return dict(value)
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
    action = component.get("action")
    if isinstance(action, dict):
        event = action.get("event")
        if isinstance(event, dict) and isinstance(event.get("context"), dict):
            event["context"] = _rewrite_tree(event["context"], finalize)
    if component.get("component") == "Panel":
        ref = component.get("ref")
        if isinstance(ref, str):
            component["ref"] = finalize(ref)


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


def redact_surfaces(value, tenant) -> list[dict]:
    """Return a copy with every user-visible surface string in placeholder space.

    Mirrors ``apps.router.panels.prepare_panels`` for titles: component text and
    labels, ChoicePicker options, action-context literals, Panel.ref and plain
    ``dataModel`` leaves pass through the same known-value redactor before the
    surface is stored. ``$ref`` entries stay untouched so live values keep
    resolving at read time.
    """
    from apps.pii.egress import redact_known_values

    surfaces = copy.deepcopy(clean_surfaces(value))

    def finalize(text: str) -> str:
        return redact_known_values(tenant, text, seam="surface_storage")

    for envelope in surfaces:
        body = envelope.get("createSurface") or {}
        for component in body.get("components", ()):
            _rewrite_component(component, finalize)
        if isinstance(body.get("dataModel"), dict):
            body["dataModel"] = _rewrite_tree(body["dataModel"], finalize)
    return surfaces


def rehydrate_surfaces(value, entity_map, *, tenant_id, tenant=None, cache: dict | None = None) -> list[dict]:
    """Rehydrate every user-visible string for an owner-facing read seam.

    Mirrors ``apps.router.panels.rehydrate_panels``: stored surfaces rest in
    PII-placeholder space, so labels, option strings, action-context literals,
    ``Panel.ref`` and every plain ``dataModel`` string leaf resolve through the
    ordinary outbound seam on the way out. When ``tenant`` is given, live
    ``$ref`` entries in the ``dataModel`` are then resolved fresh, per request.
    Resolved values are already owner-facing and are deliberately NOT passed
    through rehydration again. The input is never mutated. ``cache`` is shared
    across one feed page's rows so a ref used by several surfaces resolves once
    per page.
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

    if tenant is not None:
        from .resolve import resolve_refs

        for envelope in surfaces:
            body = envelope.get("createSurface") or {}
            if isinstance(body.get("dataModel"), dict):
                body["dataModel"] = resolve_refs(body["dataModel"], tenant=tenant, cache=cache)
    return surfaces
