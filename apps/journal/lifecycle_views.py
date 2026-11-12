"""User-facing (session-auth) write endpoints for the typed Task/Goal lifecycle.

Mirrors the agent-facing runtime endpoints in ``apps.integrations.runtime_views``
but authenticated as the logged-in user (``IsAuthenticated`` + the user's own
tenant), so the web UI can update typed rows directly instead of editing
synthesized markdown that the read layer would discard.

Status transitions use the model methods (``Task.complete`` etc.) so timestamps
(``completed_at``, ``achieved_at``) are set correctly — a plain ``status=done``
PATCH would not. See ``DocumentDetailView.patch``, which now rejects markdown
writes to typed tasks/goal docs and points here.

Serializer/model imports are LOCAL per ``feedback_local_reimport_pattern`` (the
lint-on-Edit hook reaps module-level imports that look unused at parse time).
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .document_views import _get_tenant

logger = logging.getLogger(__name__)

# Cap for the ``?q=`` name search path — keeps a Siri/Shortcuts disambiguation
# picker (EntityQuery) bounded. Only applied when ``q`` is present; the
# unfiltered list stays full so the web/iOS enumeration paths don't truncate.
_ENTITY_SEARCH_LIMIT = 20
_SEARCH_VARIANT_LIMIT = 20


def _owner_ctx(tenant, tasks=None):
    """Serializer context for owner-facing responses.

    Sets ``rehydrate=True`` so ``TaskSerializer`` / ``GoalSerializer`` swap
    ``[TYPE_N]`` PII placeholders in title/description back to real values for
    the logged-in owner. The agent/runtime serializer paths
    (``apps.integrations.runtime_views``) deliberately omit this flag so those
    fields stay in placeholder space for the model.

    Pass ``tasks`` to attach the resolved ``project`` read-model; runtime callers
    omit it, and ``TaskSerializer`` then omits the ``project`` key entirely.
    """
    context = {"tenant": tenant, "rehydrate": True}
    if tasks is not None:
        from .lifecycle_serializers import resolve_task_projects

        context["task_projects"] = resolve_task_projects(tenant, tasks)
    return context


def _author_owner_input(tenant, data, *, seam, model_label, receipts=None, include_defaults=False):
    """Route owner-authored lifecycle fields through the Layer-1 chokepoint."""
    from apps.pii.authoring import author_text

    out = data.dict() if hasattr(data, "dict") else dict(data)
    if include_defaults:
        out.setdefault("description", "")
    next_receipts = dict(receipts or {})
    for field in ("title", "description"):
        if field not in out or not isinstance(out[field], str):
            continue
        authored = author_text(
            tenant,
            out[field],
            seam=seam,
            writer="owner",
            field=field,
            model_label=model_label,
        )
        out[field] = authored.text
        next_receipts[field] = authored.receipt
    return out, next_receipts


def _search_variants(tenant, query: str) -> list[str]:
    """Return bounded original/name-placeholder query variants."""
    import re

    from apps.pii.entity_registry import inverted_names_multimap

    query_ci = query.casefold()
    matches: list[tuple[str, str]] = []
    seen_bindings: set[tuple[str, str]] = set()
    for bindings in inverted_names_multimap(getattr(tenant, "pii_entity_map", None) or {}).values():
        for value, placeholder in bindings:
            value = value.strip()
            binding = (value, placeholder)
            if len(value) >= 3 and value.casefold() in query_ci and binding not in seen_bindings:
                seen_bindings.add(binding)
                matches.append(binding)

    candidates = [query]
    if len(matches) <= 2:
        for value, placeholder in matches:
            for variant in tuple(candidates):
                candidates.append(
                    re.sub(
                        re.escape(value),
                        lambda _match, replacement=placeholder: replacement,
                        variant,
                        flags=re.IGNORECASE,
                    )
                )
    else:
        for value, placeholder in matches:
            candidates.append(
                re.sub(
                    re.escape(value),
                    lambda _match, replacement=placeholder: replacement,
                    query,
                    flags=re.IGNORECASE,
                )
            )

    variants: list[str] = []
    for candidate in candidates:
        if candidate in variants:
            continue
        if len(variants) >= _SEARCH_VARIANT_LIMIT:
            logger.warning(
                "pii_search_variants_capped tenant=%s matched_bindings=%d cap=%d",
                getattr(tenant, "pk", getattr(tenant, "id", "?")),
                len(matches),
                _SEARCH_VARIANT_LIMIT,
            )
            break
        variants.append(candidate)
    return variants


def _filter_title_variants(queryset, variants):
    from django.db.models import Q

    title_q = Q()
    for variant in variants:
        title_q |= Q(title__icontains=variant)
    return queryset.filter(title_q)


def _parse_iso_date(raw, field_name):
    """Parse a YYYY-MM-DD query param, or return None. Raise ValueError if malformed."""
    if not raw:
        return None
    from datetime import date

    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date (YYYY-MM-DD)") from exc


def _parse_completed_after(raw, tenant):
    """Inclusive ISO timestamp; dates/naive datetimes use the tenant timezone."""
    if not raw:
        return None
    from datetime import datetime

    from django.utils import timezone

    from apps.common.tenant_tz import tenant_tz

    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("completed_after must be an ISO date or datetime") from exc
    return timezone.make_aware(value, tenant_tz(tenant)) if timezone.is_naive(value) else value


def _parse_uuid(raw, field_name):
    """Return ``raw`` if it is a valid UUID string, else None. Raise ValueError if malformed."""
    if not raw:
        return None
    import uuid

    try:
        uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc
    return raw


class TaskDetailView(APIView):
    """GET/PATCH/DELETE /api/v1/journal/tasks/<uuid>/ — read, update, or delete a task."""

    permission_classes = [IsAuthenticated]

    def get(self, request, task_id):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(TaskSerializer(task, context=_owner_ctx(tenant, [task])).data)

    def patch(self, request, task_id):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        authored_data, receipts = _author_owner_input(
            tenant,
            request.data,
            seam="journal.owner.task.patch",
            model_label="journal.Task",
            receipts=task.pii_receipts,
        )
        serializer = TaskSerializer(
            task,
            data=authored_data,
            partial=True,
            context={"tenant": tenant},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(pii_receipts=receipts)
        return Response(TaskSerializer(task, context=_owner_ctx(tenant, [task])).data)

    def delete(self, request, task_id):
        from django.db import transaction

        from apps.friends import access
        from apps.friends import services as friends_services
        from apps.friends.access import backstop_service_context
        from apps.integrations.runtime_views import _task_descendant_ids, _task_pending_action_count

        from .models import Task

        tenant = _get_tenant(request.user)
        with transaction.atomic():
            task = Task.objects.select_for_update().filter(tenant=tenant, id=task_id).first()
            if task is None:
                return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

            # The ORM CASCADE ignores the tenant filter, so an unscoped walk is
            # the only honest check that this delete cannot reach a stranger's row.
            if _task_descendant_ids(task) != _task_descendant_ids(task, tenant=tenant):
                return Response({"error": "integrity_error"}, status=status.HTTP_409_CONFLICT)

            # ``parent_task`` is CASCADE, so deleting without re-parenting would
            # destroy the direct children too. Move them up one level first.
            subtasks_kept = Task.objects.filter(tenant=tenant, parent_task=task).update(parent_task=task.parent_task)
            deleted_id = str(task.id)
            pending_actions_deleted = _task_pending_action_count({task.id})
            step_moved_to = None
            # ``assignment_for_task`` reads the FORCE-RLS friends tables; widen the
            # service scope so the lookup and adoption see the same transaction
            # under the transaction-mode pooler, not just ``task.delete()``.
            with backstop_service_context():
                assignment = access.assignment_for_task(task)
                if assignment is not None:
                    twin = friends_services._adopt_open_todo(
                        tenant,
                        task.title,
                        related_ref=task.related_ref,
                        due_date=task.due_date,
                        parent_goal_id=task.parent_goal_id,
                        exclude_id=task.id,
                    )
                    if twin is not None:
                        assignment.task = twin
                        assignment.save(update_fields=["task"])
                        step_moved_to = str(twin.id)
                task.delete()

        return Response(
            {
                "status": "deleted",
                "id": deleted_id,
                "subtasks_kept": subtasks_kept,
                "pending_actions_deleted": pending_actions_deleted,
                "step_moved_to": step_moved_to,
            },
            status=status.HTTP_200_OK,
        )


class TaskCompleteView(APIView):
    """POST .../tasks/<uuid>/complete/ — status=done + completed_at=now."""

    permission_classes = [IsAuthenticated]

    def post(self, request, task_id):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        task.complete()
        return Response(TaskSerializer(task, context=_owner_ctx(tenant, [task])).data)


class TaskReopenView(APIView):
    """POST .../tasks/<uuid>/reopen/ — status=open, clear completed_at."""

    permission_classes = [IsAuthenticated]

    def post(self, request, task_id):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        task.status = Task.Status.OPEN
        task.completed_at = None
        task.save(update_fields=["status", "completed_at", "updated_at"])
        return Response(TaskSerializer(task, context=_owner_ctx(tenant, [task])).data)


class GoalDetailView(APIView):
    """GET/PATCH /api/v1/journal/goals/<uuid>/ — read or update a goal."""

    permission_classes = [IsAuthenticated]

    def get(self, request, goal_id):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(GoalSerializer(goal, context=_owner_ctx(tenant)).data)

    def patch(self, request, goal_id):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        authored_data, receipts = _author_owner_input(
            tenant,
            request.data,
            seam="journal.owner.goal.patch",
            model_label="journal.Goal",
            receipts=goal.pii_receipts,
        )
        serializer = GoalSerializer(
            goal,
            data=authored_data,
            partial=True,
            context={"tenant": tenant},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(pii_receipts=receipts)
        return Response(GoalSerializer(goal, context=_owner_ctx(tenant)).data)


class GoalAchieveView(APIView):
    """POST .../goals/<uuid>/achieve/ — status=achieved + achieved_at=now."""

    permission_classes = [IsAuthenticated]

    def post(self, request, goal_id):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        goal.mark_achieved()
        return Response(GoalSerializer(goal, context=_owner_ctx(tenant)).data)


class GoalAbandonView(APIView):
    """POST .../goals/<uuid>/abandon/ — status=abandoned."""

    permission_classes = [IsAuthenticated]

    def post(self, request, goal_id):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        goal.abandon()
        return Response(GoalSerializer(goal, context=_owner_ctx(tenant)).data)


class TaskListCreateView(APIView):
    """GET /api/v1/journal/tasks/ — list the tenant's tasks (filters: status,
    pillar, parent_goal_id, due_before, due_after, completed_after, q). POST — create a task.

    The detail/transition endpoints (PATCH, complete, reopen) already exist
    above; this adds the collection read + create the connected iOS client and
    web UI need to enumerate and add tasks without going through the agent.

    ``?q=`` does a case-insensitive title search (capped) for the Siri
    EntityQuery / Shortcuts disambiguation picker. ``completed_after`` is
    inclusive; dates mean tenant-local midnight and naive datetimes are local.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        qs = Task.objects.filter(tenant=tenant)
        params = request.query_params
        for field in ("status", "pillar"):
            value = params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        try:
            parent_goal_id = _parse_uuid(params.get("parent_goal_id"), "parent_goal_id")
            due_before = _parse_iso_date(params.get("due_before"), "due_before")
            due_after = _parse_iso_date(params.get("due_after"), "due_after")
            completed_after = _parse_completed_after(params.get("completed_after"), tenant)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if parent_goal_id:
            qs = qs.filter(parent_goal_id=parent_goal_id)
        if due_before:
            qs = qs.filter(due_date__lte=due_before)
        if due_after:
            qs = qs.filter(due_date__gte=due_after)
        if completed_after:
            qs = qs.filter(completed_at__gte=completed_after)
        qs = qs.order_by("-updated_at")
        q = (params.get("q") or "").strip()
        if q:
            qs = _filter_title_variants(qs, _search_variants(tenant, q))[:_ENTITY_SEARCH_LIMIT]
        return Response(TaskSerializer(qs, many=True, context=_owner_ctx(tenant, qs)).data)

    def post(self, request):
        from .lifecycle_serializers import TaskSerializer

        tenant = _get_tenant(request.user)
        authored_data, receipts = _author_owner_input(
            tenant,
            request.data,
            seam="journal.owner.task.create",
            model_label="journal.Task",
            include_defaults=True,
        )
        serializer = TaskSerializer(data=authored_data, context=_owner_ctx(tenant))
        serializer.is_valid(raise_exception=True)
        task = serializer.save(pii_receipts=receipts)
        return Response(
            TaskSerializer(task, context=_owner_ctx(tenant, [task])).data,
            status=status.HTTP_201_CREATED,
        )


