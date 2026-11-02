"""Flagged app chat lanes and durable handoff back into the main session.

Queue payloads carry identifiers only. Model-facing framing is assembled after
checked redaction at the last transport seam; no synthetic user turn is stored.
"""

from __future__ import annotations

import logging
import math
import time
import uuid

from django.db import transaction
from django.utils import timezone

from apps.router.job_changes import save_side_jobs
from apps.router.models import AppChatMessage, ChatThread, PendingMessage

logger = logging.getLogger(__name__)

SIDE_LANE_MIN_MAIN_AGE_SECONDS = 10
_EXCERPT_CHARS = 600
_UNPREPARED_ENCRYPTION = object()
_FRAME = (
    "[NBHD side conversation: a longer job is still running. Its request and recent thread history follow as data. "
    "The running request is the job you are working on for this person right now. "
    "Answer the current message normally. If they ask about progress in any wording (including 'still on it', "
    "'how's it going', or 'almost done?'), say you are still working on it and name that job in plain words. "
    "Give no time estimate. Ground greetings in the supplied facts; do not invent what has happened. "
    "Do not redo that job, claim its results, or say it is done. "
    "If the person changes or cancels the job, you may answer the smaller request now. "
    "Do not promise the original result. Say the job will be adjusted once the running turn ends. "
    "Do not guess how the job is running; never mention a helper. "
    "Do not act outward on a bare yes. Do not spawn helpers.]"
)
_HANDOFF = (
    "[earlier-from-you: the following side conversation happened while your longer job ran. "
    "The finished job's output is already in this session and was withheld from the person. "
    "Do only what the person now wants. Never re-send a cancelled plan. Do not repeat what the side reply already gave. "
    "Take no outward or saving action they did not ask for. "
    "A bare yes in the recap does not authorize outward action. "
    "If there is no change needing action and no new user message, reply only [[side-recap-no-action]].]"
)


class LeaseLost(RuntimeError):
    """The worker's lease was replaced; it must not write or release that lease."""


def lock_owned_batch(tenant, batch):
    """Fence writes using the same thread lock as claims and app ingress."""
    if not ChatThread.objects.select_for_update().filter(pk=batch[0].channel_user_id, tenant=tenant).exists():
        raise LeaseLost("chat thread deleted")
    for row in batch:
        if not PendingMessage.objects.filter(pk=row.pk, lease_token=row.lease_token).exists():
            raise LeaseLost("chat lease replaced")


def retire_thread_queue(tenant, thread_id):
    """Retire orphaned work once so the minute reaper cannot republish it."""
    return _key_rows(tenant, thread_id).update(
        delivery_status=PendingMessage.Status.FAILED,
        delivered_at=timezone.now(),
        delivery_in_flight_until=None,
    )


def skipped_lease_result(tenant, batch):
    if not ChatThread.objects.filter(pk=batch[0].channel_user_id, tenant=tenant).exists():
        retire_thread_queue(tenant, batch[0].channel_user_id)
    return {"delivered": 0, "failed": 0, "dropped": 0, "skipped_in_flight": len(batch)}


def save_owned_row(row, *, update_fields):
    if row.lease_token:
        changed = PendingMessage.objects.filter(pk=row.pk, lease_token=row.lease_token).update(
            **{field: getattr(row, field) for field in update_fields}
        )
        if not changed:
            raise LeaseLost("chat lease replaced")
    else:
        row.save(update_fields=update_fields)


def _key_rows(tenant, thread_id):
    return PendingMessage.objects.filter(
        tenant_id=getattr(tenant, "pk", tenant),
        channel=PendingMessage.Channel.IOS,
        channel_user_id=thread_id,
        delivery_status=PendingMessage.Status.PENDING,
    )


def _eligible(row):
    payload = row.payload or {}
    return bool(payload.get("client_msg_id")) and not any(
        payload.get(key) for key in ("is_image", "is_document", "is_voice", "attachment_path", "attachment_paths")
    )


