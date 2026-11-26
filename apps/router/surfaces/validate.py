"""Validate and normalize an A2UI v1.0 ``createSurface`` message.

The validator is the trust boundary between the server (or a model) and the iOS
renderer. It fails closed with a stable ``SurfaceError.code`` so callers can log
or map the failure without parsing exception text. Everything here is pure: no
models, no database, no settings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from apps.router.panels import validate_panels

from .catalog import (
    CATALOG_ID,
    MAX_TEXT_LENGTH,
    REF_PATTERN,
    Binding,
    Card,
    Column,
    Component,
    List,
    Panel,
    Row,
)

MAX_SURFACE_BYTES = 32768
MAX_COMPONENTS = 60
MAX_DEPTH = 8
MAX_DATA_DEPTH = 6
MAX_DATA_LEAVES = 200
MAX_DATA_REFS = 20
SURFACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DATA_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_COMPONENTS = TypeAdapter(list[Component])
_SURFACE_KEYS = frozenset({"surfaceId", "catalogId", "components", "dataModel"})


class SurfaceError(ValueError):
    """A surface payload was rejected; ``code`` is stable and log-friendly."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _children_ids(component: BaseModel) -> list[str]:
    """Normal child edges only; a List's template edge is deliberately absent."""
    if isinstance(component, (Column, Row, Card)):
        return list(component.children)
    return []


def _component_targets(component: BaseModel) -> list[str]:
    """Graph edges: children for containers, the template id for a List."""
    if isinstance(component, List):
        return [component.children.template]
    return _children_ids(component)


def _reachable(edges: dict[str, list[str]], start: str) -> set[str]:
    seen: set[str] = set()
    pending = [start]
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(edges.get(node, ()))
    return seen


def _bindings(value: Any) -> Iterator[Binding]:
    if isinstance(value, Binding):
        yield value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _bindings(getattr(value, name))
    elif isinstance(value, dict):
        for item in value.values():
            yield from _bindings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _bindings(item)


def _template_ids(components: list[BaseModel], edges: dict[str, list[str]]) -> set[str]:
    """Every component rendered by a List template may use relative bindings."""
    inside: set[str] = set()
    pending = [component.children.template for component in components if isinstance(component, List)]
    while pending:
        node = pending.pop()
        if node in inside:
            continue
        inside.add(node)
        pending.extend(edges.get(node, ()))
    return inside


def _check_panels(components: list[BaseModel]) -> None:
    """A Panel's kind+params must pass the same rules as ``panels.py``.

    Reusing ``validate_panels`` keeps the surface panel vocabulary and the
    assistant-attached panel vocabulary from drifting: one item in, exactly one
    item out, otherwise the payload is a schema failure.
    """
    for component in components:
        if not isinstance(component, Panel):
            continue
        if len(validate_panels([{"kind": component.kind, "params": component.params}], log=False)) != 1:
            raise SurfaceError("schema")


def _check_graph(components: list[BaseModel]) -> None:
    ids = [component.id for component in components]
    if len(set(ids)) != len(ids):
        raise SurfaceError("duplicate_id")
    known = set(ids)
    if "root" not in known:
        raise SurfaceError("missing_root")

    edges = {component.id: _component_targets(component) for component in components}
    for targets in edges.values():
        if any(target not in known for target in targets):
            raise SurfaceError("dangling_child")

    for component in components:
        if not isinstance(component, (Column, Row, Card)):
            continue
        children = component.children
        if len(set(children)) != len(children):
            raise SurfaceError("duplicate_child")

    # Iterative three-color DFS over every component, so a cycle in a
    # disconnected island is rejected just like one under root.
    state: dict[str, int] = {}
    for start in edges:
        if state.get(start):
            continue
        state[start] = 1
        stack = [(start, iter(edges[start]))]
        while stack:
            node, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                state[node] = 2
                stack.pop()
            elif state.get(target) == 1:
                raise SurfaceError("cycle")
            elif not state.get(target):
                state[target] = 1
                stack.append((target, iter(edges.get(target, ()))))

    # "direct" is what the renderer reaches from root WITHOUT following a
    # List's template edge. A node in direct is a normal node, not a per-item
    # array element, so relative bindings there are unsafe; a List template
    # pointing back into direct (especially at root) would let an item-only
    # relative pointer masquerade as a normal node binding.
    direct = _reachable({component.id: _children_ids(component) for component in components}, "root")
    for component in components:
        if isinstance(component, List) and (
            component.children.template == "root" or component.children.template in direct
        ):
            raise SurfaceError("bad_template")

    templates = _template_ids(components, edges)
    allowed_relative = templates - direct
    for component in components:
        if component.id in allowed_relative:
            continue
        if any(not binding.path.startswith("/") for binding in _bindings(component)):
            raise SurfaceError("bad_template")

    # Nothing may float outside the root-reachable graph, or an unattached
    # subtree could smuggle in components (and their bindings) that never
    # render.
    reachable = _reachable(edges, "root")
    if any(component.id not in reachable for component in components):
        raise SurfaceError("orphan")

    # Longest path from root, counting root as depth 1. Cycles are gone by now,
    # so revisiting a node only matters when we found a longer route to it.
    depth = {"root": 1}
    stack = [("root", 1)]
    while stack:
        node, level = stack.pop()
        if level > MAX_DEPTH:
            raise SurfaceError("too_deep")
        for target in edges.get(node, ()):
            if depth.get(target, 0) < level + 1:
                depth[target] = level + 1
                stack.append((target, level + 1))


