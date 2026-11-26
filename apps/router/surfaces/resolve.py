"""Resolve live A2UI ``dataModel`` references at the owner-facing read seam.

A stored surface never carries a snapshot of the user's data: its ``dataModel``
may hold references such as ``{"$ref": "tasks:due_today"}``. Each reference is
resolved fresh, per request, through the same models/serializers the app's REST
endpoints use, and the result is substituted on the way out. Nothing resolved
here is ever stored (same rule as panels: live references, never snapshots).

Resolution is fail-soft: an unknown, foreign or broken reference becomes
``None`` and logs one content-free line. Never log a title or an id.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from apps.common.tenant_tz import tenant_today, tenant_tz_name
from apps.common.windows import Window, resolve_window

from .catalog import REF_KINDS, TASK_LISTS

logger = logging.getLogger(__name__)

MAX_TASKS = 10
_MISSION_OPEN_STEP_STATUSES = ("open", "in_progress")


class _Unresolved(Exception):
    """Internal control flow: a ref could not be resolved for a safe reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def resolve_refs(data_model: dict, *, tenant) -> dict:
    """Return a new ``data_model`` with every ``{"$ref": ...}`` substituted.

    The same ref string is resolved at most once per call. Unknown refs, rows
    owned by another tenant and resolution errors all become ``None``.
    """
    if not isinstance(data_model, dict):
        return {}
    return _walk(data_model, tenant, {})