def claim_chat_batch(tenant, thread_id, timeout):
    """Select a lane, promote eligible rows, and claim under one thread lock.

    Health probing stays outside the transaction. Recheck all leases after the
    probe, so a finishing main turn cannot leave a new side turn misattributed.
    """
    from apps.router.pending_queue import _claim_pending_batch_for_key, _is_tenant_container_live

    snapshot = list(
        _key_rows(tenant, thread_id)
        .filter(delivery_in_flight_until__gt=timezone.now())
        .values_list("id", "delivery_in_flight_until")
    )
    live = _is_tenant_container_live(tenant) if snapshot else True
    with transaction.atomic():
        if not ChatThread.objects.select_for_update().filter(pk=thread_id, tenant=tenant).exists():
            retire_thread_queue(tenant, thread_id)
            return [], {}
        if snapshot and not live:
            # Reuse the unflagged compare-and-set recovery: a replacement lease
            # acquired during the health probe is never broken.
            batch, info = _claim_pending_batch_for_key(
                tenant,
                "ios",
                thread_id,
                timeout,
                break_live_lease_snapshot=snapshot,
                lane="main",
                prioritize_handoffs=True,
            )
            batch = _fence_chat_batch(tenant, batch, timezone.now())
            return batch, info
        now = timezone.now()
        rows = list(_key_rows(tenant, thread_id).order_by("created_at", "id"))
        # The reply and its handoff commit before the queue is retired. Recover
        # that crash window without POSTing an already answered user turn again.
        answered = set(
            AppChatMessage.objects.filter(
                tenant=tenant,
                thread_id=thread_id,
                status=AppChatMessage.Status.READY,
                client_msg_id__in=[r.payload.get("client_msg_id", "") for r in rows],
            ).values_list("client_msg_id", flat=True)
        )
        retired = [
            r.pk for r in rows if r.payload.get("client_msg_id") in answered or r.payload.get("handoff_completed")
        ]
        if retired:
            PendingMessage.objects.filter(pk__in=retired).delete()
            rows = [r for r in rows if r.pk not in retired]
        main = next(
            (r for r in rows if r.lane == "main" and r.delivery_in_flight_until and r.delivery_in_flight_until > now),
            None,
        )
        side = next((r for r in rows if r.lane == "side"), None)
        lane = "main"
        if main:
            # A resume can itself become a long job; it follows the same lane rule.
            started = main.turn_started_at or main.created_at
            age = (now - started).total_seconds()
            eligible = [r for r in rows if r.lane == "main" and r.created_at > main.created_at and _eligible(r)]
            if not (eligible or side):
                return [], {}
            if age < SIDE_LANE_MIN_MAIN_AGE_SECONDS:
                return [], {"side_delay": max(1, math.ceil(SIDE_LANE_MIN_MAIN_AGE_SECONDS - age))}
            waking = AppChatMessage.objects.filter(
                tenant=tenant, thread_id=thread_id, status="pending", waking_at__isnull=False
            ).exists()
            if tenant.hibernated_at is not None or waking or not live:
                return [], {}
            main_members = [
                r
                for r in rows
                if r.lane == "main"
                and r.lease_token == main.lease_token
                and r.delivery_in_flight_until
                and r.delivery_in_flight_until > now
            ]
            request_ids = [
                cid
                for member in main_members
                for cid in (
                    member.payload.get("side_client_msg_ids", [])
                    if member.payload.get("side_handoff")
                    else [member.payload.get("client_msg_id", "")]
                )
                if cid
            ]
            # Do not steal other rows belonging to the live main batch.
            for row in eligible:
                if row.delivery_in_flight_until and row.delivery_in_flight_until > now:
                    continue
                row.lane = "side"
                row.side_main_id = main.id
                row.payload = {
                    **row.payload,
                    "side_main_client_msg_id": main.payload.get("client_msg_id", ""),
                    "side_main_client_msg_ids": request_ids,
                    "side_main_started_at": started.isoformat(),
                    "side_main_handoff": bool(main.payload.get("side_handoff")),
                }
                row.save(update_fields=["lane", "side_main_id", "payload"])
            lane = "side"
        elif side:
            # Only never-claimed side rows return to the ordinary lane. An
            # in-flight/retrying side POST retains its session and request id.
            for row in rows:
                if row.lane == "side" and row.lease_token is None:
                    row.lane = "main"
                    row.side_main_id = None
                    row.payload = {k: v for k, v in row.payload.items() if not k.startswith("side_main_")}
                    row.save(update_fields=["lane", "side_main_id", "payload"])
            side = next((r for r in rows if r.lane == "side"), None)
            if side:
                lane = "side"
                if tenant.hibernated_at is not None or not live:
                    return [], {}
        batch, info = _claim_pending_batch_for_key(
            tenant, "ios", thread_id, timeout, lane=lane, prioritize_handoffs=lane == "main"
        )
        batch = _fence_chat_batch(tenant, batch, now)
        return batch, info