def _check_data_model(data_model: Any) -> None:
    """Bound the ``dataModel`` tree and allow only live-ref dicts.

    A surface stores references (``{"$ref": "tasks:due_today"}``), never a
    snapshot: values are resolved fresh at the owner-facing read seam. The tree
    itself is bounded so a payload cannot smuggle in a large document.
    """
    if not isinstance(data_model, dict):
        raise SurfaceError("bad_data")
    leaves = 0
    refs = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal leaves, refs
        if isinstance(node, dict):
            if depth > MAX_DATA_DEPTH:
                raise SurfaceError("bad_data")
            if "$ref" in node:
                refs += 1
                leaves += 1
                if refs > MAX_DATA_REFS or leaves > MAX_DATA_LEAVES:
                    raise SurfaceError("bad_data")
                ref = node.get("$ref")
                if len(node) != 1 or not isinstance(ref, str) or not REF_PATTERN.fullmatch(ref):
                    raise SurfaceError("bad_data")
                return
            for key, item in node.items():
                if not isinstance(key, str) or not DATA_KEY_PATTERN.fullmatch(key):
                    raise SurfaceError("bad_data")
                walk(item, depth + 1)
            return
        if isinstance(node, list):
            if depth > MAX_DATA_DEPTH:
                raise SurfaceError("bad_data")
            for item in node:
                walk(item, depth + 1)
            return
        leaves += 1
        if leaves > MAX_DATA_LEAVES:
            raise SurfaceError("bad_data")
        if isinstance(node, str) and (len(node) > MAX_TEXT_LENGTH or "://" in node):
            raise SurfaceError("bad_data")

    walk(data_model, 1)


def validate_surface(payload: dict) -> dict:
    """Return the normalized surface dict or raise ``SurfaceError(code)``.

    ``payload`` is the envelope, e.g. ``{"version": "1.0", "createSurface": ...}``.
    Output uses model aliases (``@path``), drops nulls, and materializes defaults.
    """
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError):
        raise SurfaceError("schema") from None
    # json.dumps is ASCII by default, so this is also the UTF-8 byte length.
    if len(encoded) > MAX_SURFACE_BYTES:
        raise SurfaceError("too_large")

    if not isinstance(payload, dict):
        raise SurfaceError("bad_envelope")
    if payload.get("version") != "1.0":
        raise SurfaceError("bad_version")
    if len(payload) != 2 or set(payload) != {"version", "createSurface"}:
        raise SurfaceError("bad_envelope")

    surface = payload["createSurface"]
    if not isinstance(surface, dict) or set(surface) - _SURFACE_KEYS:
        raise SurfaceError("bad_envelope")
    surface_id = surface.get("surfaceId")
    if not isinstance(surface_id, str) or not SURFACE_ID_PATTERN.fullmatch(surface_id):
        raise SurfaceError("bad_envelope")
    if surface.get("catalogId") != CATALOG_ID:
        raise SurfaceError("bad_catalog")

    raw_components = surface.get("components")
    if not isinstance(raw_components, list):
        raise SurfaceError("schema")
    if len(raw_components) > MAX_COMPONENTS:
        raise SurfaceError("too_many")
    try:
        components = _COMPONENTS.validate_python(raw_components)
    except ValidationError:
        raise SurfaceError("schema") from None

    _check_panels(components)

    data_model = surface.get("dataModel")
    if "dataModel" in surface:
        _check_data_model(data_model)

    _check_graph(components)

    normalized: dict[str, Any] = {
        "version": "1.0",
        "createSurface": {
            "surfaceId": surface_id,
            "catalogId": CATALOG_ID,
            "components": [component.model_dump(by_alias=True, exclude_none=True) for component in components],
        },
    }
    if "dataModel" in surface:
        normalized["createSurface"]["dataModel"] = data_model
    return normalized
