"""Per-turn prompt telling the assistant how to compose one ``nbhd-card``.

The vocabulary is rendered from ``catalog.py`` and ``validate.py`` at import
time, so the prompt cannot drift from the validator that enforces it.
"""

from __future__ import annotations

import json
from typing import get_args

from .catalog import (
    ALLOWED_ACTION_NAMES,
    CATALOG_ID,
    MAX_TEXT_LENGTH,
    REF_KINDS,
    TASK_LISTS,
    Component,
)
from .demo import build_demo_surface
from .resolve import MAX_TASKS
from .validate import MAX_COMPONENTS, MAX_DATA_REFS, MAX_DEPTH, MAX_SURFACE_BYTES

# Context keys the iOS action router reads for each event. Kept next to the
# catalog so a new action cannot be documented without its payload.
_ACTION_CONTEXTS = {
    "task.toggle": "{task}",
    "task.move": '{task?,to:"tomorrow"|"next_monday"}',
    "task.drop": "{task}",
    "task.create": "{title}",
    "journal.checkin": "{mood?,energy?}",
    "weight.log": "{kg}",
    "workout.start": "{workout}",
    "mission.add_steps": "{mission}",
    "week.set_focus": "{picked}",
    "card.dismiss": "{}",
}
assert set(_ACTION_CONTEXTS) == set(ALLOWED_ACTION_NAMES)

_REF_FORMS = {
    "tasks": "tasks:{" + "|".join(TASK_LISTS) + "}",
    "task": "task:<id>",
    "workout": "workout:today|workout:<id>",
    "sleep": "sleep:last_night",
    "weight": "weight:latest",
    "mission": "mission:<id>",
}
_REF_SHAPES = {
    "tasks": f"list of {{id,title,due_date,status,done}} (max {MAX_TASKS})",
    "task": "{id,title,due_date,status,done}",
    "workout": "{id,date,activity,status,exercises:[{name,sets:[{reps,weight}]}]}",
    "sleep": "{date,hours,quality,summary}",
    "weight": "{date,kg,unit,display}",
    "mission": "{id,title,next_step:{id,title,due_date}|null,steps_open}",
}


def _component_docs() -> str:
    models = get_args(get_args(Component)[0])
    docs = []
    for model in models:
        props = []
        for name, field in model.model_fields.items():
            if name in {"id", "component"}:
                continue
            props.append(name if field.is_required() else f"{name}?")
        docs.append(f"{model.__name__}({','.join(props)})")
    return " ".join(docs)


def _ref_docs() -> str:
    return "; ".join(f"{_REF_FORMS[kind]} -> {_REF_SHAPES[kind]}" for kind in REF_KINDS)


def _action_docs() -> str:
    return "; ".join(f"{name}{_ACTION_CONTEXTS[name]}" for name in ALLOWED_ACTION_NAMES)


_EXAMPLE = json.dumps(build_demo_surface("gym")[1], separators=(",", ":"))

CARD_INSTRUCTION = (
    "A2UI card\n"
    "When: add a card only when the user can act on live data right now "
    "(plan the day, a check-in, a review, a stuck project, a workout). "
    "Most replies need no card. At most ONE ```nbhd-card block, at the very end, "
    "after your text. Never mention the block in text.\n"
    "Format: one JSON envelope "
    '{"version":"1.0","createSurface":{"surfaceId":"<kebab id>",'
    '"catalogId":"' + CATALOG_ID + '","components":[...],"dataModel":{...}}}. '
    "Flat components have id and component; root id is root; containers reference "
    "children by id.\n"
    "Components: " + _component_docs() + ".\n"
    'Bindings: {"@path":"/abs/pointer"}; inside a List template, relative '
    '{"@path":"field"}.\n'
    "Data: dataModel values from the user's data MUST be refs, never copied values. " + _ref_docs() + ".\n"
    'Actions: {"event":{"name":<name>,"context":{...}}}: ' + _action_docs() + ".\n"
    f"Limits: <= {MAX_COMPONENTS} components, depth <= {MAX_DEPTH}, envelope <= "
    f"{MAX_SURFACE_BYTES} bytes, <= {MAX_DATA_REFS} data refs, text <= "
    f"{MAX_TEXT_LENGTH} chars, no URLs.\n"
    "Example:\n```nbhd-card\n" + _EXAMPLE + "\n```\n"
)
