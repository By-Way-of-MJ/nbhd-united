"""Projects v2 pushes (DIRECTIVE_neighborhood_projects.md §3.7).

Visible APNs alerts with typed routing (``type`` + ``mission_id`` / ``step_id``) so
iOS opens the project, the step, or the ask sheet. Rules:

* Lock-screen privacy: a body carries names and step/project titles only — never a
  note, a counter-offer reason, or assistant text.
* Respect ``SharedGoalMembership.muted`` and friends blocks; never push the actor.
* Off the request thread (after commit) and best-effort: a push failure never breaks
  the write that caused it.
"""

from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

TITLE_CAP = 60


def _short(text: str, cap: int = TITLE_CAP) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def _name(tenant_id) -> str:
    from .models import NeighborProfile

    profile = NeighborProfile.objects.filter(tenant_id=tenant_id).only("display_name").first()
    return profile.display_name if profile and profile.display_name else "A neighbor"


def _deliver(memberships, *, ptype: str, body: str, mission_id, step_id=None, actor_tenant_id=None) -> int:
    """Synchronous core (tests call it directly). Returns how many users were pushed."""
    from apps.common.apns import apns_configured

    if not apns_configured():
        return 0
    from apps.router.push_views import _push_to_user_devices

    from . import access

    blocked = access.blocked_counterpart_ids(actor_tenant_id) if actor_tenant_id else set()
    sent = 0
    extra = {"type": ptype, "mission_id": str(mission_id)}
    if step_id:
        extra["step_id"] = str(step_id)
    for membership in memberships:
        if membership.muted or membership.tenant_id == actor_tenant_id or membership.tenant_id in blocked:
            continue
        try:
            _push_to_user_devices(
                membership.user,
                body=body,
                thread_id=None,
                collapse_id=f"{ptype}-{step_id or mission_id}"[:64],
                content_available=True,
                extra=extra,
            )
            sent += 1
        except Exception:  # noqa: BLE001 — one device failure must not stop the rest
            logger.warning("project push %s failed for membership %s", ptype, membership.id, exc_info=True)
    return sent


def _dispatch(fn) -> None:
    """Run after commit, off the request thread (mirrors notify_friend_message)."""

    def _run():
        try:
            from . import access

            with access.backstop_service_context():
                fn()
        except Exception:  # noqa: BLE001
            logger.exception("project push dispatch failed")

    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        transaction.on_commit(_run)
    else:
        transaction.on_commit(lambda: threading.Thread(target=_run, daemon=True).start())


def _members(goal, *, ids=None, statuses=("active",)):
    from . import access

    qs = access.mission_memberships().filter(shared_goal=goal, status__in=list(statuses)).select_related("user")
    if ids is not None:
        qs = qs.filter(id__in=list(ids))
    return list(qs)


# ── Events ───────────────────────────────────────────────────────────────────


def notify_project_invite(goal, actor_tenant) -> None:
    def fn():
        body = f"{_name(actor_tenant.id)} invited you to “{_short(goal.title)}”"
        _deliver(
            _members(goal, statuses=("invited",)),
            ptype="project_invite",
            body=body,
            mission_id=goal.id,
            actor_tenant_id=actor_tenant.id,
        )

    _dispatch(fn)


def notify_step_ask(goal, step, membership_ids, actor_tenant) -> None:
    ids = list(membership_ids)
    title = step.title

    def fn():
        body = f"{_name(actor_tenant.id)} asked you to take “{_short(title)}”"
        _deliver(
            _members(goal, ids=ids, statuses=("active", "invited")),
            ptype="step_ask",
            body=body,
            mission_id=goal.id,
            step_id=step.id,
            actor_tenant_id=actor_tenant.id,
        )

    _dispatch(fn)


_ANSWER_TEXT = {
    "yes": "said yes to “{title}”",
    "dates": "suggested other dates for “{title}”",
    "smaller": "offered to take part of “{title}”",
    "no": "can’t take “{title}” this time",
}