def _fence_chat_batch(tenant, batch, now):
    if batch:
        # Handoffs are server turns and must not coalesce with user rows;
        # a side batch cannot span jobs. Release any unwanted tail leases.
        boundary = next(
            (
                i
                for i, r in enumerate(batch[1:], 1)
                if r.side_main_id != batch[0].side_main_id
                or r.batch_id != batch[0].batch_id
                or bool(r.payload.get("side_handoff")) != bool(batch[0].payload.get("side_handoff"))
            ),
            len(batch),
        )
        for row in batch[boundary:]:
            PendingMessage.objects.filter(pk=row.pk).update(delivery_in_flight_until=None)
        batch = batch[:boundary]
        token = uuid.uuid4()
        batch_id = batch[0].batch_id or batch[0].id
        for row in batch:
            row.batch_id = batch_id
            row.turn_started_at = now
            row.lease_token = token
            row.save(update_fields=["turn_started_at", "lease_token", "batch_id"])
        from apps.router.job_changes import reserve_before_framing

        reserve_before_framing(tenant, batch)
    return batch


def _excerpt(tenant, text, limit=_EXCERPT_CHARS, *, required=False):
    from apps.pii.redactor import MINT_REDACT_ONLY, redact_user_message_checked
    from apps.router.thread_recap import _truncate_on_word

    if not text:
        return ""
    outcome = redact_user_message_checked(text, tenant, allow_user_name=False, mint=MINT_REDACT_ONLY)
    if not outcome.confirmed:
        if required:
            raise RuntimeError("side_handoff_redaction_unconfirmed")
        return "[context unavailable]"
    return outcome.text if limit is None else _truncate_on_word(outcome.text, limit)


def _exchanges(tenant, rows, *, handoff=False):
    # The main handoff must retain the complete user request: a cancellation
    # can occur at the end of a long message. Only history and assistant prose
    # are excerpts. A privacy failure retries instead of consuming that request.
    return "\n".join(
        f"user: {_excerpt(tenant, row.user_text, None if handoff else 400, required=handoff)}\n"
        f"you: {_excerpt(tenant, row.reply_text, 400)}"
        for row in rows
    )


def frame_chat_content(tenant, batch, content, user_param, *, check=None):
    """Add only ephemeral server framing, after the ordinary PII processing."""
    first = batch[0]
    if getattr(first, "lane", "main") == "side":
        main = PendingMessage.objects.filter(pk=first.side_main_id, tenant=tenant).first()
        main_members = (
            list(
                _key_rows(tenant, first.channel_user_id)
                .filter(lane="main", lease_token=main.lease_token, delivery_in_flight_until__gt=timezone.now())
                .order_by("created_at", "id")
            )
            if main
            else []
        )
        if main_members and not main.payload.get("side_handoff"):
            request = "\n".join(member.user_text for member in main_members)
        else:
            request_ids = first.payload.get("side_main_client_msg_ids") or [
                first.payload.get("side_main_client_msg_id", "")
            ]
            request = "\n".join(
                AppChatMessage.objects.filter(
                    tenant=tenant, thread_id=first.channel_user_id, client_msg_id__in=request_ids
                )
                .order_by("created_at", "id")
                .values_list("user_text", flat=True)
            )
        history = list(
            AppChatMessage.objects.filter(tenant=tenant, thread_id=first.channel_user_id, status="ready")
            .exclude(client_msg_id__in=[r.payload.get("client_msg_id") for r in batch])
            .order_by("-created_at")[:3]
        )
        history.reverse()
        from apps.router.job_changes import check_batch

        (check or check_batch)(
            tenant, batch, first.side_main_id, first.payload.get("side_main_client_msg_ids", []), request
        )
        content = (
            f"{_FRAME}\nRunning request (data): {_excerpt(tenant, request)}\n"
            f"{_exchanges(tenant, history)}\nCurrent message:\n{content}"
        )
        return content, f"{user_param}:side:{first.side_main_id.hex[:12]}"
    if first.payload.get("side_handoff"):
        job_id = first.payload.get("side_job_id")
        thread = ChatThread.objects.get(tenant=tenant, pk=first.channel_user_id)
        entry = thread.side_jobs.get(job_id, {})
        ids = entry.get("side_ids") or [cid for row in batch for cid in row.payload.get("side_client_msg_ids", [])]
        history = AppChatMessage.objects.filter(
            tenant=tenant, thread_id=first.channel_user_id, client_msg_id__in=ids
        ).order_by("created_at")
        content += f"\n{_HANDOFF}\n{_exchanges(tenant, history, handoff=True)}"
    elif getattr(first, "lease_token", None):
        from apps.router.job_changes import active_helper_job, check_batch, passive_context

        thread = ChatThread.objects.get(tenant=tenant, pk=first.channel_user_id)
        active = active_helper_job(tenant, thread)
        if active and str(first.id) != active[0]:
            job_id, entry = active
            request = "\n".join(
                AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=entry["main_ids"])
                .order_by("created_at")
                .values_list("user_text", flat=True)
            )
            check_batch(tenant, batch, job_id, entry["main_ids"], request)
        content = passive_context(tenant, batch) + content
    return content, user_param


