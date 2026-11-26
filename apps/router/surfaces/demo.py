"""Fixed demo A2UI surfaces for the ``post_demo_surface`` operator task.

Each surface stores layout, live ``$ref`` entries and static English copy only.
No user data is captured at build time: the owner-facing read seam resolves the
references fresh per request (see ``resolve.py``).
"""

from __future__ import annotations

import logging

from apps.router.chat_gates import chat_surfaces_enabled
from apps.router.proactive_context import record_proactive_outbound
from apps.tenants.models import Tenant

from .catalog import CATALOG_ID
from .validate import validate_surface

logger = logging.getLogger(__name__)

SCENARIOS = ("gym", "review", "stuck", "rough")
JOB_NAME = "a2ui_demo"


def _gym() -> tuple[str, dict, str]:
    components = [
        {
            "id": "root",
            "component": "Card",
            "children": [
                "title",
                "sleep-summary",
                "workout-panel",
                "due-label",
                "due-list",
                "actions",
                "energy",
            ],
        },
        {"id": "title", "component": "Text", "text": "Tonight, go easier", "style": "title"},
        {"id": "sleep-summary", "component": "Text", "text": {"@path": "/sleep/summary"}, "style": "caption"},
        {"id": "workout-panel", "component": "Panel", "kind": "workout", "ref": {"@path": "/workout/id"}},
        {"id": "due-label", "component": "Text", "text": "Due today", "style": "label"},
        {
            "id": "due-list",
            "component": "List",
            "children": {"template": "due-task", "path": "/tasks"},
            "max_items": 5,
        },
        {
            "id": "due-task",
            "component": "CheckBox",
            "label": {"@path": "title"},
            "value": {"@path": "done"},
            "action": {"event": {"name": "task.toggle", "context": {"task": {"@path": "id"}}}},
        },
        {"id": "actions", "component": "Row", "children": ["start-workout", "move-first-task"]},
        {
            "id": "start-workout",
            "component": "Button",
            "label": "Start workout",
            "variant": "primary",
            "action": {"event": {"name": "workout.start", "context": {"workout": {"@path": "/workout/id"}}}},
        },
        {
            "id": "move-first-task",
            "component": "Button",
            "label": "Move first task to tomorrow",
            "action": {"event": {"name": "task.move", "context": {"to": "tomorrow"}}},
        },
        {
            "id": "energy",
            "component": "Slider",
            "label": "Energy right now",
            "min": 1,
            "max": 10,
            "value": {"@path": "/checkin/energy"},
        },
    ]
    data_model = {
        "sleep": {"$ref": "sleep:last_night"},
        "workout": {"$ref": "workout:today"},
        "tasks": {"$ref": "tasks:due_today"},
        "checkin": {"energy": 5},
    }
    return "Short night, so let's keep tonight lighter.", components, data_model


def _review() -> tuple[str, dict, str]:
    components = [
        {
            "id": "root",
            "component": "Card",
            "children": ["title", "slipped-label", "overdue-list", "focus-label", "focus-picker", "lock-week"],
        },
        {"id": "title", "component": "Text", "text": "Your week", "style": "title"},
        {"id": "slipped-label", "component": "Text", "text": "Slipped", "style": "label"},
        {
            "id": "overdue-list",
            "component": "List",
            "children": {"template": "slip", "path": "/overdue"},
        },
        {
            "id": "slip",
            "component": "Row",
            "children": ["slip-title", "slip-move", "slip-drop"],
        },
        {"id": "slip-title", "component": "Text", "text": {"@path": "title"}},
        {
            "id": "slip-move",
            "component": "Button",
            "label": "Move",
            "action": {
                "event": {
                    "name": "task.move",
                    "context": {"task": {"@path": "id"}, "to": "next_monday"},
                }
            },
        },
        {
            "id": "slip-drop",
            "component": "Button",
            "label": "Drop",
            "action": {"event": {"name": "task.drop", "context": {"task": {"@path": "id"}}}},
        },
        {"id": "focus-label", "component": "Text", "text": "Top 3 for next week", "style": "label"},
        {
            "id": "focus-picker",
            "component": "ChoicePicker",
            "options": [
                "Ship the proposal",
                "4 workouts",
                "Lights out by 11",
                "Call family",
                "One mission step",
            ],
            "max": 3,
            "value": {"@path": "/focus/picked"},
        },
        {
            "id": "lock-week",
            "component": "Button",
            "label": "Lock in my week",
            "variant": "primary",
            "action": {
                "event": {
                    "name": "week.set_focus",
                    "context": {"picked": {"@path": "/focus/picked"}},
                }
            },
        },
    ]
    data_model = {
        "overdue": {"$ref": "tasks:overdue"},
        "focus": {"picked": []},
    }
    return "Here's your week. Two quick decisions, then pick a focus.", components, data_model


