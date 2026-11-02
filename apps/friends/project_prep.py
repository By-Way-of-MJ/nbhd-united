"""Private preparation records and the assistant's bounded homework surface."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from django.core import signing
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from . import access
from .models import StepPrep
from .project_hygiene import UNTRUSTED_RULE, fence
from .project_services import _row


class PrepNext(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    run_token: str
    body: Annotated[str, Field(max_length=50_000)]


class PrepReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    status: Literal["ready", "stuck", "needs_choice"]
    question: Annotated[str, Field(max_length=300)] = ""
    choices: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=3)] = []
    last_seen_note_at: datetime | None = None
    run_token: str

    @model_validator(mode="after")
    def choice_required(self):
        if self.status == "needs_choice":
            if not self.question or not 2 <= len(self.choices) <= 3 or len(set(self.choices)) != len(self.choices):
                raise ValueError("needs_choice requires one question and two or three distinct choices")
        elif self.status == "stuck":
            if self.choices:
                raise ValueError("stuck cannot include choices")
        elif self.question or self.choices:
            raise ValueError("ready cannot include a question or choices")
        if self.last_seen_note_at is not None and (
            timezone.is_naive(self.last_seen_note_at) or self.last_seen_note_at > timezone.now()
        ):
            raise ValueError("The note cursor must not be in the future and must include a timezone")
        return self


def _validate(model, data):
    from .project_assistant import _pydantic_error

    try:
        return model.model_validate(data)
    except PydanticValidationError as exc:
        raise _pydantic_error(exc) from exc


def _iso(value):
    return value.isoformat() if value else None


@transaction.atomic
def runtime_next(tenant, data, *, update=False):
    _validate(PrepNext, data)
    access.stop_ineligible_step_preps(tenant)
    if not update:
        rows = (
            access.my_step_preps(tenant)
            .filter(status="working")
            .filter(Q(last_run_at__isnull=True) | Q(updated_at__gt=F("last_run_at"), answer__gt=""))
        )
    else:
        rows = access.step_preps_with_unseen_notes(tenant).filter(
            status__in=["ready", "stuck"], last_run_at__isnull=False
        )
    rows = rows.select_related("step__shared_goal").select_for_update(of=("self",))
    for prep in rows.order_by("created_at", "id"):
        notes = []
        cursor = prep.last_seen_note_at if update else None
        if update:
            notes = list(
                access.newest_shared_notes(prep.step.shared_goal, viewer=tenant, with_cursor=True).filter(
                    step_id=prep.step_id
                )
            )
            notes = [n for n in notes if cursor is None or n["created_at"] > cursor]
            if not notes:
                continue
            cursor = max(n["created_at"] for n in notes)
        from apps.billing.services import check_budget

        if check_budget(tenant):
            return {"job": None}
        snapshot = {
            "tenant": str(tenant.id),
            "id": str(prep.id),
            "version": _iso(prep.updated_at),
            "cursor": _iso(cursor),
        }
        job = {
            "id": str(prep.id),
            "brief": {
                key: fence(value, None) if isinstance(value, str) else value for key, value in prep.brief.items()
            },
            "answer": prep.answer,
            "run_token": signing.dumps(snapshot, salt="step-prep-run"),
        }
        if update:
            from apps.journal.models import Document

            job["body"] = (
                Document.objects.filter(tenant=tenant, kind="prep", slug=prep.doc_slug)
                .values_list("markdown", flat=True)
                .first()
                or ""
            )
            job["last_seen_note_at"] = _iso(cursor)
            job["notes"] = [
                {
                    "text": fence(n["text"][:400], n["author__tenant__neighbor_profile__handle"]),
                    "photo_count": n["photo_count"],
                }
                for n in notes
            ]
        return {"rule": UNTRUSTED_RULE, "job": job}
    return {"job": None}


def _runtime_row(tenant, prep_id, run_token):
    access.stop_ineligible_step_preps(tenant)
    prep = access.my_step_preps(tenant).select_for_update(of=("self",)).filter(id=prep_id).first()
    if prep is None:
        raise NotFound("No such preparation.")
    if prep.status == "stopped":
        raise ValidationError("This preparation has stopped.")
    try:
        snapshot = signing.loads(run_token, salt="step-prep-run", max_age=24 * 60 * 60)
    except signing.BadSignature as exc:
        raise ValidationError("Call next again before saving or reporting.") from exc
    expected = {"tenant": str(tenant.id), "id": str(prep.id), "version": _iso(prep.updated_at)}
    if not isinstance(snapshot, dict) or {k: snapshot.get(k) for k in expected} != expected:
        raise ValidationError("The preparation changed. Call next again.")
    return prep, snapshot


def runtime_save(tenant, data):
    from apps.integrations.runtime_views import _put_runtime_document

    spec = _validate(PrepSave, data)
    with transaction.atomic():
        prep, _snapshot = _runtime_row(tenant, spec.id, spec.run_token)
        slug = f"step-prep-{prep.id}"
        title = prep.brief["title"]
    # Authoring may call out: release the row and request transaction first.
    doc, _created = _put_runtime_document(tenant, kind="prep", slug=slug, title=title, markdown=spec.body)
    failure = None
    with transaction.atomic():
        try:
            prep, _snapshot = _runtime_row(tenant, spec.id, spec.run_token)
        except (NotFound, ValidationError) as exc:
            # Stop may have run before this write landed. Commit the cleanup
            # before refusing; don't remove a newer concurrent replacement.
            access.delete_step_prep_written_page(tenant, doc)
            failure = exc
        else:
            # Publishing the coordinate must not invalidate the report token.
            access.publish_step_prep_page(tenant, prep, slug)
    if failure is not None:
        raise failure
    return {"id": str(prep.id), "saved": True}


@transaction.atomic
def runtime_report(tenant, data):
    spec = _validate(PrepReport, data)
    prep, snapshot = _runtime_row(tenant, spec.id, spec.run_token)
    if snapshot.get("cursor") != _iso(spec.last_seen_note_at):
        raise ValidationError("The note cursor was not returned by next. Call next again.")
    access.update_step_prep(
        tenant,
        prep,
        status=spec.status,
        question=spec.question,
        choices=spec.choices,
        last_run_at=timezone.now(),
        last_seen_note_at=spec.last_seen_note_at,
    )
    return {"id": str(prep.id), "status": prep.status, "doc_slug": prep.doc_slug}


def payload(prep):
    from apps.pii.redactor import rehydrate_for_tenant

    tenant = prep.tenant
    return {
        "id": str(prep.id),
        "mission_id": str(prep.step.shared_goal_id),
        "step_id": str(prep.step_id),
        "project_title": prep.step.shared_goal.title,
        "status": prep.status,
        "brief": prep.brief,
        "question": rehydrate_for_tenant(tenant, prep.question),
        "choices": [rehydrate_for_tenant(tenant, choice) for choice in prep.choices],
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
    access.delete_step_prep_pages(tenant, [prep.id])
    return access.update_step_prep(tenant, prep, status=StepPrep.Status.STOPPED, doc_slug="")


@transaction.atomic
def choose(tenant, mission_id, step_id, choice):
    prep = _mine(tenant, mission_id, step_id)
    if prep.status != StepPrep.Status.NEEDS_CHOICE:
        raise ValidationError("This preparation is not waiting for a choice.")
    from apps.pii.redactor import rehydrate_for_tenant

    offered = [rehydrate_for_tenant(tenant, value) for value in prep.choices]
    if not isinstance(choice, str) or len(choice) > 120 or choice not in offered:
        raise ValidationError("Choose one of the offered choices.")
    return access.update_step_prep(
        tenant,
        prep,
        answer=prep.choices[offered.index(choice)],
        question="",
        choices=[],
        status=StepPrep.Status.WORKING,
        last_seen_note_at=None,
    )


def list_mine(tenant):
    access.stop_ineligible_step_preps(tenant)
    return [
        payload(prep)
        for prep in access.my_step_preps(tenant)
        .exclude(status=StepPrep.Status.STOPPED)
        .select_related("step__shared_goal", "tenant")
        .order_by("-created_at", "-id")
    ]
