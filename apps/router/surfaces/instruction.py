"""Per-turn prompt telling the assistant how to compose one ``nbhd-card``.

The vocabulary is rendered from ``catalog.py`` and ``validate.py`` at import
time, so the prompt cannot drift from the validator that enforces it.
"""

from __future__ import annotations

import json
from typing import Literal, get_args, get_origin

from pydantic import BaseModel

from apps.router.panels import PANEL_VOCABULARY

from .catalog import (
    ALLOWED_ACTION_NAMES,
    CATALOG_ID,
    COMPONENT_ID_PATTERN,
    MAX_TEXT_LENGTH,
    REF_KINDS,
    TASK_LISTS,
    Action,
    ActionEvent,
    Binding,
    Button,
    Card,
    CheckBox,
    ChoicePicker,
    Component,
    List,
    ListTemplate,
    Slider,
    Text,
)
from .validate import MAX_COMPONENTS, MAX_DATA_REFS, MAX_DEPTH, MAX_SURFACE_BYTES

# Inline literal sets longer than this are rendered by their vocabulary line.
_MAX_INLINE_LITERALS = 32

# Context keys the iOS action router reads for each event. Kept next to the
# catalog so a new action cannot be documented without its payload.
_ACTION_CONTEXTS = {
    "task.toggle": "{task}",
    "task.move": '{task?,to:"tomorrow"|"next_monday"}',
    "task.drop": "{task}",
    "task.create": "{title}",
    "journal.checkin": "{mood?,energy?}",
    "weight.log": "{kg:number}",
    "workout.start": "{workout}",
    "mission.add_steps": "{mission}",
    "week.set_focus": '{picked:<picker binding, e.g. {"picked":{"@path":"/focus/picked"}}}',
    "card.dismiss": "{}",
}

# Plain forms only: a model must never copy prose braces into the ``$ref``
# string (``tasks:{due_today|overdue}`` is rejected by the validator).
_REF_FORMS = {
    "tasks": tuple(f"tasks:{name}" for name in TASK_LISTS),
    "task": ("task:<uuid>",),
    "workout": ("workout:today", "workout:<uuid>"),
    "sleep": ("sleep:last_night",),
    "weight": ("weight:latest",),
    "mission": ("mission:<uuid>",),
}


def _literal_values(model: type[BaseModel], field_name: str) -> tuple[str, ...]:
    annotation = model.model_fields[field_name].annotation
    if get_origin(annotation) is not Literal:
        return ()
    return tuple(str(value) for value in get_args(annotation))


def _bounds(model: type[BaseModel], field_name: str) -> tuple[int, int]:
    ge = le = None
    for constraint in model.model_fields[field_name].metadata:
        if hasattr(constraint, "ge"):
            ge = constraint.ge
        if hasattr(constraint, "le"):
            le = constraint.le
    return ge, le


def _field_doc(model: type[BaseModel], name: str, field) -> str:
    if model is List and name == "children":
        return '{template:<id>,path:"/abs"}'
    label = name if field.is_required() else f"{name}?"
    if model is Slider and name in {"min", "max"}:
        lo, hi = _bounds(model, name)
        prefix = "min<max," if name == "min" else ""
        return f"{prefix}{name}={lo}..{hi}"
    if model is ChoicePicker and name == "max":
        lo, hi = _bounds(model, name)
        return f"{label}={lo}..{hi} ({lo}=single select)"
    values = _literal_values(model, name)
    # A long literal set is already documented by its own vocabulary line
    # (Panel kinds); keep the component shape readable instead of repeating it.
    if values and len("|".join(values)) <= _MAX_INLINE_LITERALS:
        return f"{label}={'|'.join(values)}"
    return label


def _component_docs() -> str:
    models = get_args(get_args(Component)[0])
    docs = []
    for model in models:
        props = [
            _field_doc(model, name, field)
            for name, field in model.model_fields.items()
            if name not in {"id", "component"}
        ]
        suffix = "" if model.__name__ == "Divider" else f"({','.join(props)})"
        docs.append(f"{model.__name__}{suffix}")
    return " ".join(docs)


def _ref_forms() -> str:
    return " | ".join(form for kind in REF_KINDS for form in _REF_FORMS[kind])


def _action_docs() -> str:
    return "; ".join(f"{name}{_ACTION_CONTEXTS[name]}" for name in ALLOWED_ACTION_NAMES)


def build_small_example() -> dict:
    """Build the prompt example from the catalog models, not hand-written JSON."""
    components = [
        Card(id="root", component="Card", children=["t", "l", "b"]),
        Text(id="t", component="Text", text="Hi", style="title"),
        List(id="l", component="List", children=ListTemplate(template="i", path="/t")),
        CheckBox(
            id="i",
            component="CheckBox",
            label=Binding(path="title"),
            value=Binding(path="done"),
            action=Action(event=ActionEvent(name="task.toggle", context={"task": Binding(path="id")})),
        ),
        Button(
            id="b",
            component="Button",
            label="Tomorrow",
            action=Action(event=ActionEvent(name="task.move", context={"to": "tomorrow"})),
        ),
    ]
    return {
        "version": "1.0",
        "createSurface": {
            "surfaceId": "t",
            "catalogId": CATALOG_ID,
            "components": [
                component.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
                for component in components
            ],
            "dataModel": {"t": {"$ref": "tasks:due_today"}},
        },
    }


SMALL_EXAMPLE = build_small_example()
_EXAMPLE = json.dumps(SMALL_EXAMPLE, separators=(",", ":"))

CARD_INSTRUCTION = (
    "When: card only if user can act on live data now (day plan, check-in, "
    "review, stuck project, workout). Most need none. <=1 "
    "```nbhd-card block, last, after text; never mention.\n"
    "Format: "
    '{"version":"1.0","createSurface":{"surfaceId":"<kebab id>",'
    '"catalogId":"' + CATALOG_ID + '","components":[...],"dataModel":{...}}}. '
    "Flat: id+component; root id=root; containers list child ids; ids "
    "match " + COMPONENT_ID_PATTERN + " lowercase.\n"
    "Components: " + _component_docs() + ". Bindings: "
    '{"@path":"/abs"}; in a List template, relative {"@path":"field"}.\n'
    "Panels " + PANEL_VOCABULARY + ".\n"
    "Data: dataModel MUST be refs, never copied: "
    '{"$ref":"<form>"}, <form> = ' + _ref_forms() + ".\n"
    'Actions: {"event":{"name":<name>,"context":{...}}}: ' + _action_docs() + ".\n"
    f"Limits: {MAX_COMPONENTS} components, depth <= {MAX_DEPTH}, "
    f"{MAX_SURFACE_BYTES}B, <= {MAX_DATA_REFS} refs, text {MAX_TEXT_LENGTH} "
    "chars, no URLs.\n"
    "Example\n```nbhd-card\n" + _EXAMPLE + "\n```\n"
)
