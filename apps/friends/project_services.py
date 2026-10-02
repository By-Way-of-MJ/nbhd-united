"""Projects v2 writes. Every mutation serializes on the project row.

Text authoring for a private journal mirror runs before the transaction. The
locked recheck is the lease validation: a changed step asks the caller to retry.
No other tenant's journal is ever loaded or changed.
"""

from datetime import date
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from . import access, plan_projection, services
from .project_hygiene import clean_text


class Conflict(APIException):
    status_code = 409
    default_detail = "This step changed since you loaded it — refresh and try again."

    def __init__(self, detail=None):
        super().__init__(detail)
        if isinstance(detail, dict) and "version" in detail:
            self.detail["version"] = detail["version"]


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("A valid ID is required.") from exc


def _date(value):
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        raise ValidationError("Dates must be YYYY-MM-DD.") from exc


def _row(qs, pk):
    row = qs.filter(id=_uuid(pk)).first()
    if row is None:
        raise NotFound("No such project item.")
    return row


def _fields(data, *, milestone=False, creating=False):
    allowed = (
        {"title", "order", "target_date"}
        if milestone
        else {"title", "description", "start_date", "due_date", "milestone_id", "order", "status", "version"}
    )
    if set(data) - allowed:
        raise ValidationError("Unknown project fields.")
    result = {}
    if creating or "title" in data:
        result["title"] = clean_text(data.get("title"), limit=120, required=True)
    if "description" in data:
        result["description"] = clean_text(data["description"])
    for field in {"target_date", "start_date", "due_date"} & set(data):
        result[field] = _date(data[field])
    if "order" in data:
        if type(data["order"]) is not int or not -(2**31) <= data["order"] < 2**31:
            raise ValidationError("order must be an integer.")
        result["order"] = data["order"]
    if "milestone_id" in data:
        result["milestone_id"] = _uuid(data["milestone_id"]) if data["milestone_id"] else None
    if "status" in data:
        if data["status"] not in {"open", "in_progress"}:
            raise ValidationError("Use complete/reopen to change completion.")
        result["status"] = data["status"]
    return result


def _validate_step(goal, fields, step=None):
    if fields.get("milestone_id"):
        _row(access.project_milestones(goal), fields["milestone_id"])
    start = fields.get("start_date", getattr(step, "start_date", None))
    due = fields.get("due_date", getattr(step, "due_date", None))
    if start and due and start > due:
        raise ValidationError("Start must be on or before due date.")
    if step and step.status in {"done", "skipped", "in_review"} and "status" in fields:
        raise ValidationError("Use reopen to change completion.")


def _assert_step_editor(goal, member, step):
    """Return the caller's accepted assignment, if any, after checking edit rights.

    A creator retains control until another member is asked or accepts. Historical
    declined/countered assignments do not reserve editing rights for that member.
    Call under the project lock before mutating; PATCH also checks before authoring.
    """
    assignments = access.project_assignments(goal).filter(step=step)
    own = assignments.filter(membership=member, status="accepted").first()
    if own or member.role == "owner":
        return own
    if (
        step.created_by_id == member.tenant_id
        and not assignments.filter(status__in=["asked", "accepted"]).exclude(membership=member).exists()
    ):
        return None
    raise PermissionDenied("Ask the step's owner to change it.")


def _check_step_version(step, data):
    if type(data.get("version")) is not int:
        raise ValidationError("version is required and must be an integer.")
    if data["version"] != step.version:
        raise Conflict({"detail": Conflict.default_detail, "version": step.version})


def get_plan(tenant, mission_id):
    from apps.common.tenant_tz import tenant_tz

    goal, member = services._assert_mission_member(tenant, mission_id)
    plan = plan_projection.build_plan(
        access.project_snapshot(goal, viewer=tenant), today=timezone.now().astimezone(tenant_tz(tenant)).date()
    )
    plan.update(
        my_membership_id=str(member.id),
        my_role=member.role,
        # Invitations are checked against the creator's neighbors, so only the
        # creator (while an owner) can add people.
        can_invite=member.role == "owner" and goal.created_by_id == tenant.id,
    )
    return plan


MAX_PROJECT_PEOPLE = 12