def notify_step_answer(goal, step, answer: str, responder_tenant, asker_tenant_id) -> None:
    """Tell the person who asked. Only the answer — never the note or reason."""
    title = step.title

    def fn():
        from . import access

        asker = (
            access.mission_memberships()
            .filter(shared_goal=goal, tenant_id=asker_tenant_id, status="active")
            .select_related("user")
        )
        body = f"{_name(responder_tenant.id)} " + _ANSWER_TEXT.get(answer, "answered about “{title}”").format(
            title=_short(title)
        )
        _deliver(
            list(asker),
            ptype="step_answer",
            body=body,
            mission_id=goal.id,
            step_id=step.id,
            actor_tenant_id=responder_tenant.id,
        )

    _dispatch(fn)


def notify_step_unblocked(goal, step_id, blocker_title: str, actor_tenant) -> None:
    """Every blocker of this step is done → its accepted owners can start."""

    def fn():
        from . import access

        step = access.project_steps(goal).filter(id=step_id).first()
        if step is None or step.status in {"done", "skipped"}:
            return
        owner_ids = (
            access.project_assignments(goal)
            .filter(step=step, status="accepted")
            .values_list("membership_id", flat=True)
        )
        body = f"“{_short(step.title)}” can start now — “{_short(blocker_title, 40)}” is done"
        _deliver(
            _members(goal, ids=owner_ids),
            ptype="step_unblocked",
            body=body,
            mission_id=goal.id,
            step_id=step.id,
            actor_tenant_id=actor_tenant.id if actor_tenant else None,
        )

    _dispatch(fn)


def notify_milestone_reached(goal, milestone_title: str, actor_tenant) -> None:
    def fn():
        body = f"Milestone reached in “{_short(goal.title, 40)}”: {_short(milestone_title, 50)}"
        _deliver(
            _members(goal),
            ptype="milestone_reached",
            body=body,
            mission_id=goal.id,
            actor_tenant_id=actor_tenant.id if actor_tenant else None,
        )

    _dispatch(fn)


# ── "Due tomorrow" (hourly cron) ─────────────────────────────────────────────

NUDGE_LOCAL_HOUR = 9


def run_due_nudges(now=None) -> dict:
    """One gentle reminder per (owner, step, due date), at 09:00 in the owner's own
    time zone the day before a step is due. Claimed with a compare-and-set on
    ``due_nudged_for`` so overlapping runs never double-send. Never nags twice."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.common.tenant_tz import tenant_tz

    from . import access
    from .models import SharedGoalStepAssignment
    from .project_flags import projects_v2_enabled

    now = now or timezone.now()
    sent = claimed = 0
    with access.backstop_service_context():
        rows = SharedGoalStepAssignment.objects.filter(
            status="accepted",
            membership__status="active",
            membership__muted=False,
            step__status__in=["open", "in_progress"],
            step__due_date__isnull=False,
            step__shared_goal__status="active",
        ).select_related("step", "step__shared_goal", "membership", "membership__user", "membership__tenant")
        for row in rows:
            tenant = row.membership.tenant
            if not projects_v2_enabled(tenant):
                continue
            local = now.astimezone(tenant_tz(tenant))
            due = row.step.due_date
            if local.hour != NUDGE_LOCAL_HOUR or due != local.date() + timedelta(days=1):
                continue
            if row.due_nudged_for == due:
                continue
            won = (
                SharedGoalStepAssignment.objects.filter(id=row.id)
                .exclude(due_nudged_for=due)
                .update(due_nudged_for=due)
            )
            if not won:
                continue
            claimed += 1
            body = f"“{_short(row.step.title)}” is due tomorrow"
            sent += _deliver(
                [row.membership], ptype="step_due", body=body, mission_id=row.step.shared_goal_id, step_id=row.step_id
            )
    return {"claimed": claimed, "sent": sent}