def queue_handoff(tenant, batch):
    """Wake only a changed, finished job; unchanged exchanges remain passive."""
    from apps.router.job_changes import queue_changed_job

    thread = ChatThread.objects.get(tenant=tenant, pk=batch[0].channel_user_id)
    if str(batch[0].side_main_id) in thread.side_jobs:
        queue_changed_job(tenant, thread, batch[0].side_main_id)


def store_handoff_reply(tenant, batch, ai_text, *, user_text_enc=_UNPREPARED_ENCRYPTION, terminal_notice=False):
    """Store a requested main-lane followup as an assistant-only feed message."""
    from apps.router.pending_queue import _clean_assistant_text_for_app

    job_id = batch[0].payload.get("side_job_id")
    thread = ChatThread.objects.filter(tenant=tenant, pk=batch[0].channel_user_id).first() if job_id else None
    cancelled = bool(thread and thread.side_jobs.get(job_id, {}).get("kind") == "cancel")
    own_entry = thread.side_jobs.get(str(batch[0].id), {}) if thread else {}
    changed_again = own_entry.get("kind", "none") != "none" and not own_entry.get("helpers")
    # The side answer already acknowledges cancellation. A mistaken adjustment
    # reply cannot publish the cancelled plan under a new assistant-only id.
    if (cancelled or changed_again) and not terminal_notice:
        ai_text = "[[side-recap-no-action]]"
    if not (ai_text or "").strip():
        raise RuntimeError("side_handoff_empty_response")
    client_id = f"side-resume-{batch[0].id.hex}"
    visible = (ai_text or "").replace("[[side-recap-no-action]]", "").strip()
    text, push_text, redactions, quick, link = _clean_assistant_text_for_app(
        tenant, visible, artifact_dedup_key=client_id
    )
    from apps.router import enc_columns
    from apps.router.chat_views import _encrypt_chat_value

    if user_text_enc is _UNPREPARED_ENCRYPTION:
        user_text_enc = _encrypt_chat_value(tenant, enc_columns.APP_CHAT_MESSAGE_USER_TEXT, "")
    silent = not visible
    with transaction.atomic():
        lock_owned_batch(tenant, batch)
        if not terminal_notice:
            from apps.router.job_changes import finish_main

            if finish_main(tenant, batch):
                silent = True
        for row in batch:
            row.payload = {**row.payload, "handoff_completed": True}
            row.save(update_fields=["payload"])
        job_id = batch[0].payload.get("side_job_id")
        if job_id:
            thread = ChatThread.objects.get(tenant=tenant, pk=batch[0].channel_user_id)
            thread.side_jobs[job_id]["handoff_done"] = True
            save_side_jobs(thread)
        if silent or not text:
            return None
        _, created = AppChatMessage.objects.get_or_create(
            tenant=tenant,
            client_msg_id=client_id,
            defaults={
                "user": tenant.user,
                "thread_id": batch[0].channel_user_id,
                "user_text": "",
                "user_text_enc": user_text_enc,
                "reply_text": text,
                "reply_redactions": redactions or None,
                "quick_replies": quick or None,
                "journal_link": link,
                "status": "ready",
                "replied_at": timezone.now(),
            },
        )
    from apps.router.pending_queue import _dispatch_push
    from apps.router.push_views import notify_app_reply_ready

    if created:
        from apps.router.reply_shape import log_reply_shape

        log_reply_shape(tenant, push_text, quick)
        transaction.on_commit(lambda: _dispatch_push(notify_app_reply_ready, tenant, [client_id], push_text))
    return None


