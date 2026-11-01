"""Private preparation records for step holders; no assistant work runs here."""

from django.db import transaction
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from . import access
from .models import StepPrep
from .project_services import _row


def payload(prep):
    return {
        "id": str(prep.id),
        "mission_id": str(prep.step.shared_goal_id),
        "step_id": str(prep.step_id),
        "project_title": prep.step.shared_goal.title,
        "status": prep.status,
        "brief": prep.brief,
        "question": prep.question,
        "choices": prep.choices,
        "doc_slug": prep.doc_slug,
        "updated_at": prep.updated_at.isoformat(),
    }


@transaction.atomic
def start(tenant, user, mission_id, step_id):
    goal, member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal).select_related("shared_goal"), step_id)
    if not access.project_assignments(goal).filter(step=step, membership=member, status="accepted").exists():
        raise PermissionDenied("Only a member holding this step can prepare it.")
    if step.status not in {"open", "in_progress"}:
        raise ValidationError("Only an open step can be prepared.")
    prep = access.my_step_preps(tenant).select_for_update(of=("self",)).filter(membership=member, step=step).first()
    if prep is not None and prep.status != StepPrep.Status.STOPPED:
        prep.step = step
        return prep
    brief = {
        "title": step.title,
        "description": step.description,
        "start_date": step.start_date.isoformat() if step.start_date else None,
        "due_date": step.due_date.isoformat() if step.due_date else None,
    }
    if prep is None:
        return access.create_step_prep(tenant, member, step, brief)
    prep.step = step
    return access.update_step_prep(
        tenant,
        prep,
        brief=brief,
        status=StepPrep.Status.WORKING,
        question="",
        choices=[],
        answer="",
        doc_slug="",
        last_seen_note_at=None,
        last_run_at=None,
    )


def _mine(tenant, mission_id, step_id):
    prep = (
        access.my_step_preps(tenant)
        .select_related("step__shared_goal")
        .select_for_update(of=("self",))
        .filter(step_id=step_id, step__shared_goal_id=mission_id)
        .first()
    )
    if prep is None:
        raise NotFound("No such preparation.")
    return prep


@transaction.atomic
def stop(tenant, mission_id, step_id):
    prep = _mine(tenant, mission_id, step_id)
    return access.update_step_prep(tenant, prep, status=StepPrep.Status.STOPPED)


@transaction.atomic
def choose(tenant, mission_id, step_id, choice):
    prep = _mine(tenant, mission_id, step_id)
    if prep.status != StepPrep.Status.NEEDS_CHOICE:
        raise ValidationError("This preparation is not waiting for a choice.")
    if not isinstance(choice, str) or len(choice) > 120 or choice not in prep.choices:
        raise ValidationError("Choose one of the offered choices.")
    return access.update_step_prep(tenant, prep, answer=choice, question="", choices=[], status=StepPrep.Status.WORKING)


def list_mine(tenant):
    access.stop_ineligible_step_preps(tenant)
    return [
        payload(prep)
        for prep in access.my_step_preps(tenant)
        .exclude(status=StepPrep.Status.STOPPED)
        .select_related("step__shared_goal")
        .order_by("-created_at", "-id")
    ]
