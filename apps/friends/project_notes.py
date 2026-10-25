"""Shared step notes. Moderate before locking; every write rechecks membership."""

import io
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import access, photos, services
from .project_hygiene import clean_text
from .project_services import _row

MAX_PHOTOS = 6


def check_step(tenant, mission_id, step_id):
    goal, member = services._assert_mission_member(tenant, mission_id)
    return goal, member, _row(access.project_steps(goal), step_id)


def prepare(text, uploads):
    text = clean_text(text, limit=2000)
    if len(uploads) > MAX_PHOTOS:
        raise ValidationError("Choose at most 6 photos per note.")
    if not text and not uploads:
        raise ValidationError("Write a note or add a photo.")
    prepared = []
    for upload in uploads:
        jpeg = photos.normalize(upload.read(photos.MAX_UPLOAD_BYTES + 1), note=True)
        photos.check_safe(jpeg)
        with Image.open(io.BytesIO(jpeg)) as image:
            prepared.append((jpeg, *image.size))
    return text, prepared


def representation(note, member, profile):
    return {
        "id": str(note.id),
        "author": {
            "membership_id": str(note.author_id),
            "display_name": profile.display_name if profile else "Neighbor",
            "hue": profile.avatar_hue if profile else 210,
            "photo_url": access.photo_url(profile),
        },
        "text": note.text,
        "photos": [
            {
                "id": str(p.id),
                "width": p.width,
                "height": p.height,
                "url": f"/api/v1/friends/missions/{note.shared_goal_id}/notes/photos/{p.id}/",
            }
            for p in note.photos.all()
        ],
        "created_at": note.created_at.isoformat(),
        "edited_at": note.edited_at.isoformat() if note.edited_at else None,
        "mine": note.author_id == member.id,
        "can_report": member.status == "active" and note.author_id != member.id,
    }


def list_notes(tenant, mission_id, step_id):
    goal, member, step = check_step(tenant, mission_id, step_id)
    rows = list(access.notes_for_display(goal, step=step, viewer=tenant))
    profiles = access.note_profiles(rows)
    return {"notes": [representation(n, member, profiles.get(n.author.tenant_id)) for n in rows]}


def _display_one(goal, member, note):
    row = access.notes_for_display(goal, step=note.step_id, viewer=member.tenant_id).get(id=note.id)
    return representation(row, member, access.note_profiles([row]).get(row.author.tenant_id))


@transaction.atomic
def create(tenant, user, mission_id, step_id, text, prepared):
    goal, member = access.lock_project(tenant, mission_id)
    step = _row(access.project_steps(goal), step_id)
    text = clean_text(text, limit=2000)
    if len(prepared) > MAX_PHOTOS or (not text and not prepared):
        raise ValidationError("Write a note or add up to 6 photos.")
    # Under the same project lock as insertion: concurrent posts cannot double-push.
    recent = (
        access.mission_updates()
        .filter(
            shared_goal=goal,
            tenant=tenant,
            kind="step_note",
            payload__step_id=str(step.id),
            created_at__gte=timezone.now() - timedelta(minutes=10),
        )
        .exists()
    )
    note = access.create_note(goal, step, member, text, prepared)
    services._append_update(
        goal,
        tenant,
        user,
        "step_note",
        text=text[:120],
        payload={"step_id": str(step.id), "note_id": str(note.id)},
    )
    if not recent:
        from .project_notifications import notify_step_note

        notify_step_note(goal, step, tenant)
    return _display_one(goal, member, note)


@transaction.atomic
def edit(tenant, mission_id, note_id, data):
    goal, member = access.lock_project(tenant, mission_id, allow_finished=True)
    note = _row(access.project_notes(goal), note_id)
    if note.author_id != member.id:
        access.assert_project_writable(goal)
        raise PermissionDenied("Only the author can edit this note.")
    if set(data) != {"text"}:
        raise ValidationError("Only text can be edited.")
    text = clean_text(data["text"], limit=2000)
    if not text and not access.project_note_photos(goal).filter(note=note).exists():
        raise ValidationError("Write a note or add a photo.")
    note.text = text
    note.edited_at = timezone.now()
    note.save(update_fields=["text", "edited_at"])
    access.mission_updates().filter(shared_goal=goal, kind="step_note", payload__note_id=str(note.id)).update(
        text=text[:120]
    )
    return _display_one(goal, member, note)


@transaction.atomic
def delete(tenant, mission_id, note_id):
    goal, member = access.lock_project(tenant, mission_id, allow_finished=True)
    note = _row(access.project_notes(goal), note_id)
    if note.author_id != member.id:
        access.assert_project_writable(goal)
    if note.author_id != member.id and member.role != "owner":
        raise PermissionDenied("Only the author or a project owner can delete this note.")
    note.deleted_at = timezone.now()
    note.save(update_fields=["deleted_at"])
    access.mission_updates().filter(shared_goal=goal, kind="step_note", payload__note_id=str(note.id)).update(text="")


def photo(tenant, mission_id, photo_id):
    goal, _ = services._assert_mission_member(tenant, mission_id)
    return _row(access.project_note_photos(goal, viewer=tenant), photo_id)