@transaction.atomic
def add_members(tenant, mission_id, friendship_ids):
    """Invite more of MY neighbors into a project I started. They still choose to
    join; a push goes only to the people invited now. Returns the new invitations."""
    from apps.tenants.models import Tenant

    if not isinstance(friendship_ids, list) or not friendship_ids or len(friendship_ids) > MAX_PROJECT_PEOPLE:
        raise ValidationError("member_friendship_ids must be a non-empty list.")
    goal, member = access.lock_project(tenant, mission_id)
    if member.role != "owner" or goal.created_by_id != tenant.id:
        raise PermissionDenied("Only the person who started the project can add people.")
    edges = [access.assert_neighbors(tenant, value) for value in friendship_ids]
    other_ids = {e.addressee_id if e.requester_id == tenant.id else e.requester_id for e in edges}
    existing = {m.tenant_id: m for m in access.mission_memberships().filter(shared_goal=goal)}
    live = sum(m.status in {"active", "invited"} for m in existing.values())
    invited = []
    for other in Tenant.objects.select_related("user").filter(id__in=other_ids).order_by("id"):
        current = existing.get(other.id)
        if current and current.status in {"active", "invited"}:
            continue
        if live >= MAX_PROJECT_PEOPLE:
            raise ValidationError(f"A project can have up to {MAX_PROJECT_PEOPLE} people.")
        if current:
            current.status, current.left_at = "invited", None
            current.save(update_fields=["status", "left_at"])
        else:
            current = access.mission_memberships().create(
                shared_goal=goal, tenant=other, user=other.user, role="member", status="invited"
            )
        invited.append(current.id)
        live += 1
    if invited:
        from .project_notifications import notify_project_invite

        notify_project_invite(goal, tenant, membership_ids=invited)
    return invited


@transaction.atomic
def delete_project(tenant, mission_id):
    """An owner removes the project for everyone. Rows stay — the project turns
    ``abandoned`` and every membership ``left`` — so nobody's own journal Tasks are
    touched, and it drops out of lists, plans, nudges and assistant context, which
    all read active memberships only."""
    goal, member = access.lock_project(tenant, mission_id)
    if member.role != "owner":
        raise PermissionDenied("Only a project owner can delete it.")
    access.mission_memberships().filter(shared_goal=goal, status__in=["active", "invited"]).update(
        status="left", left_at=timezone.now()
    )
    access.set_mission_status(goal, "abandoned")
    access.expire_project_proposals(goal)


def linked_projects(tenant):
    """My projects linked to one of MY Horizons goals, for that goal's card in
    Horizons: progress plus my own next open step."""
    out = []
    for member in access.my_active_project_memberships(tenant).filter(linked_goal_id__isnull=False):
        goal = member.shared_goal
        steps = list(access.project_steps(goal).values("id", "title", "status", "start_date", "due_date"))
        mine = set(
            access.project_assignments(goal)
            .filter(membership=member, status="accepted")
            .values_list("step_id", flat=True)
        )
        open_mine = sorted(
            (s for s in steps if s["id"] in mine and s["status"] in {"open", "in_progress"}),
            key=lambda s: (s["due_date"] or s["start_date"] or date.max, s["title"]),
        )
        nxt = open_mine[0] if open_mine else None
        out.append(
            {
                "mission_id": str(goal.id),
                "title": goal.title,
                "linked_goal_id": str(member.linked_goal_id),
                "done_count": sum(s["status"] == "done" for s in steps),
                "total": len(steps),
                "next_step": (
                    {"id": str(nxt["id"]), "title": nxt["title"], "due_date": nxt["due_date"]} if nxt else None
                ),
            }
        )
    return out


@transaction.atomic
def create_step(tenant, user, mission_id, data):
    goal, _ = access.lock_project(tenant, mission_id)
    fields = _fields(data, creating=True)
    _validate_step(goal, fields)
    if access.project_steps(goal).count() >= 60:
        raise ValidationError("A project can have at most 60 steps.")
    step = access.project_steps(goal).create(shared_goal=goal, created_by=tenant, **fields)
    services._append_update(goal, tenant, user, "step_added", text=step.title, payload={"step_id": str(step.id)})
    _refresh_milestones(goal, tenant, user)
    return step