class GoalListCreateView(APIView):
    """GET /api/v1/journal/goals/ — list the tenant's goals (filters: status,
    pillar, parent_goal_id, q). POST — create a goal.

    ``?q=`` does a case-insensitive title search (capped) for the Siri
    EntityQuery / Shortcuts disambiguation picker.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        qs = Goal.objects.filter(tenant=tenant)
        params = request.query_params
        for field in ("status", "pillar"):
            value = params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        try:
            parent_goal_id = _parse_uuid(params.get("parent_goal_id"), "parent_goal_id")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if parent_goal_id:
            qs = qs.filter(parent_goal_id=parent_goal_id)
        qs = qs.order_by("-updated_at")
        q = (params.get("q") or "").strip()
        if q:
            qs = _filter_title_variants(qs, _search_variants(tenant, q))[:_ENTITY_SEARCH_LIMIT]
        return Response(GoalSerializer(qs, many=True, context=_owner_ctx(tenant)).data)

    def post(self, request):
        from .lifecycle_serializers import GoalSerializer

        tenant = _get_tenant(request.user)
        authored_data, receipts = _author_owner_input(
            tenant,
            request.data,
            seam="journal.owner.goal.create",
            model_label="journal.Goal",
            include_defaults=True,
        )
        serializer = GoalSerializer(data=authored_data, context=_owner_ctx(tenant))
        serializer.is_valid(raise_exception=True)
        serializer.save(pii_receipts=receipts)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