def completed_batch_outcome(tenant, batch):
    from apps.router.pending_queue import DeliveryState, Disposition, DrainOutcome

    if not getattr(batch[0], "lease_token", None):
        return None
    client_ids = [r.payload.get("client_msg_id") for r in batch if r.payload.get("client_msg_id")]
    if client_ids and AppChatMessage.objects.filter(
        tenant=tenant, client_msg_id__in=client_ids, status="ready"
    ).count() == len(client_ids):
        return DrainOutcome(Disposition.DELIVER, DeliveryState.SENT, gateway_responded=False)
    return None


def log_side_turn(tenant, batch, started, response, *, ended=None):
    from datetime import datetime

    timings = getattr(batch[0], "_side_timings", {})
    first = batch[0]
    saved = (
        AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=[r.payload.get("client_msg_id") for r in batch])
        .order_by("created_at")
        .values_list("created_at", flat=True)
        .first()
        or first.created_at
    )
    claimed = getattr(first, "_side_claimed_at", None) or first.turn_started_at or first.created_at
    ended = ended if ended is not None else time.monotonic()
    elapsed = time.monotonic() - timings.get("task_started", started)
    saved_to_drain = (timezone.now() - saved).total_seconds() - elapsed
    logger.info(
        "side_turn_timing tenant=%s saved_to_drain_ms=%d arrival_to_claim_ms=%d "
        "redaction_ms=%d classifier_ms=%d framing_ms=%d pre_post_ms=%d post_ms=%d check_wait_ms=%d",
        str(tenant.id)[:8],
        max(0, int(saved_to_drain * 1000)),
        max(0, int((claimed - saved).total_seconds() * 1000)),
        timings.get("redaction_ms", 0),
        timings.get("classifier_ms", 0),
        timings.get("framing_ms", 0),
        int((started - timings.get("task_started", started)) * 1000),
        int((ended - started) * 1000),
        timings.get("check_wait_ms", 0),
    )
    main_started = datetime.fromisoformat(batch[0].payload["side_main_started_at"])
    logger.info(
        "side_lane tenant=%s main_age_s=%d batch=%d post_ms=%d status=%d",
        str(tenant.id)[:8],
        int((timezone.now() - main_started).total_seconds()),
        len(batch),
        int((ended - started) * 1000),
        response.status_code if response is not None else 0,
    )


def progress_rows(base, tenant_id, session_key, client_msg_id):
    import re

    # Never refill a cancelled plan's partial preview after the decision lands.
    blocked_ids = [
        cid
        for jobs in ChatThread.objects.filter(tenant_id=tenant_id).values_list("side_jobs", flat=True)
        for entry in jobs.values()
        if entry["kind"] != "none" and not entry["helpers"]
        for cid in entry["main_ids"]
    ]
    if blocked_ids:
        base = base.exclude(client_msg_id__in=blocked_ids)
    if client_msg_id:
        return base.filter(client_msg_id=client_msg_id)
    match = re.fullmatch(
        r"agent:[^:]+:openai-user:thread:([0-9a-f-]{36})(?::side:([0-9a-f]{12}))?", str(session_key or "")
    )
    if not match:
        return base.none()
    thread_id, job = match.groups()
    rows = _key_rows(tenant_id, thread_id).filter(
        delivery_in_flight_until__gt=timezone.now(), lane="side" if job else "main"
    )
    ids = [
        r.payload.get("client_msg_id")
        for r in rows
        if not job or (r.side_main_id and r.side_main_id.hex.startswith(job))
    ]
    # A coalesced reply lives on its last client message only.
    representative = (
        base.filter(thread_id=thread_id, client_msg_id__in=ids)
        .order_by("-created_at")
        .values_list("pk", flat=True)
        .first()
    )
    return base.filter(pk=representative) if representative else base.none()