def patch_step(tenant, mission_id, step_id, data):
    from apps.journal.models import Task
    from apps.pii.authoring import author_text, truncate_placeholder_safe

    goal, member = services._assert_mission_member(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    assignment = _assert_step_editor(goal, member, step)
    _check_step_version(step, data)
    fields = _fields(data)
    _validate_step(goal, fields, step)
    mirror = bool({"title", "start_date", "due_date"} & fields.keys())
    linked_task_id = assignment.task_id if assignment and mirror else None
    # Journal authoring may do external work. Prepare before taking either lock.
    authored_title = (
        author_text(
            tenant, fields["title"], seam="friends.project.local_task.update", writer="background", field="title"
        )
        if linked_task_id and "title" in fields
        else None
    )
    with transaction.atomic():
        # Same order as completion and Task.save's synchronous receiver.
        task = (
            Task.objects.select_for_update().filter(id=linked_task_id, tenant=tenant).first()
            if linked_task_id
            else None
        )
        goal, member = access.lock_project(tenant, mission_id)
        step = _row(access.project_steps(goal), step_id)
        assignment = _assert_step_editor(goal, member, step)
        _check_step_version(step, data)
        if mirror and (assignment.task_id if assignment else None) != linked_task_id:
            raise Conflict()
        _validate_step(goal, fields, step)
        for key, value in fields.items():
            setattr(step, key, value)
        step.version += 1
        step.save()
        if task:
            update_fields = ["updated_at"]
            if {"start_date", "due_date"} & fields.keys():
                task.due_date = step.due_date
                update_fields.append("due_date")
            if authored_title:
                task.title = truncate_placeholder_safe(authored_title.text, Task._meta.get_field("title").max_length)
                task.pii_receipts = {**(task.pii_receipts or {}), "title": authored_title.receipt}
                update_fields.extend(["title", "pii_receipts"])
            task.save(update_fields=update_fields)
        _refresh_milestones(goal, tenant, None)
        return step


@transaction.atomic
def delete_step(tenant, mission_id, step_id):
    goal, member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    _assert_step_editor(goal, member, step)
    step.delete()
    _refresh_milestones(goal, tenant, None)


@transaction.atomic
def ask(tenant, user, mission_id, step_id, membership_ids):
    goal, _ = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    if not isinstance(membership_ids, list) or not membership_ids:
        raise ValidationError("membership_ids must be a nonempty list.")
    ids = {_uuid(value) for value in membership_ids}
    members = list(access.mission_memberships().filter(shared_goal=goal, status__in=["active", "invited"], id__in=ids))
    if len(members) != len(ids):
        raise NotFound("No such project member.")
    from .project_flags import projects_v2_enabled

    for member in members:
        if member.status == "invited":
            access.assert_project_invitee(member.tenant_id, goal)
        if member.tenant_id != tenant.id and not projects_v2_enabled(member.tenant):
            # Their app can't show or answer an ask yet — refuse rather than dead-end.
            raise ValidationError("They need the newest version of the app before you can ask them.")
    newly_asked = []
    for member in members:
        assignment, created = access.project_assignments(goal).get_or_create(
            step=step, membership=member, defaults={"asked_by": tenant}
        )
        if not created and assignment.status in {"accepted", "asked"}:
            continue
        assignment.status = "asked"
        assignment.asked_by = tenant
        assignment.asked_at = timezone.now()
        assignment.counter_start = assignment.counter_due = assignment.responded_at = None
        assignment.released_at = assignment.suggested_membership = None
        assignment.note = ""
        assignment.save()
        newly_asked.append(member.id)
        services._append_update(
            goal, tenant, user, "step_assigned", payload={"step_id": str(step.id), "membership_id": str(member.id)}
        )
    if newly_asked:
        from .project_notifications import notify_step_ask

        notify_step_ask(goal, step, newly_asked, tenant)


def respond(tenant, user, mission_id, step_id, data):
    goal, member = services._assert_mission_member(tenant, mission_id)
    answer = data.get("answer")
    if answer not in {"yes", "dates", "smaller", "no", "other"}:
        raise ValidationError("answer must be yes, dates, smaller, no, or other.")
    note = clean_text(data.get("note"), limit=200 if answer == "smaller" else 500)
    start, due = _date(data.get("start")), _date(data.get("due"))
    if start and due and start > due:
        raise ValidationError("Start must be on or before due date.")
    if answer == "dates" and not (start or due):
        raise ValidationError("Suggest a start or due date.")
    if answer == "smaller" and not note:
        raise ValidationError("Describe the smaller part.")
    before = _row(access.project_steps(goal), step_id)
    existing = access.project_assignments(goal).filter(step=before, membership=member).first()
    if existing is None or existing.status not in {"asked", "countered", "accepted"}:
        raise PermissionDenied("Only the asked member can respond.")
    if existing.status == "accepted" and answer != "yes":
        raise ValidationError("This step is already accepted.")
    prepared = (
        services._prepare_member_task(tenant, before.title, before.description)
        if answer == "yes" and not existing.task_id
        else None
    )
    with transaction.atomic():
        goal, member = access.lock_project(tenant, mission_id)
        step = _row(access.project_steps(goal), step_id)
        if step.version != before.version:
            raise Conflict()
        assignment = access.project_assignments(goal).filter(step=step, membership=member).first()
        if assignment is None or assignment.status not in {"asked", "countered", "accepted"}:
            raise PermissionDenied("Only the asked member can respond.")
        if assignment.status == "accepted":
            if answer == "yes":
                return assignment
            raise ValidationError("This step is already accepted.")
        assignment.status = {
            "yes": "accepted",
            "no": "declined",
            "other": "declined",
            "dates": "countered",
            "smaller": "countered",
        }[answer]
        assignment.counter_start, assignment.counter_due = (start, due) if answer == "dates" else (None, None)
        assignment.note = note
        assignment.responded_at = timezone.now()
        # "Not me — maybe them?" only points; the asker decides whether to ask them.
        assignment.suggested_membership = None
        if answer == "other" and data.get("suggest_membership_id"):
            suggested = (
                access.mission_memberships()
                .filter(shared_goal=goal, status__in=["active", "invited"], id=_uuid(data["suggest_membership_id"]))
                .exclude(id=member.id)
                .first()
            )
            if suggested is None:
                raise NotFound("No such project member.")
            assignment.suggested_membership = suggested
        if answer == "yes" and not assignment.task_id:
            if prepared is None:
                raise Conflict()
            assignment.task = services._mint_member_task(
                tenant, goal, step.title, step.description, step.due_date, step=step, prepared=prepared
            )
        assignment.save()
        services._append_update(
            goal, tenant, user, "step_answered", payload={"step_id": str(step.id), "answer": answer}
        )
        if assignment.asked_by_id and assignment.asked_by_id != tenant.id:
            from .project_notifications import notify_step_answer

            notify_step_answer(goal, step, answer, tenant, assignment.asked_by_id)
        return assignment


def add_owned_step(tenant, user, mission_id, *, title, description="", due_date=None, prepared=None):
    services._assert_mission_member(tenant, mission_id)
    title, description = clean_text(title, limit=120, required=True), clean_text(description)
    if prepared is None:
        prepared = services._prepare_member_task(tenant, title, description)
    with transaction.atomic():
        goal, member = access.lock_project(tenant, mission_id)
        step = create_step(tenant, user, mission_id, {"title": title, "description": description, "due_date": due_date})
        task = services._mint_member_task(tenant, goal, title, description, due_date, step=step, prepared=prepared)
        access.project_assignments(goal).create(
            step=step, membership=member, status="accepted", task=task, asked_by=tenant, responded_at=timezone.now()
        )
        services._append_update(
            goal,
            tenant,
            user,
            "task_added",
            text=title,
            payload={"title": title, "task_id": str(task.id), "step_id": str(step.id)},
        )
        return step, task


def _refresh_milestones(goal, tenant, user):
    for milestone in access.milestone_completion_rows(goal):
        reached = milestone.step_count > 0 and milestone.open_count == 0
        if reached and not milestone.reached_at:
            milestone.reached_at = timezone.now()
            milestone.save(update_fields=["reached_at"])
            services._append_update(
                goal, tenant, user, "milestone_reached", payload={"milestone_id": str(milestone.id)}
            )
            from .project_notifications import notify_milestone_reached

            notify_milestone_reached(goal, milestone.title, tenant)
        elif not reached and milestone.reached_at:
            milestone.reached_at = None
            milestone.save(update_fields=["reached_at"])


_FINISH_FIELDS = [
    "status",
    "completed_at",
    "completed_by",
    "done_note",
    "done_link",
    "reviewed_at",
    "reviewed_by",
    "version",
]


def _finish(goal, step, tenant, user, *, reopen=False, note="", link=""):
    """Tick a step off (or reopen it). A step that needs a second look parks in
    ``in_review`` — not done for milestones or the steps waiting on it — until a
    member who doesn't own it confirms (:func:`confirm`)."""
    if reopen:
        if step.status == "open":
            return
        step.status, step.completed_at, step.completed_by = "open", None, None
        step.done_note = step.done_link = ""
        step.reviewed_at = step.reviewed_by = None
        step.version += 1
        step.save(update_fields=_FINISH_FIELDS)
        _refresh_milestones(goal, tenant, user)
        return
    if step.status in {"done", "in_review"}:
        return
    step.completed_at, step.completed_by = timezone.now(), tenant
    step.done_note, step.done_link = note, link
    # Park only if someone is able to give the look; otherwise it would wait forever.
    step.status = "in_review" if step.needs_review and _can_anyone_look(goal, step, tenant) else "done"
    step.version += 1
    step.save(update_fields=_FINISH_FIELDS)
    if step.status == "in_review":
        services._append_update(goal, tenant, user, "step_submitted", payload={"step_id": str(step.id)})
        from .project_notifications import notify_step_needs_look

        notify_step_needs_look(goal, step, tenant)
        return
    _announce_done(goal, step, tenant, user)


def _can_anyone_look(goal, step, completer):
    """An active member who neither ticked the step nor holds it can confirm —
    and whose app can show it (someone outside the rollout can't look)."""
    from .project_flags import projects_v2_enabled

    holders = (
        access.project_assignments(goal).filter(step=step, status="accepted").values_list("membership_id", flat=True)
    )
    others = (
        access.mission_memberships()
        .filter(shared_goal=goal, status="active")
        .exclude(tenant=completer)
        .exclude(id__in=list(holders))
        .select_related("tenant")
    )
    return any(projects_v2_enabled(m.tenant) for m in others)


def _announce_done(goal, step, tenant, user, *, credit=None):
    """``credit``: who did the step when someone else (the confirmer) is the actor.
    Only the ``step_done`` row is theirs; the actor stays the one skipped by pushes."""
    services._append_update(
        goal,
        credit or tenant,
        None if credit else user,
        "step_done",
        text=step.title,
        payload={"step_id": str(step.id)},
    )
    for edge in access.project_dependencies(goal).filter(blocker=step):
        if (
            not access.project_dependencies(goal)
            .filter(blocked_id=edge.blocked_id)
            .exclude(blocker__status__in=["done", "skipped"])
            .exists()
        ):
            services._append_update(goal, tenant, user, "step_unblocked", payload={"step_id": str(edge.blocked_id)})
            from .project_notifications import notify_step_unblocked

            notify_step_unblocked(goal, edge.blocked_id, step.title, tenant)
    _refresh_milestones(goal, tenant, user)


def _done_proof(data):
    """The optional note and link an owner adds when ticking a step off."""
    data = data or {}
    note = clean_text(data.get("note"), limit=500)
    link = clean_text(data.get("link"), limit=500)
    if link and not link.lower().startswith(("https://", "http://")):
        raise ValidationError("A link must start with https:// or http://.")
    return note, link


@transaction.atomic
def complete(tenant, user, mission_id, step_id, *, reopen=False, data=None):
    from apps.journal.models import Task

    note, link = ("", "") if reopen else _done_proof(data)
    # Match journal writes' lock order: private Task first, project second.
    # Otherwise Task.save's synchronous receiver (Task -> project) can deadlock
    # against a project completion holding the project while waiting on Task.
    goal, member = services._assert_mission_member(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    assignment = access.project_assignments(goal).filter(step=step, membership=member, status="accepted").first()
    if assignment is None:
        if reopen and member.role == "owner":
            return _unpark(tenant, user, mission_id, step_id)
        raise PermissionDenied("Only an accepted owner can complete or reopen this step.")
    linked_task_id = assignment.task_id
    task = Task.objects.select_for_update().filter(id=linked_task_id, tenant=tenant).first() if linked_task_id else None
    goal, member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    assignment = access.project_assignments(goal).filter(step=step, membership=member, status="accepted").first()
    if assignment is None:
        raise PermissionDenied("Only an accepted owner can complete or reopen this step.")
    if assignment.task_id != linked_task_id:
        raise Conflict()
    _finish(goal, step, tenant, user, reopen=reopen, note=note, link=link)
    # Owner-only private mirror; never fetch another owner's task.
    if task and reopen and task.status == "done":
        task.status, task.completed_at = "open", None
        task.save(update_fields=["status", "completed_at", "updated_at"])
    elif task and not reopen and task.status != "done":
        task.complete()
    return step


def _unpark(tenant, user, mission_id, step_id):
    """A project owner reopens a step stuck waiting for a look after everyone who held
    it has left. While an active member still holds it, reopening is theirs. Only
    ``in_review`` — never finished work — and no private Task is touched."""
    goal, member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    held = access.project_assignments(goal).filter(step=step, status="accepted")
    if member.role != "owner" or step.status != "in_review" or held.filter(membership__status="active").exists():
        raise PermissionDenied("Only an accepted owner can complete or reopen this step.")
    # The people who held it are gone: their rows become "had this", so the step
    # shows as open again and its creator can edit it.
    held.update(status="released", released_at=timezone.now(), note="")
    _finish(goal, step, tenant, user, reopen=True)
    return step


# ── Showing the work: the second look ────────────────────────────────────────


@transaction.atomic
def set_second_look(tenant, mission_id, step_id, on):
    """Any member can ask for a second look on a step that isn't ticked off yet.
    Switching it off takes whoever switched it on, or a project owner — never the
    doer alone (unless they asked for it themselves)."""
    if type(on) is not bool:
        raise ValidationError("on must be true or false.")
    goal, member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    if step.status not in {"open", "in_progress"}:
        raise ValidationError("Change this before the step is ticked off.")
    if step.needs_review == on:
        return step
    if on:
        if access.mission_memberships().filter(shared_goal=goal, status="active").count() < 2:
            raise ValidationError("A second look needs a second person in the project.")
        step.needs_review, step.review_set_by = True, tenant
    else:
        holds = access.project_assignments(goal).filter(step=step, membership=member, status="accepted").exists()
        if step.review_set_by_id != tenant.id and (holds or member.role != "owner"):
            raise PermissionDenied("Whoever asked for the second look, or a project owner, can switch it off.")
        step.needs_review, step.review_set_by = False, None
    step.version += 1
    step.save(update_fields=["needs_review", "review_set_by", "version"])
    return step


@transaction.atomic
def confirm(tenant, user, mission_id, step_id):
    """The second look: a member who doesn't own the step says it's really done."""
    goal, member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    if step.status != "in_review":
        raise ValidationError("This step isn't waiting for a look.")
    if (
        step.completed_by_id == tenant.id
        or access.project_assignments(goal).filter(step=step, membership=member, status="accepted").exists()
    ):
        raise PermissionDenied("Someone who didn't do the step takes the second look.")
    step.status, step.reviewed_at, step.reviewed_by = "done", timezone.now(), tenant
    step.version += 1
    step.save(update_fields=["status", "reviewed_at", "reviewed_by", "version"])
    _announce_done(goal, step, tenant, user, credit=step.completed_by)
    from .project_notifications import notify_step_confirmed

    notify_step_confirmed(goal, step, tenant)
    return step


@transaction.atomic
def question(tenant, user, mission_id, step_id, data):
    """ "Is this really done?" — a nudge to the step's owners. It never reopens the
    step; the owner decides. One per member, per step, per day."""
    from datetime import timedelta

    goal, _member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    if step.status not in {"done", "in_review"}:
        raise ValidationError("You can ask about a step once it's ticked off.")
    note = clean_text((data or {}).get("note"), limit=200)
    if (
        access.mission_updates()
        .filter(
            shared_goal=goal,
            tenant=tenant,
            kind="step_questioned",
            payload__step_id=str(step.id),
            created_at__gte=timezone.now() - timedelta(days=1),
        )
        .exists()
    ):
        raise ValidationError("You already asked about this step today.")
    services._append_update(goal, tenant, user, "step_questioned", text=note, payload={"step_id": str(step.id)})
    from .project_notifications import notify_step_question

    notify_step_question(goal, step, tenant)
    return step


# ── Stepping back, leaving, looking after the project ────────────────────────


def _release(goal, member, *, picks=None):
    """Let go of ``member``'s steps that are still open. ``picks`` maps step id →
    hand-off note; None means every open step. A step they had ACCEPTED becomes
    ``released``. An ask they never answered is closed as declined — they never held
    it, so nothing "opens again". The private journal Task is left alone.
    Returns ``(released_steps, declined_rows)``."""
    rows = (
        access.project_assignments(goal)
        .filter(
            membership=member,
            status__in=["asked", "accepted", "countered"],
            step__status__in=["open", "in_progress"],
        )
        .select_related("step")
    )
    if picks is not None:
        rows = rows.filter(step_id__in=list(picks))
    released, declined = [], []
    for row in rows:
        held = row.status == "accepted"
        row.status = "released" if held else "declined"
        row.released_at = timezone.now() if held else None
        row.note = (picks or {}).get(row.step_id, "") if held else ""
        row.counter_start = row.counter_due = row.suggested_membership = row.kept_at = None
        if not held:
            row.responded_at = timezone.now()
        row.save()
        if held:
            released.append(row.step)
        else:
            declined.append(row)
    return released, declined


@transaction.atomic
def step_back(tenant, user, mission_id, data):
    """I stay in the project but let go of steps: ``{"all": true}`` or
    ``{"steps": [{"step_id", "note"?}]}``. Each goes back to "anyone", with my
    optional hand-off line. Nobody is told why."""
    goal, member = access.lock_project(tenant, mission_id)
    data = data or {}
    picks = None
    if data.get("all") is not True:
        items = data.get("steps")
        if not isinstance(items, list) or not items or len(items) > 60:
            raise ValidationError("Choose the steps to let go of.")
        picks = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError("Choose the steps to let go of.")
            picks[_uuid(item.get("step_id"))] = clean_text(item.get("note"), limit=500)
    released, declined = _release(goal, member, picks=picks)
    for step in released:
        services._append_update(
            goal, tenant, user, "step_released", payload={"step_id": str(step.id), "membership_id": str(member.id)}
        )
    from .project_notifications import notify_step_answer, notify_stepped_back

    if released:
        notify_stepped_back(goal, released, tenant)
    # Letting go of a step I was only asked about is a plain "no" to whoever asked.
    for row in declined:
        services._append_update(
            goal, tenant, user, "step_answered", payload={"step_id": str(row.step_id), "answer": "no"}
        )
        if row.asked_by_id and row.asked_by_id != tenant.id:
            notify_step_answer(goal, row.step, "no", tenant, row.asked_by_id)
    return released


@transaction.atomic
def keep_step(tenant, mission_id, step_id):
    """ "Still yours?" → yes. Quiets the overdue flag for a week."""
    goal, member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    assignment = access.project_assignments(goal).filter(step=step, membership=member, status="accepted").first()
    if assignment is None:
        raise PermissionDenied("Only the step's owner can say that.")
    assignment.kept_at = timezone.now()
    assignment.save(update_fields=["kept_at"])
    return step


@transaction.atomic
def set_owner_role(tenant, user, mission_id, data):
    """A project owner shares or passes on looking after the project: make another
    active member an owner, or step down themselves once someone else is one."""
    goal, member = access.lock_project(tenant, mission_id)
    if member.role != "owner":
        raise PermissionDenied("Only a project owner can change who looks after it.")
    role = (data or {}).get("role")
    if role not in {"owner", "member"}:
        raise ValidationError("role must be owner or member.")
    target = (
        access.mission_memberships()
        .filter(shared_goal=goal, status="active", id=_uuid((data or {}).get("membership_id")))
        .first()
    )
    if target is None:
        raise NotFound("No such project member.")
    if target.role == role:
        return target
    if role == "member":
        if target.id != member.id:
            raise PermissionDenied("You can only step down yourself.")
        if not (
            access.mission_memberships()
            .filter(shared_goal=goal, status="active", role="owner")
            .exclude(id=member.id)
            .exists()
        ):
            raise ValidationError("Make someone else an owner first.")
    target.role = role
    target.save(update_fields=["role"])
    services._append_update(
        goal, tenant, user, "owner_changed", payload={"membership_id": str(target.id), "role": role}
    )
    if role == "owner":
        from .project_notifications import notify_now_owner

        notify_now_owner(goal, target.id, tenant)
    return target


@transaction.atomic
def milestone_write(tenant, mission_id, data=None, *, milestone_id=None, delete=False):
    goal, member = access.lock_project(tenant, mission_id)
    if milestone_id:
        milestone = _row(access.project_milestones(goal), milestone_id)
        if member.role != "owner" and milestone.created_by_id != tenant.id:
            raise PermissionDenied("Ask the milestone's creator or a project owner to change it.")
        if delete:
            milestone.delete()
            return None
        fields = _fields(data, milestone=True)
        for key, value in fields.items():
            setattr(milestone, key, value)
        milestone.save()
        return milestone
    if access.project_milestones(goal).count() >= 8:
        raise ValidationError("A project can have at most 8 milestones.")
    return access.project_milestones(goal).create(
        shared_goal=goal, created_by=tenant, **_fields(data, milestone=True, creating=True)
    )


@transaction.atomic
def dependency_write(tenant, mission_id, data=None, *, dependency_id=None):
    goal, member = access.lock_project(tenant, mission_id)
    if dependency_id:
        edge = _row(access.project_dependencies(goal), dependency_id)
        _assert_step_editor(goal, member, _row(access.project_steps(goal), edge.blocked_id))
        edge.delete()
        return None
    blocker = _row(access.project_steps(goal), data.get("blocker_id"))
    blocked = _row(access.project_steps(goal), data.get("blocked_id"))
    edges = list(access.project_dependencies(goal))
    for edge in edges:
        if edge.blocker_id == blocker.id and edge.blocked_id == blocked.id:
            return edge
    if len(edges) >= 120:
        raise ValidationError("A project can have at most 120 dependencies.")
    children = {}
    for edge in edges:
        children.setdefault(edge.blocker_id, []).append(edge.blocked_id)
    stack, seen = [blocked.id], set()
    while stack:
        current = stack.pop()
        if current == blocker.id:
            raise ValidationError("Dependencies cannot form a cycle.")
        if current not in seen:
            seen.add(current)
            stack.extend(children.get(current, []))
    return access.project_dependencies(goal).create(blocker=blocker, blocked=blocked)


@transaction.atomic
def set_linked_goal(tenant, mission_id, goal_id):
    """Link this project to ONE of my own Horizons goals (or clear it with None).

    Private to me. My accepted step Tasks move under the goal (so they show in its
    Horizons checklist); unlinking moves back only the Tasks this link placed there.
    Queryset ``update()`` on my own Tasks: no Task.save signal re-enters the project.
    """
    from django.db.models import Q

    from apps.journal.models import Goal, Task

    goal, member = access.lock_project(tenant, mission_id)
    new_id = _uuid(goal_id) if goal_id else None
    if new_id and not Goal.objects.filter(id=new_id, tenant=tenant).exists():
        raise NotFound("No such goal.")
    old_id = member.linked_goal_id
    if old_id == new_id:
        return member
    task_ids = list(access.my_linked_step_tasks(member).values_list("task_id", flat=True))
    mine = Task.objects.filter(id__in=task_ids, tenant=tenant)
    if old_id:
        mine.filter(parent_goal_id=old_id).update(parent_goal_id=new_id)
    if new_id:
        mine.filter(Q(parent_goal__isnull=True)).update(parent_goal_id=new_id)
    member.linked_goal_id = new_id
    member.save(update_fields=["linked_goal_id"])
    return member
