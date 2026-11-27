"""Silent-degradation alarm for fail-open features.

Several features quietly fall back to old behaviour when a dependency fails:
Jev routing, chat shape, talk route. That is right for users, but it means an
outage can run for weeks unnoticed. Every TypeSafe Jev call failed from
2026-11-05 to 2026-11-27 because OpenRouter added two usage fields; the only
trace was ``reason=unavailable`` log lines.

``record_outcome`` counts successes and failures per feature in hourly Redis
buckets. ``check_degradation`` (an hourly system cron) looks at the last
``WINDOW_HOURS`` complete hours. When a feature made at least ``MIN_CALLS``
calls and at least ``FAIL_RATIO`` of them failed, it sends one operator alert
(Pushover, email fallback), then stays quiet for that feature for
``COOLDOWN_SECONDS``.

Content-free by construction:
- feature names come from ``FEATURES``;
- a failure reason is an exception class name checked against ``_SAFE_REASON``;
- nothing else is stored.

Every function here is best-effort and never raises into the caller's path.
"""

from __future__ import annotations

import logging
import re
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

FEATURES = frozenset({"jev"})
WINDOW_HOURS = 3
MIN_CALLS = 5
FAIL_RATIO = 0.5
COOLDOWN_SECONDS = 6 * 60 * 60
BUCKET_TTL_SECONDS = (WINDOW_HOURS + 2) * 60 * 60
_SAFE_REASON = re.compile(r"\A[A-Za-z][A-Za-z0-9_]{0,63}\Z")


def _hour_bucket(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // 3600)


def _key(feature: str, bucket: int, field: str) -> str:
    return f"degradation:{feature}:{bucket}:{field}"


def _incr(key: str) -> None:
    # add() is a no-op when the key exists, so the TTL is set once per bucket.
    cache.add(key, 0, BUCKET_TTL_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        # The key expired between add() and incr(); losing one count is fine.
        pass


def record_outcome(feature: str, ok: bool, *, reason: str | None = None, now: float | None = None) -> None:
    """Count one call outcome for ``feature``. Never raises."""
    try:
        if feature not in FEATURES:
            return
        bucket = _hour_bucket(now)
        _incr(_key(feature, bucket, "ok" if ok else "fail"))
        if not ok and reason and _SAFE_REASON.match(reason):
            cache.set(_key(feature, bucket, "reason"), reason, BUCKET_TTL_SECONDS)
    except Exception:
        logger.warning("degradation_record_failed feature=%s", feature if feature in FEATURES else "?")


def record_outcome_async(feature: str, ok: bool, *, reason: str | None = None) -> None:
    """Fire-and-forget ``record_outcome`` for latency-bounded callers.

    Jev runs under the caller's wall-clock budget (``future.result(timeout=…)``);
    a slow Redis must never turn a good answer into a timeout.
    """
    import threading

    try:
        threading.Thread(target=record_outcome, args=(feature, ok), kwargs={"reason": reason}, daemon=True).start()
    except Exception:
        logger.warning("degradation_record_async_failed")


def window_counts(feature: str, *, now: float | None = None) -> tuple[int, int, str]:
    """Return ``(ok, fail, last_reason)`` over the last ``WINDOW_HOURS`` complete hours."""
    current = _hour_bucket(now)
    ok = fail = 0
    reason = ""
    for bucket in range(current - WINDOW_HOURS, current):
        ok += int(cache.get(_key(feature, bucket, "ok")) or 0)
        fail += int(cache.get(_key(feature, bucket, "fail")) or 0)
        reason = cache.get(_key(feature, bucket, "reason")) or reason
    return ok, fail, reason


def degraded_features(*, now: float | None = None) -> list[dict]:
    """Features over the failure threshold in the current window."""
    out = []
    for feature in sorted(FEATURES):
        ok, fail, reason = window_counts(feature, now=now)
        total = ok + fail
        if total >= MIN_CALLS and fail / total >= FAIL_RATIO:
            out.append({"feature": feature, "ok": ok, "fail": fail, "reason": reason})
    return out


def check_degradation(*, now: float | None = None) -> dict:
    """Hourly task: alert once per feature per cooldown. Returns a content-free summary."""
    try:
        degraded = degraded_features(now=now)
    except Exception:
        logger.warning("degradation_check_failed")
        return {"degraded": [], "alerted": [], "error": True}

    fresh = [d for d in degraded if not cache.get(f"degradation:alerted:{d['feature']}")]
    for d in degraded:
        logger.warning(
            "degradation feature=%s ok=%d fail=%d reason=%s window_h=%d",
            d["feature"],
            d["ok"],
            d["fail"],
            d["reason"] or "-",
            WINDOW_HOURS,
        )
    if not fresh:
        return {"degraded": [d["feature"] for d in degraded], "alerted": []}

    lines = [f"NBHD silent failure — last {WINDOW_HOURS}h:"]
    for d in fresh:
        total = d["ok"] + d["fail"]
        why = f" ({d['reason']})" if d["reason"] else ""
        lines.append(f"  - {d['feature']}: {d['fail']}/{total} calls failed{why}")
    lines.append("Features fall back quietly, so users see no error. Check logs for degradation lines.")

    from apps.common.operator_alert import send_operator_alert

    status = send_operator_alert("\n".join(lines), subject="NBHD silent failure")
    alerted = []
    if status in ("delivered", "undeliverable"):
        for d in fresh:
            cache.set(f"degradation:alerted:{d['feature']}", True, COOLDOWN_SECONDS)
            alerted.append(d["feature"])
    return {"degraded": [d["feature"] for d in degraded], "alerted": alerted, "alert_status": status}


def check_degradation_task() -> dict:
    """QStash entry point (``TASK_MAP['check_degradation']``)."""
    return check_degradation()