def _walk(node: Any, tenant, cache: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if len(node) == 1 and isinstance(ref, str):
            if ref not in cache:
                cache[ref] = _resolve_ref(ref, tenant)
            return cache[ref]
        return {key: _walk(item, tenant, cache) for key, item in node.items()}
    if isinstance(node, list):
        return [_walk(item, tenant, cache) for item in node]
    return node


def _resolve_ref(ref: str, tenant) -> Any:
    kind, _, target = ref.partition(":")
    log_kind = kind if kind in REF_KINDS else "unknown"
    try:
        if kind == "tasks":
            return _tasks(tenant, target)
        if kind == "task":
            return _task(tenant, target)
        if kind == "workout":
            return _workout(tenant, target)
        if kind == "sleep":
            return _sleep(tenant, target)
        if kind == "weight":
            return _weight(tenant, target)
        if kind == "mission":
            return _mission(tenant, target)
        raise _Unresolved("unknown")
    except _Unresolved as exc:
        logger.info("a2ui_ref_unresolved kind=%s reason=%s", log_kind, exc.reason)
        return None
    except Exception:
        logger.info("a2ui_ref_unresolved kind=%s reason=%s", log_kind, "error")
        return None


# ── tasks ────────────────────────────────────────────────────────────────


def _task_list_filter(tenant, list_name: str) -> tuple[dict, bool]:
    """Map a list ref onto the query params ``TaskListCreateView`` already reads.

    Date boundaries come from ``apps.common.windows`` in the tenant's local
    timezone — the same "tenant-local date" the REST list uses — so no date
    math is reinvented here. ``overdue`` is the exception: it is an open-ended
    "strictly before today" range, so it uses ``tenant_today`` directly.
    """
    if list_name == "open":
        return {"status": "open"}, False
    if list_name == "overdue":
        # Strictly before the tenant-local today, with no lower bound, oldest
        # first — a task overdue by a week must still show up.
        return {
            "due_before_strict": tenant_today(tenant).isoformat(),
            "order_by": "due_date",
        }, True
    tz_name = tenant_tz_name(tenant)
    if list_name == "due_today":
        window = resolve_window(Window(kind="today"), tz_name)
    else:  # this_week
        window = resolve_window(Window(kind="this_week"), tz_name)
    if window is None:
        raise _Unresolved("unknown")
    start, end = window
    return {"due_after": start.isoformat(), "due_before": end.isoformat()}, True


def _tasks(tenant, list_name: str) -> list[dict]:
    if list_name not in TASK_LISTS:
        raise _Unresolved("unknown")

    from apps.journal.lifecycle_serializers import TaskSerializer
    from apps.journal.lifecycle_views import _owner_ctx, _parse_iso_date
    from apps.journal.models import Task

    params, open_only = _task_list_filter(tenant, list_name)
    # Mirrors TaskListCreateView.get's status/pillar/due_before/due_after
    # filtering and ordering, against the same model + owner serializer.
    qs = Task.objects.filter(tenant=tenant)
    for field in ("status", "pillar"):
        value = params.get(field)
        if value:
            qs = qs.filter(**{field: value})
    due_before = _parse_iso_date(params.get("due_before"), "due_before")
    due_before_strict = _parse_iso_date(params.get("due_before_strict"), "due_before_strict")
    due_after = _parse_iso_date(params.get("due_after"), "due_after")
    if due_before:
        qs = qs.filter(due_date__lte=due_before)
    if due_before_strict:
        qs = qs.filter(due_date__lt=due_before_strict)
    if due_after:
        qs = qs.filter(due_date__gte=due_after)
    if open_only:
        qs = qs.exclude(status__in=[Task.Status.DONE, Task.Status.SKIPPED])
    qs = qs.order_by(params.get("order_by", "-updated_at"))
    tasks = list(qs[:MAX_TASKS])
    return [_task_row(row) for row in TaskSerializer(tasks, many=True, context=_owner_ctx(tenant, tasks)).data]


def _task(tenant, task_id: str) -> dict:
    from apps.journal.lifecycle_serializers import TaskSerializer
    from apps.journal.lifecycle_views import _owner_ctx
    from apps.journal.models import Task

    task = Task.objects.filter(tenant=tenant, id=task_id).first()
    if task is None:
        raise _Unresolved("missing")
    return _task_row(TaskSerializer(task, context=_owner_ctx(tenant, [task])).data)


def _task_row(data: dict) -> dict:
    return {
        "id": str(data["id"]),
        "title": data["title"],
        "due_date": data.get("due_date"),
        "status": data["status"],
        "done": data["status"] == "done",
    }


# ── workouts ─────────────────────────────────────────────────────────────


def _workout(tenant, target: str) -> dict:
    from django.db.models import Case, F, IntegerField, Value, When

    from apps.fuel.models import Workout, WorkoutStatus
    from apps.fuel.serializers import WorkoutSerializer

    qs = Workout.objects.filter(tenant=tenant)
    if target == "today":
        qs = qs.filter(
            date=tenant_today(tenant),
            status__in=[WorkoutStatus.PLANNED, WorkoutStatus.IN_PROGRESS],
        ).annotate(
            _rank=Case(
                When(status=WorkoutStatus.IN_PROGRESS, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        workout = qs.order_by("_rank", F("scheduled_at").asc(nulls_last=True), "created_at").first()
    else:
        workout = qs.filter(id=target).first()
    if workout is None:
        raise _Unresolved("missing")

    data = WorkoutSerializer(workout, context={"tenant": tenant, "rehydrate": True}).data
    return {
        "id": str(data["id"]),
        "date": data["date"],
        "activity": data["activity"],
        "status": data["status"],
        "exercises": _exercises(data.get("detail_json")),
    }


def _exercises(detail: Any) -> list[dict]:
    """Project ``detail_json`` onto the small shape a surface binds to."""
    if not isinstance(detail, dict):
        return []
    out = []
    for exercise in detail.get("exercises") or []:
        if not isinstance(exercise, dict):
            continue
        sets = []
        for workout_set in exercise.get("sets") or []:
            if not isinstance(workout_set, dict):
                continue
            logged = workout_set.get("logged")
            logged = logged if isinstance(logged, dict) else {}
            reps = workout_set.get("reps")
            weight = workout_set.get("weight")
            sets.append(
                {
                    "reps": logged.get("reps") if reps is None else reps,
                    "weight": logged.get("weight") if weight is None else weight,
                }
            )
        out.append({"name": exercise.get("name", ""), "sets": sets})
    return out


# ── sleep ────────────────────────────────────────────────────────────────


def _sleep(tenant, target: str) -> dict:
    if target != "last_night":
        raise _Unresolved("unknown")

    from apps.fuel.models import SleepLog
    from apps.fuel.serializers import SleepLogSerializer

    log = SleepLog.objects.filter(tenant=tenant).order_by("-date").first()
    if log is None:
        raise _Unresolved("missing")
    data = SleepLogSerializer(log, context={"tenant": tenant, "rehydrate": True}).data
    return {
        "date": data["date"],
        "hours": float(data["duration_hours"]),
        "quality": data.get("quality"),
        "summary": _sleep_summary(data["duration_hours"]),
    }


def _sleep_summary(hours) -> str:
    """Integer-minute summary; the modulo guarantees "60m" can never print."""
    total = round(float(hours) * 60)
    return f"{total // 60}h {total % 60}m"


# ── weight ───────────────────────────────────────────────────────────────


def _weight(tenant, target: str) -> dict:
    if target != "latest":
        raise _Unresolved("unknown")

    from apps.fuel.models import BodyWeightLog

    log = BodyWeightLog.objects.filter(tenant=tenant).order_by("-date").first()
    if log is None:
        raise _Unresolved("missing")
    kg = float(log.weight_kg)
    # Fuel has no weight-unit preference (FuelProfile only carries
    # distance_unit), so the storage unit, kg, is the display unit.
    return {"date": log.date.isoformat(), "kg": kg, "unit": "kg", "display": f"{kg:.1f} kg"}


# ── missions ─────────────────────────────────────────────────────────────


def _mission(tenant, mission_id: str) -> dict:
    from apps.friends import access

    goal = access.get_mission(mission_id)
    if goal is None:
        raise _Unresolved("missing")
    membership = access.mission_memberships().filter(shared_goal=goal, tenant=tenant, status="active").first()
    if membership is None:
        raise _Unresolved("not_owned")

    steps = list(access.project_steps(goal).values("id", "title", "status", "due_date", "order"))
    open_steps = [step for step in steps if step["status"] in _MISSION_OPEN_STEP_STATUSES]
    open_steps.sort(
        key=lambda step: (
            step["due_date"] is None,
            step["due_date"] or date.max,
            step["order"],
            step["title"],
        )
    )
    next_step = None
    if open_steps:
        step = open_steps[0]
        next_step = {
            "id": str(step["id"]),
            "title": step["title"],
            "due_date": step["due_date"].isoformat() if step["due_date"] else None,
        }
    return {
        "id": str(goal.id),
        "title": goal.title,
        "next_step": next_step,
        "steps_open": len(open_steps),
    }
