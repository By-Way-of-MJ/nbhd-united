"""A2UI v1.0 surface catalog: the only components and props the app may render.

This is an allowlist, not a schema registry. Every model forbids unknown props and
every plain string is bounded and URL-free, so a payload that parses is also safe
to send to the iOS renderer. Graph checks (ids, root, cycles, depth) live in
``validate.py``; this module only owns shape.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from apps.router.panels import PANEL_KINDS

CATALOG_ID = "nbhd/v1"

ALLOWED_ACTION_NAMES = (
    "task.toggle",
    "task.move",
    "task.drop",
    "task.create",
    "journal.checkin",
    "weight.log",
    "workout.start",
    "mission.add_steps",
    "week.set_focus",
    "card.dismiss",
)
ALLOWED_ACTIONS = Literal[*ALLOWED_ACTION_NAMES]

PanelKind = Literal[*PANEL_KINDS]

# RFC 6901-ish, deliberately without ``~`` escapes: tokens are letters, digits,
# "_" and "-". Bindings may be absolute ("/sleep/summary") or relative ("title"),
# but only components inside a List template may use relative pointers; the
# validator enforces that scope.
_POINTER_TOKEN = r"[A-Za-z0-9_-]+"
POINTER_PATTERN = rf"^/(?:{_POINTER_TOKEN})(?:/{_POINTER_TOKEN})*$"
BINDING_POINTER_PATTERN = rf"^/?{_POINTER_TOKEN}(?:/{_POINTER_TOKEN})*$"
COMPONENT_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,47}$"
MAX_TEXT_LENGTH = 500

# A ``dataModel`` never carries a snapshot of the user's data. Instead it may
# hold live references that the owner-facing read seams resolve per request
# (``resolve.py``); the reference string itself is code, not content.
REF_KINDS = ("tasks", "task", "workout", "sleep", "weight", "mission")
TASK_LISTS = ("due_today", "overdue", "this_week", "open")

_UUID4 = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}"
    r"-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
REF_PATTERN = re.compile(
    rf"^(?:"
    rf"tasks:(?:{'|'.join(TASK_LISTS)})"
    rf"|task:{_UUID4}"
    rf"|workout:today"
    rf"|workout:{_UUID4}"
    rf"|sleep:last_night"
    rf"|weight:latest"
    rf"|mission:{_UUID4}"
    rf")$"
)


def _no_url(value: str) -> str:
    if "://" in value:
        raise ValueError("URLs are not allowed in surfaces")
    return value


SafeStr = Annotated[str, Field(max_length=MAX_TEXT_LENGTH), AfterValidator(_no_url)]
ComponentId = Annotated[str, Field(pattern=COMPONENT_ID_PATTERN)]


class Binding(BaseModel):
    """A JSON Pointer into the surface's dataModel."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str = Field(alias="@path", pattern=BINDING_POINTER_PATTERN, max_length=MAX_TEXT_LENGTH)


class ListTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: ComponentId
    path: str = Field(pattern=POINTER_PATTERN, max_length=MAX_TEXT_LENGTH)


class ActionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ALLOWED_ACTIONS
    context: dict[SafeStr, str | int | bool | Binding] = Field(default_factory=dict, max_length=10)


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: ActionEvent


class _Component(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: ComponentId


class _ChildrenComponent(_Component):
    children: list[ComponentId] = Field(min_length=1, max_length=20)


class Column(_ChildrenComponent):
    component: Literal["Column"]


class Row(_ChildrenComponent):
    component: Literal["Row"]


class Card(_ChildrenComponent):
    component: Literal["Card"]


class List(_Component):
    component: Literal["List"]
    children: ListTemplate
    max_items: int = Field(default=10, ge=1, le=20)


class Text(_Component):
    component: Literal["Text"]
    text: SafeStr | Binding
    style: Literal["title", "body", "caption", "label"] = "body"


class Divider(_Component):
    component: Literal["Divider"]


class Button(_Component):
    component: Literal["Button"]
    label: SafeStr | Binding
    variant: Literal["primary", "secondary"] = "secondary"
    action: Action


class CheckBox(_Component):
    component: Literal["CheckBox"]
    label: SafeStr | Binding
    value: Binding
    action: Action | None = None


class ChoicePicker(_Component):
    component: Literal["ChoicePicker"]
    label: SafeStr | None = None
    options: Annotated[list[SafeStr], Field(max_length=12)] | Binding
    max: int = Field(default=1, ge=1, le=12)
    value: Binding


class DateTimeInput(_Component):
    component: Literal["DateTimeInput"]
    label: SafeStr | None = None
    value: Binding
    mode: Literal["date", "time", "datetime"] = "datetime"


class Slider(_Component):
    component: Literal["Slider"]
    label: SafeStr
    min: int = Field(ge=0, le=100)
    max: int = Field(ge=0, le=100)
    value: Binding

    @model_validator(mode="after")
    def ordered_range(self):
        if self.min >= self.max:
            raise ValueError("min must be less than max")
        return self


class TextField(_Component):
    component: Literal["TextField"]
    label: SafeStr
    value: Binding
    max_length: int = Field(default=200, ge=1, le=500)


class Panel(_Component):
    component: Literal["Panel"]
    kind: PanelKind
    ref: SafeStr | Binding | None = None
    params: dict[SafeStr, str | int] = Field(default_factory=dict, max_length=6)


Component = Annotated[
    Column
    | Row
    | Card
    | List
    | Text
    | Divider
    | Button
    | CheckBox
    | ChoicePicker
    | DateTimeInput
    | Slider
    | TextField
    | Panel,
    Field(discriminator="component"),
]
