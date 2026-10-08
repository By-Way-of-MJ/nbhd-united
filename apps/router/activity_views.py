"""The "since you were last here" feed: what the assistant changed while the owner was away.

``GET /api/v1/activity/since/?since=<ISO8601>`` lists the items behind the
``RuntimeWriteEvent`` rows newer than ``since``: newest first, one entry per
item, calendar events grouped into one entry, at most ``MAX_ITEMS``. Events
hold ids only; every title is read from the live row through the owner-read
path (``rehydrate_for_tenant``, as the owner serializers do), and an item whose
row is gone is dropped.

``POST /api/v1/activity/since/events`` counts shown/tap/dismiss through the
content-free tool telemetry — no ids, no titles.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.authentication import JWTAuthenticationWithRLS

MAX_ITEMS = 12
# Bounds the per-request scan; a tenant writing more than this since the last
# visit still gets its newest items.
MAX_EVENTS_SCANNED = 500
RUNTIME_WRITE_EVENT_RETENTION = timedelta(days=30)
COUNTER_EVENTS = frozenset({"shown", "tap", "dismiss"})


def _owner_tenant(request):
    return getattr(request.user, "tenant", None)


def _uuids(values) -> list[uuid.UUID]:
    out = []
    for value in values:
        try:
            out.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return out


def _calendar_title(count: int) -> str:
    return "1 event added" if count == 1 else f"{count} events added"


def _resolve(tenant, entries: list[dict]) -> dict[tuple[str, str], dict]:
    """Look up the live rows behind ``entries``: one query per kind present."""
    from apps.datebook.models import DeviceCommand
    from apps.fuel.models import Workout, WorkoutPlan
    from apps.journal.models import Document, Goal
    from apps.pii.redactor import rehydrate_for_tenant

    def ids(kind: str, key: str) -> list[uuid.UUID]:
        return _uuids(e["ref"].get(key) for e in entries if e["kind"] == kind and key in e["ref"])

    found: dict[tuple[str, str], dict] = {}
    doc_ids = ids("journal_doc", "document_id")
    if doc_ids:
        for row in Document.objects.filter(tenant=tenant, id__in=doc_ids).values("id", "kind", "slug", "title"):
            found[("document_id", str(row["id"]))] = {
                "title": rehydrate_for_tenant(tenant, row["title"]),
                "ref": {"document_id": str(row["id"]), "doc_kind": row["kind"], "slug": row["slug"]},
            }
    workout_ids = ids("fuel", "workout_id")
    if workout_ids:
        for row in Workout.objects.filter(tenant=tenant, id__in=workout_ids).values("id", "activity", "date"):
            found[("workout_id", str(row["id"]))] = {
                "title": rehydrate_for_tenant(tenant, row["activity"]),
                "ref": {"workout_id": str(row["id"]), "date": str(row["date"])},
            }
    plan_ids = ids("fuel", "plan_id")
    if plan_ids:
        for row in WorkoutPlan.objects.filter(tenant=tenant, id__in=plan_ids).values("id", "name"):
            found[("plan_id", str(row["id"]))] = {
                "title": rehydrate_for_tenant(tenant, row["name"]),
                "ref": {"plan_id": str(row["id"])},
            }
    goal_ids = ids("horizons_goal", "goal_id")
    if goal_ids:
        for row in Goal.objects.filter(tenant=tenant, id__in=goal_ids).values("id", "title"):
            found[("goal_id", str(row["id"]))] = {
                "title": rehydrate_for_tenant(tenant, row["title"]),
                "ref": {"goal_id": str(row["id"])},
            }
    command_ids = ids("calendar", "command_id")
    if command_ids:
        # Only events the device actually created count; a pending, denied or
        # failed request never became a calendar event.
        for row in DeviceCommand.objects.filter(
            tenant=tenant,
            id__in=command_ids,
            command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
            state=DeviceCommand.State.EXECUTED,
        ).values("id", "item_count"):
            found[("command_id", str(row["id"]))] = {"count": max(1, row["item_count"] or 1)}
    return found


def since_items(tenant, since) -> list[dict]:
    from apps.router.models import RuntimeWriteEvent

    events = (
        RuntimeWriteEvent.objects.filter(tenant=tenant, created_at__gt=since)
        .order_by("-created_at", "-id")
        .values("kind", "ref", "verb", "created_at")[:MAX_EVENTS_SCANNED]
    )
    # Newest first, so the first event seen per item carries its latest verb.
    latest: dict[tuple[str, str], dict] = {}
    for event in events:
        key = (event["kind"], json.dumps(event["ref"], sort_keys=True))
        if key not in latest:
            latest[key] = event
    entries = list(latest.values())
    found = _resolve(tenant, entries)

    items: list[dict] = []
    calendar: dict | None = None
    for entry in entries:
        ref_key, ref_id = next(iter(entry["ref"].items()))
        live = found.get((ref_key, str(ref_id)))
        if live is None:
            continue
        if entry["kind"] == "calendar":
            if calendar is None:
                calendar = {"kind": "calendar", "verb": "created", "count": 0, "at": entry["created_at"], "ref": {}}
                items.append(calendar)
            calendar["count"] += live["count"]
            continue
        items.append(
            {
                "kind": entry["kind"],
                "verb": entry["verb"],
                "count": 1,
                "at": entry["created_at"],
                "title": live["title"],
                "ref": live["ref"],
            }
        )
    if calendar is not None:
        calendar["title"] = _calendar_title(calendar["count"])
    items = items[:MAX_ITEMS]
    for item in items:
        item["at"] = item["at"].isoformat()
    return items


class ActivitySinceView(APIView):
    """GET: the owner's "since you were last here" items."""

    authentication_classes = [JWTAuthenticationWithRLS]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _owner_tenant(request)
        if tenant is None:
            return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)
        raw = request.query_params.get("since", "")
        since = parse_datetime(raw) if raw else None
        if since is None:
            return Response({"error": "invalid_since"}, status=status.HTTP_400_BAD_REQUEST)
        if timezone.is_naive(since):
            since = since.replace(tzinfo=UTC)
        since = max(since, timezone.now() - RUNTIME_WRITE_EVENT_RETENTION)
        return Response({"items": since_items(tenant, since)}, status=status.HTTP_200_OK)


class ActivitySinceEventView(APIView):
    """POST: count one shown/tap/dismiss. Content-free by construction."""

    authentication_classes = [JWTAuthenticationWithRLS]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.platform_logs.telemetry import emit_tool_event
        from apps.router.models import RuntimeWriteEvent

        if _owner_tenant(request) is None:
            return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)
        data = request.data if isinstance(request.data, dict) else {}
        event = data.get("event")
        kind = data.get("kind")
        if event not in COUNTER_EVENTS or (kind is not None and kind not in RuntimeWriteEvent.Kind.values):
            return Response({"error": "invalid_event"}, status=status.HTTP_400_BAD_REQUEST)
        emit_tool_event(
            namespace="activity_since",
            tool_name="activity-since",
            outcome="accepted",
            reason_code=event,
            detail={"kind": kind} if kind else None,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