def _stuck_components(mission_id: str) -> tuple[list[dict], dict]:
    components = [
        {
            "id": "root",
            "component": "Card",
            "children": ["mission-title", "next-label", "next-title", "project-panel", "when", "actions"],
        },
        {"id": "mission-title", "component": "Text", "text": {"@path": "/mission/title"}, "style": "title"},
        {"id": "next-label", "component": "Text", "text": "Next step", "style": "caption"},
        {"id": "next-title", "component": "Text", "text": {"@path": "/mission/next_step/title"}},
        {"id": "project-panel", "component": "Panel", "kind": "project"},
        {
            "id": "when",
            "component": "DateTimeInput",
            "label": "When will you do it?",
            "value": {"@path": "/plan/when"},
            "mode": "datetime",
        },
        {"id": "actions", "component": "Row", "children": ["add-steps", "not-this-week"]},
        {
            "id": "add-steps",
            "component": "Button",
            "label": "Add smaller steps",
            "variant": "primary",
            "action": {
                "event": {
                    "name": "mission.add_steps",
                    "context": {"mission": {"@path": "/mission/id"}},
                }
            },
        },
        {
            "id": "not-this-week",
            "component": "Button",
            "label": "Not this week",
            "action": {"event": {"name": "card.dismiss"}},
        },
    ]
    data_model = {
        "mission": {"$ref": f"mission:{mission_id}"},
        "plan": {"when": None},
    }
    return components, data_model


def _rough() -> tuple[str, dict, str]:
    components = [
        {
            "id": "root",
            "component": "Card",
            "children": ["title", "mood", "energy", "help", "off-plate", "save"],
        },
        {"id": "title", "component": "Text", "text": "Let's set it down", "style": "title"},
        {
            "id": "mood",
            "component": "ChoicePicker",
            "label": "How it feels",
            "options": ["Drained", "Anxious", "Flat", "Frustrated", "Okay, actually"],
            "max": 2,
            "value": {"@path": "/mood"},
        },
        {
            "id": "energy",
            "component": "Slider",
            "label": "Energy",
            "min": 1,
            "max": 10,
            "value": {"@path": "/energy"},
        },
        {
            "id": "help",
            "component": "ChoicePicker",
            "label": "One small thing",
            "options": ["10-minute walk", "5 minutes of breathing", "Early night"],
            "max": 1,
            "value": {"@path": "/help"},
        },
        {
            "id": "off-plate",
            "component": "TextField",
            "label": "Anything you want off your plate tomorrow?",
            "value": {"@path": "/off_plate"},
        },
        {
            "id": "save",
            "component": "Button",
            "label": "Save to journal",
            "variant": "primary",
            "action": {
                "event": {
                    "name": "journal.checkin",
                    "context": {"mood": {"@path": "/mood"}, "energy": {"@path": "/energy"}},
                }
            },
        },
    ]
    data_model = {"mood": [], "energy": 4, "help": [], "off_plate": ""}
    return "Thanks for telling me. Tap whatever fits.", components, data_model


def build_demo_surface(scenario: str, *, mission_id: str | None = None) -> tuple[str, dict]:
    """Build one fixed demo surface and its static message text.

    ``envelope`` is validated before it is returned. The ``stuck`` scenario
    needs a mission to reference and raises ``ValueError`` without one.
    """
    if scenario == "gym":
        message_text, components, data_model = _gym()
        surface_id = "gym-tonight"
    elif scenario == "review":
        message_text, components, data_model = _review()
        surface_id = "week-review"
    elif scenario == "stuck":
        if not mission_id:
            raise ValueError("mission_id required")
        components, data_model = _stuck_components(mission_id)
        message_text = "Let's get this moving with smaller steps."
        surface_id = "mission-unstick"
    elif scenario == "rough":
        message_text, components, data_model = _rough()
        surface_id = "checkin"
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    envelope = {
        "version": "1.0",
        "createSurface": {
            "surfaceId": surface_id,
            "catalogId": CATALOG_ID,
            "components": components,
            "dataModel": data_model,
        },
    }
    validated = validate_surface(envelope)
    assert validated
    return message_text, validated


def post_demo_surface(tenant_id: str, scenario: str, mission_id: str | None = None) -> dict:
    """Post one demo surface as an app-channel proactive assistant message."""
    tenant = Tenant.objects.get(pk=tenant_id)
    if not chat_surfaces_enabled(tenant):
        return {"status": "refused", "reason": "gate"}
    if scenario not in SCENARIOS:
        return {"status": "refused", "reason": "unknown_scenario"}

    message_text, envelope = build_demo_surface(scenario, mission_id=mission_id)
    row = record_proactive_outbound(
        tenant=tenant,
        channel="app",
        channel_user_id=str(tenant.user_id),
        message_text=message_text,
        job_name=JOB_NAME,
        a2ui=[envelope],
    )
    if row is None:
        return {"status": "error"}
    return {"status": "posted", "id": str(row.id), "scenario": scenario}


def post_demo_surface_task(tenant_id: str, scenario: str, mission_id: str | None = None) -> dict:
    """QStash task wrapper: never raises, so a bad fire cannot DLQ-loop."""
    try:
        return post_demo_surface(tenant_id, scenario, mission_id=mission_id)
    except Exception:
        logger.exception(
            "a2ui_demo: post failed tenant=%s scenario=%s",
            str(tenant_id)[:8],
            scenario,
        )
        return {"status": "error"}
