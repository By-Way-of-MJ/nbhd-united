#!/usr/bin/env python3
"""Invent a local synthetic life using the real models and journal services."""

import json
import os
from datetime import datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo


def seed():
    from apps.orchestrator.envelope_registry import suppress_refresh

    # Initial bulk data is rendered once, explicitly below. The friend has no
    # runtime/share and must not enqueue an envelope render for another tenant.
    with suppress_refresh():
        return _seed()


def _seed():
    from django.utils import timezone

    from apps.datebook.models import DatebookGateway, MirrorEvent
    from apps.friends.models import Friendship, SharedGoal, SharedGoalMembership, SharedGoalStep
    from apps.insights.pillars import Pillar
    from apps.journal.document_authoring import get_or_create_authored_document
    from apps.journal.models import Goal, Task
    from apps.journal.services import seed_default_documents_for_tenant
    from apps.orchestrator.config_generator import resolve_tenant_models
    from apps.orchestrator.services import refresh_system_cron_rows_from_seed, update_tenant_config
    from apps.tenants.models import Tenant, User

    if os.environ.get("NBHD_STAGE") != "1" or os.environ.get("AZURE_MOCK") != "true":
        raise RuntimeError("Synthetic staging environment required")
    if Tenant.objects.exists():
        raise RuntimeError("Seed requires an empty stage database")
    today = timezone.now().astimezone(ZoneInfo("Asia/Tokyo")).date()
    user = User.objects.create_user(
        username="mika-stage",
        email="mika@example.invalid",
        display_name="Mika Tanaka",
        timezone="Asia/Tokyo",
        terms_version="synthetic-stage",
        terms_accepted_at=timezone.now(),
        location_city="Tokyo",
        preferences={"occupation": "Designer at a small design studio", "agent_persona": "neighbor"},
    )
    user.full_clean()
    tenant = Tenant.objects.create(
        id=os.environ["NBHD_TENANT_ID"],
        user=user,
        status=Tenant.Status.ACTIVE,
        model_tier=Tenant.ModelTier.STARTER,
        is_synthetic=True,
        is_eval_sink=False,
        is_trial=True,
        trial_ends_at=timezone.now() + timedelta(days=7),
        container_id="nbhd-stage-assistant",
        container_fqdn="127.0.0.1:8080",
        container_image_tag=os.environ["STAGE_IMAGE_TAG"],
        internal_api_key=os.environ["NBHD_INTERNAL_API_KEY"],
        datebook_enabled=True,
        datebook_manifest_ok=True,
        datebook_events_consent_at=timezone.now(),
        neighborhood_enabled=True,
        friends_enabled=True,
        postgres_cron_canonical=True,
    )
    seed_default_documents_for_tenant(tenant=tenant)
    gateway = DatebookGateway.objects.create(
        tenant=tenant,
        installation_id="synthetic-stage-ios",
        current_generation=1,
        events_authorization="full_access",
        events_full_snapshot_required=False,
        events_last_complete_sync_at=timezone.now(),
        events_window_start=datetime.combine(today, time(), ZoneInfo("Asia/Tokyo")),
        events_window_end=datetime.combine(today + timedelta(days=6), time(), ZoneInfo("Asia/Tokyo")),
    )
    events = []
    for index, (day, hour, title) in enumerate(
        [
            (0, 15, "Studio move planning"),
            (1, 10, "Dentist visit"),
            (1, 14, "Brand sketches review"),
            (2, 11, "Visa renewal appointment"),
            (2, 19, "Dinner with friend Hana"),
            (3, 9, "Easy riverside run"),
            (4, 16, "Studio furniture measurements"),
        ]
    ):
        start = datetime.combine(today + timedelta(days=day), time(hour), ZoneInfo("Asia/Tokyo"))
        MirrorEvent.objects.create(
            tenant=tenant,
            source_key=f"stage-event-{index}",
            source_title="Synthetic iOS calendar",
            calendar_title="Mika's calendar",
            authorization_status="full_access",
            time_kind="zoned",
            zoned_start_at=start,
            zoned_end_at=start + timedelta(hours=1),
            tz_id="Asia/Tokyo",
            title=title,
            content_hash=sha256(f"{start.isoformat()}:{title}".encode()).hexdigest(),
            active=True,
            first_seen_generation=gateway.current_generation,
            last_seen_generation=gateway.current_generation,
        )
        events.append({"date": start.date().isoformat(), "title": title})
    goal = Goal.objects.create(
        tenant=tenant,
        title="Run a 10K",
        description="Build up gently from 4 km to finish a local 10K comfortably.",
        target_date=today + timedelta(days=60),
        pillar=Pillar.FUEL,
    )
    goal.full_clean()
    for title, due in [
        ("Send the studio mover quotes", -2),
        ("Sort receipts for the tax bill", -1),
        ("Pack visa renewal documents", 1),
        ("Book a weekend easy run", 2),
        ("Choose a new studio shelf", 4),
    ]:
        Task.objects.create(
            tenant=tenant,
            title=title,
            due_date=today + timedelta(days=due),
            parent_goal=goal if "run" in title else None,
        )
    for offset, note in [
        (-2, "The studio feels cramped. Hana and I measured some desks for our Studio move project."),
        (
            -1,
            "I work as a designer at a small design studio. The upcoming tax bill is worrying me; I need to sort the receipts. My 4 km run felt good.",
        ),
        (
            0,
            "A quiet start. I want to keep evenings light before the dentist and visa appointment. I already bought packing tape, so no reminder needed.",
        ),
    ]:
        day = (today + timedelta(days=offset)).isoformat()
        get_or_create_authored_document(
            tenant,
            kind="daily",
            slug=day,
            title=day,
            markdown_factory=lambda text=note, date=day: f"# {date}\n\n## Log\n\n### 08:30 — Mika Tanaka\n{text}\n",
            seam="stage.synthetic.daily_note",
        )
    friend_user = User.objects.create_user(
        username="hana-stage",
        email="hana@example.invalid",
        display_name="Hana Mori",
        timezone="Asia/Tokyo",
        terms_version="synthetic-stage",
        terms_accepted_at=timezone.now(),
    )
    friend_user.full_clean()
    # The friend has no runtime or credentials, and is explicitly synthetic too.
    friend = Tenant.objects.create(user=friend_user, is_synthetic=True, status="pending")
    friendship = Friendship.objects.create(
        requester=tenant, addressee=friend, requested_by=user, status="accepted", responded_at=timezone.now()
    )
    project = SharedGoal.objects.create(
        title="Studio move",
        description="Move our small design studio into a brighter room.",
        friendship=friendship,
        created_by=tenant,
        target_date=today + timedelta(days=21),
    )
    for member in (tenant, friend):
        SharedGoalMembership.objects.create(
            shared_goal=project, tenant=member, user=member.user, role="owner" if member == tenant else "member"
        )
    for order, title in enumerate(
        ["Collect mover quotes", "Measure desks and shelves", "Pack the sample library", "Set up the new studio"]
    ):
        SharedGoalStep.objects.create(
            shared_goal=project, title=title, order=order, created_by=tenant, due_date=today + timedelta(days=order + 2)
        )
    # seed_cron_jobs intentionally no-ops under AZURE_MOCK. This production
    # refresh service creates the same real canonical rows even in mock mode.
    refresh_system_cron_rows_from_seed(tenant)
    update_tenant_config(str(tenant.id), strict=True, refresh_crons=False)
    return {
        "tenant_id": str(tenant.id),
        "person": user.display_name,
        "today": today.isoformat(),
        "events": events,
        "tasks": 5,
        "overdue_tasks": 2,
        "notes": 3,
        "project_steps": 4,
        "models": resolve_tenant_models(tenant),
    }


if __name__ == "__main__":
    if os.environ.get("NBHD_STAGE") == "1":
        import django

        django.setup()
        print(json.dumps(seed(), default=str))
    else:
        from stack import runtime

        print(runtime("seed"), end="")
