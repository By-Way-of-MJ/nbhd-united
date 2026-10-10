"""Model I/O contracts for the Projects v2 assistant tools (pydantic v2).

Portfolio STRUCTURED_OUTPUT rule: every assistant-authored payload is validated
against a strict model (``extra="forbid"``, bounded arrays, ``Literal`` vocabularies
from the tuples below) before any business rule runs. The plugin's tool JSON
schemas mirror these models; ``test_projects_v2_p1c`` pins that they agree.

Cross-object truth (step ids exist in THIS project, owners are the user's
neighbors, the dependency graph has no cycle, the user may make the change) is
checked by ``project_assistant`` against the database, not here.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "projects-v2-assistant-1"

MAX_DRAFT_MILESTONES = 8
MAX_DRAFT_STEPS = 40
MAX_CHANGES = 12

# Every kind a proposal may carry. Own-side kinds apply on approval; ask_member is
# SENT as the user's request and the other member still answers it.
CHANGE_KINDS = (
    "move_step",  # new start/due for a step (server checks the user may edit it)
    "add_step",  # a new step; owner "me" (I take it) or "open" (nobody yet)
    "mark_done",  # my own accepted step
    "reopen",  # my own accepted step
    "add_dependency",  # blocker must finish before blocked starts
    "remove_dependency",
    "ask_member",  # ask a member (by @handle) to take a step — their answer, their choice
    "link_goal",  # link the project to one of MY OWN Horizons goals (or clear)
)
OWN_SIDE_KINDS = frozenset(CHANGE_KINDS) - {"ask_member"}
ADD_STEP_OWNERS = ("me", "open")

ChangeKind = Literal[CHANGE_KINDS]  # type: ignore[valid-type]
AddStepOwner = Literal[ADD_STEP_OWNERS]  # type: ignore[valid-type]

Title = Field(min_length=1, max_length=120)
ShortText = Field(default="", max_length=500)
Key = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")


def vocabulary_sentence(field: str, members: tuple[str, ...]) -> str:
    """The same tuple renders the tool description line and the schema enum."""
    return f"{field} must be exactly one of: {', '.join(members)}."


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ── Draft (the assistant writes a private starter plan for its own human) ──────


class DraftMilestone(Strict):
    key: str = Key
    title: str = Title
    target_date: date | None = None


class DraftStep(Strict):
    key: str = Key
    title: str = Title
    description: str = ShortText
    start_date: date | None = None
    due_date: date | None = None
    milestone_key: str | None = Field(default=None, max_length=24)
    # A neighbor's @handle, "me", or null for "anyone". Suggestions only: nobody is
    # asked until the human publishes the draft, and then they still answer.
    owner: str | None = Field(default=None, max_length=40)
    depends_on: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _dates_in_order(self) -> DraftStep:
        if self.start_date and self.due_date and self.start_date > self.due_date:
            raise ValueError(f"step {self.key}: start_date must be on or before due_date")
        return self


class ProjectDraftSpec(Strict):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    title: str = Title
    goal: str = ShortText
    milestones: list[DraftMilestone] = Field(default_factory=list, max_length=MAX_DRAFT_MILESTONES)
    steps: list[DraftStep] = Field(min_length=1, max_length=MAX_DRAFT_STEPS)

    @model_validator(mode="after")
    def _keys_are_consistent(self) -> ProjectDraftSpec:
        step_keys = [s.key for s in self.steps]
        milestone_keys = {m.key for m in self.milestones}
        if len(set(step_keys)) != len(step_keys):
            raise ValueError("step keys must be unique")
        if len(milestone_keys) != len(self.milestones):
            raise ValueError("milestone keys must be unique")
        known = set(step_keys)
        for step in self.steps:
            if step.milestone_key and step.milestone_key not in milestone_keys:
                raise ValueError(f"step {step.key}: unknown milestone_key {step.milestone_key}")
            for dep in step.depends_on:
                if dep not in known or dep == step.key:
                    raise ValueError(f"step {step.key}: depends_on must name other step keys")
        _assert_acyclic({s.key: list(s.depends_on) for s in self.steps})
        return self


def _assert_acyclic(parents: dict[str, list[str]]) -> None:
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        if state.get(node) == 1:
            raise ValueError("depends_on must not form a cycle")
        if state.get(node) == 2:
            return
        state[node] = 1
        for parent in parents.get(node, []):
            visit(parent)
        state[node] = 2

    for key in parents:
        visit(key)


# ── Proposal (the assistant suggests changes; the human approves) ───────────────


class ProjectChange(Strict):
    kind: ChangeKind
    step_id: str | None = Field(default=None, max_length=36)
    start_date: date | None = None
    due_date: date | None = None
    title: str | None = Field(default=None, max_length=120)
    milestone_id: str | None = Field(default=None, max_length=36)
    owner: AddStepOwner | None = None
    waits_on: list[str] = Field(default_factory=list, max_length=10)
    blocker_id: str | None = Field(default=None, max_length=36)
    blocked_id: str | None = Field(default=None, max_length=36)
    dependency_id: str | None = Field(default=None, max_length=36)
    member_handle: str | None = Field(default=None, max_length=40)
    goal_id: str | None = Field(default=None, max_length=36)
    clear_goal: bool = False

    @model_validator(mode="after")
    def _fields_match_kind(self) -> ProjectChange:
        need = {
            "move_step": ("step_id",),
            "add_step": ("title",),
            "mark_done": ("step_id",),
            "reopen": ("step_id",),
            "add_dependency": ("blocker_id", "blocked_id"),
            "remove_dependency": ("dependency_id",),
            "ask_member": ("step_id", "member_handle"),
        }.get(self.kind, ())
        missing = [f for f in need if not getattr(self, f)]
        if missing:
            raise ValueError(f"{self.kind} needs {', '.join(missing)}")
        if self.kind == "move_step" and not (self.start_date or self.due_date):
            raise ValueError("move_step needs start_date or due_date")
        if self.kind == "link_goal" and not (self.goal_id or self.clear_goal):
            raise ValueError("link_goal needs goal_id or clear_goal")
        if self.start_date and self.due_date and self.start_date > self.due_date:
            raise ValueError("start_date must be on or before due_date")
        return self


class ProjectProposalSpec(Strict):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    summary: str = Field(min_length=1, max_length=200)
    changes: list[ProjectChange] = Field(min_length=1, max_length=MAX_CHANGES)
