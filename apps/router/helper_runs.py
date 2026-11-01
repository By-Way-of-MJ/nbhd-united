"""Durable app-thread helper closure, independent of the tenant container."""

import logging
import time
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.router.models import ChatThread, HelperRun
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

HELPER_TIMEOUT_SECONDS = 300
HELPER_GRACE_SECONDS = 180  # 120s announce budget plus a minute for delivery.
FAILURE_MESSAGE = "I couldn't finish that research — want me to try again here?"


class HelperAdmissionSerializer(serializers.Serializer):
    occurrence_key = serializers.RegexField(r"^helper:[0-9a-f]{48}$")
    thread_id = serializers.UUIDField()


class HelperContextSerializer(serializers.Serializer):
    thread_id = serializers.UUIDField()
    occurrence_key = serializers.RegexField(r"^helper:[0-9a-f]{48}$", required=False)
    child_session_key = serializers.RegexField(r"^agent:[^:]+:subagent:[0-9a-f-]{36}$", required=False)
    source_session_key = serializers.CharField(max_length=256, required=False)


class HelperAdmissionView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, tenant_id):
        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
                expected_tenant_id=str(tenant_id),
            )
        except InternalAuthError:
            return Response({"error": "internal_auth_failed"}, status=401)
        from apps.orchestrator.config_generator import subagents_enabled

        tenant = Tenant.objects.select_related("user").filter(id=tenant_id).first()
        if (
            not tenant
            or tenant.status != Tenant.Status.ACTIVE
            or not tenant.user.is_active
            or not subagents_enabled(tenant)
        ):
            return Response({"error": "helper_unavailable"}, status=403)
        serializer = HelperAdmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        thread = ChatThread.objects.filter(id=data["thread_id"], tenant=tenant).first()
        if thread is None:
            return Response({"error": "thread_not_found"}, status=404)
        row, _created = HelperRun.objects.get_or_create(
            tenant=tenant,
            occurrence_key=data["occurrence_key"],
            defaults={
                "thread": thread,
                "deadline": timezone.now() + timedelta(seconds=HELPER_TIMEOUT_SECONDS + HELPER_GRACE_SECONDS),
            },
        )
        # Retries never extend the deadline or reopen a closed obligation.
        if row.thread_id != thread.id or row.closed_at is not None or row.deadline <= timezone.now():
            return Response({"error": "helper_admission_closed"}, status=409)
        from apps.router.job_changes import bind_helper

        bind_helper(tenant, thread.id, row.occurrence_key)
        return Response({"status": "pending"})


class HelperReleaseView(APIView):
    """Release an admission when OpenClaw explicitly rejects the spawn."""

    authentication_classes = []
    permission_classes = []

    def post(self, request, tenant_id):
        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
                expected_tenant_id=str(tenant_id),
            )
        except InternalAuthError:
            return Response({"error": "internal_auth_failed"}, status=401)
        serializer = HelperAdmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            row = (
                HelperRun.objects.select_for_update()
                .filter(tenant_id=tenant_id, occurrence_key=data["occurrence_key"])
                .first()
            )
            if row and row.thread_id != data["thread_id"]:
                return Response({"error": "helper_thread_mismatch"}, status=400)
            if row and row.closed_at is None:
                row.delete()
        return Response({"status": "released"})


class HelperContextView(APIView):
    """Return checked side context for a runtime-produced parent announcement."""

    authentication_classes = []
    permission_classes = []

    def post(self, request, tenant_id):
        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
                expected_tenant_id=str(tenant_id),
            )
        except InternalAuthError:
            return Response({"error": "internal_auth_failed"}, status=401)
        serializer = HelperContextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        tenant = Tenant.objects.filter(pk=tenant_id).first()
        from apps.router.chat_gates import keep_chatting_enabled
        from apps.router.models import AppChatMessage
        from apps.router.side_lane import _exchanges

        if tenant is None or not keep_chatting_enabled(tenant):
            return Response({"status": "pending", "context": ""})
        thread = ChatThread.objects.filter(tenant=tenant, pk=data["thread_id"]).first()
        if thread is None:
            return Response({"error": "thread_not_found"}, status=404)
        if data.get("child_session_key"):
            from apps.router.job_changes import helper_entry

            with transaction.atomic():
                thread = ChatThread.objects.select_for_update().get(pk=thread.pk)
                entry = helper_entry(thread, data.get("occurrence_key"))
                if entry:
                    entry.setdefault("helper_sessions", {})[data["occurrence_key"]] = data["child_session_key"]
                    thread.save(update_fields=["side_jobs"])
            return Response({"status": "pending", "context": ""})
        open_keys = set(
            HelperRun.objects.filter(tenant=tenant, thread=thread)
            .exclude(closed_by=HelperRun.ClosedBy.RESULT)
            .values_list("occurrence_key", flat=True)
        )
        active_entries = [e for e in thread.side_jobs.values() if open_keys.intersection(e["helpers"])]
        source = data.get("source_session_key")
        entries = [
            e
            for e in active_entries
            if source
            and any(e.get("helper_sessions", {}).get(key) == source for key in open_keys.intersection(e["helpers"]))
        ]
        # Old in-flight admissions may lack the mapping. Only an unambiguous,
        # entirely unmapped job can use that backwards-compatible fallback.
        if not entries and len(active_entries) == 1 and not active_entries[0].get("helper_sessions"):
            entries = active_entries
        ids = [cid for e in entries for cid in e["side_ids"]]
        if not ids:
            return Response({"status": "pending", "context": ""})
        exchanges = _exchanges(
            tenant,
            AppChatMessage.objects.filter(tenant=tenant, thread=thread, client_msg_id__in=ids).order_by("created_at"),
            handoff=True,
        )
        return Response(
            {
                "status": "pending",
                "context": (
                    "[earlier-from-you: these messages arrived while the requested helper worked. "
                    "Follow the person's latest change or cancellation. Never re-send a cancelled result. "
                    "Do not repeat the side answer or take outward or saving actions they did not request.]\n"
                    + exchanges
                ),
            }
        )


