"""Phase 1 spike: can Django fire a cron job straight into a tenant container?

Always-on plan, Phase 1 (``CONTINUITY_always_on_architecture.md``): Django
becomes the only timer. Before building it, two questions need measured
answers on the canary:

- **Q2:** Does today's cron prompt (the Morning Briefing) work unchanged when
  Django sends it as a turn in an isolated session (``user=timer-spike:…``,
  which OpenClaw maps to its own session, not the user's chat)?
- **Q3:** How long does a cold wake take when the turn is fired at the due time
  instead of pre-waking? This determines whether Phase 1 can drop cron-wake
  arming.

``timer_spike_task`` runs one fire in a background thread, so it never holds a
QStash request past gunicorn's 300s timeout. It logs one content-free timing
line and nothing else. The assistant's reply is discarded, never relayed to
chat; a briefing reaches the user only through the assistant's own
``nbhd_send_to_user`` tool, exactly as with a real cron run.

Gated by ``TIMER_SPIKE_TENANT_IDS`` (empty = nobody). This module is
temporary: delete it once Phase 1 has its real scheduler.
"""

from __future__ import annotations

import logging
import threading
import uuid
from time import monotonic, sleep

import httpx

logger = logging.getLogger(__name__)

GATE_SETTING = "TIMER_SPIKE_TENANT_IDS"
DEADLINE_SECONDS = 290
RETRY_SLEEP_SECONDS = 5
NOT_READY_STATUSES = {404, 502, 503, 504}

PING_MESSAGE = (
    "This is an automated system timing test from the nbhd team. "
    "Reply with exactly: OK. Do not call any tools and do not message the user."
)
BRIEFING_PREFIX = (
    "[nbhd test run] This is a one-off TEST of a new delivery path. When you send the briefing "
    "to the user, start the message with: '🧪 Test briefing (new delivery path) — please ignore:'.\n\n"
)


def _message(tenant, mode: str) -> str:
    if mode == "briefing":
        from apps.orchestrator.config_generator import _build_cron_message, _build_morning_briefing_prompt

        return BRIEFING_PREFIX + _build_cron_message(
            _build_morning_briefing_prompt(tenant), "Morning Briefing", foreground=True, tenant=tenant
        )
    return PING_MESSAGE


def run_spike(tenant_id: str, mode: str = "ping", *, post=None, wake=None, clock=monotonic, pause=sleep) -> dict:
    """One fire. Returns (and logs) a content-free timing summary."""
    from apps.cron.gateway_client import get_gateway_token_for_tenant
    from apps.orchestrator.hibernation import wake_hibernated_tenant
    from apps.router.chat_gates import _tenant_allowed
    from apps.tenants.models import Tenant

    post = post or httpx.post
    wake = wake or wake_hibernated_tenant
    result = {"mode": mode, "status": "skipped", "hibernated": False}
    tenant = Tenant.objects.select_related("user").filter(pk=tenant_id).first()
    if (
        tenant is None
        or not tenant.container_fqdn
        or tenant.status != Tenant.Status.ACTIVE
        or not _tenant_allowed(tenant, GATE_SETTING)
    ):
        result["reason"] = "not_allowed"
        logger.info("timer_spike tenant=%s outcome=skipped reason=not_allowed", str(tenant_id)[:8])
        return result
    # The briefing has real side effects (daily-note writes, a message). It
    # runs only on a warm container and is POSTed exactly once: a retry after
    # a mid-turn disconnect would run the whole briefing twice, and the idle
    # sweep could hibernate a freshly woken container mid-turn.
    if mode == "briefing" and tenant.hibernated_at is not None:
        result["reason"] = "briefing_needs_warm_container"
        logger.info("timer_spike tenant=%s outcome=skipped reason=briefing_needs_warm_container", str(tenant.id)[:8])
        return result

    started = clock()
    result["hibernated"] = tenant.hibernated_at is not None
    if result["hibernated"]:
        wake_started = clock()
        result["woke"] = bool(wake(tenant))
        result["wake_call_s"] = round(clock() - wake_started, 1)

    body = {
        "model": "openclaw",
        "user": f"timer-spike:{mode}:{uuid.uuid4().hex[:8]}",
        "messages": [{"role": "user", "content": _message(tenant, mode)}],
    }
    headers = {
        "Authorization": f"Bearer {get_gateway_token_for_tenant(tenant)}",
        "X-User-Timezone": getattr(tenant.user, "timezone", None) or "UTC",
    }
    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    attempts = 0
    while True:
        attempts += 1
        attempt_started = clock()
        try:
            resp = post(url, json=body, headers=headers, timeout=httpx.Timeout(240.0, connect=10.0))
            code = resp.status_code
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            code = None
        except httpx.TimeoutException:
            result.update(status="timeout", attempts=attempts)
            break
        if code is not None and code not in NOT_READY_STATUSES:
            result["ready_s"] = round(attempt_started - started, 1)
            result["turn_s"] = round(clock() - attempt_started, 1)
            result["status"] = "ok" if 200 <= code < 300 else f"http_{code}"
            result["attempts"] = attempts
            if 200 <= code < 300:
                try:
                    reply = resp.json()["choices"][0]["message"]["content"] or ""
                except Exception:
                    reply = ""
                result["reply_chars"] = len(reply)
            break
        if mode == "briefing":
            result.update(status=f"not_ready_{code or 'conn'}", attempts=attempts)
            break
        if clock() - started > DEADLINE_SECONDS:
            result.update(status="not_ready", attempts=attempts)
            break
        pause(RETRY_SLEEP_SECONDS)
    result["total_s"] = round(clock() - started, 1)
    logger.info(
        "timer_spike tenant=%s mode=%s hibernated=%s wake_call_s=%s ready_s=%s turn_s=%s total_s=%s "
        "attempts=%s status=%s reply_chars=%s",
        str(tenant.id)[:8],
        mode,
        int(result["hibernated"]),
        result.get("wake_call_s", "-"),
        result.get("ready_s", "-"),
        result.get("turn_s", "-"),
        result["total_s"],
        result.get("attempts", "-"),
        result["status"],
        result.get("reply_chars", "-"),
    )
    return result


def timer_spike_task(tenant_id: str, mode: str = "ping") -> dict:
    """QStash entry point (``TASK_MAP['timer_spike']``). Returns immediately."""
    if mode not in ("ping", "briefing"):
        return {"started": False, "reason": "bad_mode"}

    def _run():
        from django.db import connection

        try:
            run_spike(str(tenant_id), mode)
        except Exception:
            logger.exception("timer_spike tenant=%s outcome=error", str(tenant_id)[:8])
        finally:
            connection.close()

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "mode": mode}