class _JobChangePending(Exception):
    pass


def deliver_helper(*, tenant, occurrence_key, message_text, thread_id=None, expired_only=False, **content):
    """Wait outside transactions for an admitted side decision before publishing."""
    deadline = time.monotonic() + 60
    while True:
        try:
            return _deliver_helper_once(
                tenant=tenant,
                occurrence_key=occurrence_key,
                message_text=message_text,
                thread_id=thread_id,
                expired_only=expired_only,
                **content,
            )
        except _JobChangePending:
            if time.monotonic() >= deadline:
                return Response({"error": "job_change_pending"}, status=503)
            # No row lock or transaction spans this wait. A lost HTTP response
            # remains safe: publication and closure still commit together.
            time.sleep(0.1)


def _deliver_helper_once(*, tenant, occurrence_key, message_text, thread_id=None, expired_only=False, **content):
    """Commit the app-feed row and closure together; rollback is safe to retry.

    The runtime only admits app-thread helpers. External transports have no
    atomic send/ack primitive, so closure belongs in that original app thread.
    APNs is a post-commit hint; the durable feed row is the delivery.
    """
    from apps.router.proactive_context import record_proactive_outbound

    # The ended hook and watchdog both send this fixed notice.
    is_failure = message_text == FAILURE_MESSAGE
    from apps.router.chat_gates import keep_chatting_enabled

    keep_on = keep_chatting_enabled(tenant)
    target_thread_id = thread_id
    if keep_on and not target_thread_id:
        target_thread_id = (
            HelperRun.objects.filter(tenant=tenant, occurrence_key=occurrence_key)
            .values_list("thread_id", flat=True)
            .first()
        )
    with transaction.atomic():
        thread = (
            ChatThread.objects.select_for_update().filter(tenant=tenant, pk=target_thread_id).first()
            if keep_on and target_thread_id
            else None
        )
        row = HelperRun.objects.select_for_update().filter(tenant=tenant, occurrence_key=occurrence_key).first()
        if row is None:
            return Response({"error": "helper_not_found"}, status=404)
        if thread_id and str(row.thread_id) != str(thread_id):
            return Response({"error": "helper_thread_mismatch"}, status=400)
        if row.closed_at is not None:
            return Response({"status": "duplicate", "channel": "app"})
        if expired_only and row.deadline > timezone.now():
            return Response({"status": "pending"})
        if tenant.status != Tenant.Status.ACTIVE or not tenant.user.is_active:
            return Response({"status": "blocked"})
        from apps.router.job_changes import helper_entry

        if keep_on:
            thread = thread or ChatThread.objects.select_for_update().get(pk=row.thread_id)
            entry = helper_entry(thread, occurrence_key)
            if entry and entry.get("checking_until", 0) > timezone.now().timestamp():
                raise _JobChangePending
            if entry and entry["kind"] == "cancel":
                message_text = "Cancelled — I won't send that result."
                content = {}
                is_failure = False
        outbound = record_proactive_outbound(
            tenant=tenant,
            channel="eval" if tenant.is_eval_sink else "app",
            channel_user_id=str(tenant.user_id),
            message_text=message_text,
            job_name="_subagent_result",
            thread_id=row.thread_id,
            defer_push=True,
            **content,
        )
        if outbound is None:
            # Also roll back any partial work performed by the shared writer.
            transaction.set_rollback(True)
            return Response({"error": "app_delivery_not_recorded"}, status=503)
        row.closed_at = timezone.now()
        row.closed_by = HelperRun.ClosedBy.FAILURE if is_failure else HelperRun.ClosedBy.RESULT
        row.save(update_fields=["closed_at", "closed_by"])
    return Response({"status": "sent", "channel": "app", "delivered_to": ["app_feed"]})


def close_expired_helpers():
    """Minute sweep. Failed deliveries remain pending for the next sweep."""
    pending = (
        HelperRun.objects.filter(closed_at=None, deadline__lte=timezone.now())
        .select_related("tenant__user")
        .defer("tenant__cron_jobs_snapshot", "tenant__openclaw_migration")
    )
    sent = 0
    for row in pending.iterator(chunk_size=100):
        try:
            response = deliver_helper(
                tenant=row.tenant,
                occurrence_key=row.occurrence_key,
                message_text=FAILURE_MESSAGE,
                expired_only=True,
            )
            sent += response.data.get("status") == "sent"
        except Exception:
            logger.exception("helper_watchdog_delivery_failed helper=%s", row.pk)
    return {"sent": sent}
