"""Proactive signals: "notice things for me", Phase 1 (calendar).

Plain code notices facts and a fixed template writes the heads-up. No model
call, no container wake, no new path for user data to leave nbhd. The user's
own assistant still sees every heads-up afterwards, because delivered
heads-ups are ordinary ``ProactiveOutbound`` rows and surface in its
``[earlier-from-you]`` context.

Flow:
1. ``commit_sync_run`` reports new / moved calendar events after commit and
   queues ``detect_calendar_signals``.
2. Detection enriches each event with facts:
   - public holiday in the user's own country or in a country they work with;
   - starts outside usual hours;
   - overlaps another meeting.
   Each fact becomes a ``ProactiveSignal`` row (codes, dates and holiday
   names only).
3. ``deliver_proactive_signals`` (every 15 min) sends pending signals inside
   the user's heartbeat window, at most ``DAILY_CAP`` per local day, and drops
   any whose event has already started.

Event titles, notes and locations are read in memory only, to decide whether
something looks like a meeting with other people. They are never stored on a
signal or put in a message: those fields are written by strangers (invites).

Design: ``ANALYSIS_proactive_everything.md`` (rev 2). Plan:
``CONTINUITY_proactive_signals.md``.
"""

from __future__ import annotations

import logging
import re
import zoneinfo
from datetime import datetime, timedelta
from functools import lru_cache

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

JOB_NAME = "_proactive:calendar"
GATE_SETTING = "PROACTIVE_SIGNALS_TENANT_IDS"
LOOKAHEAD_DAYS = 45
BULK_CHANGE_LIMIT = 20
DAILY_CAP = 3
EARLY_HOUR = 7
LATE_HOUR = 22
FALLBACK_WINDOW = (9, 12)  # start hour, length in hours, used when heartbeat is off
STALE_CLAIM = timedelta(minutes=10)

# Supported "countries I work with": ISO-2 -> a representative time zone, used
# to read the event's local date in that country.
COUNTRY_TZ: dict[str, str] = {
    "US": "America/New_York",
    "CA": "America/Toronto",
    "MX": "America/Mexico_City",
    "BR": "America/Sao_Paulo",
    "GB": "Europe/London",
    "IE": "Europe/Dublin",
    "FR": "Europe/Paris",
    "DE": "Europe/Berlin",
    "IT": "Europe/Rome",
    "ES": "Europe/Madrid",
    "NL": "Europe/Amsterdam",
    "CH": "Europe/Zurich",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "CN": "Asia/Shanghai",
    "TW": "Asia/Taipei",
    "HK": "Asia/Hong_Kong",
    "SG": "Asia/Singapore",
    "PH": "Asia/Manila",
    "IN": "Asia/Kolkata",
    "AU": "Australia/Sydney",
    "NZ": "Pacific/Auckland",
}

COUNTRY_LABEL: dict[str, str] = {
    "US": "the US",
    "GB": "the UK",
    "NL": "the Netherlands",
    "PH": "the Philippines",
    "CA": "Canada",
    "MX": "Mexico",
    "BR": "Brazil",
    "IE": "Ireland",
    "FR": "France",
    "DE": "Germany",
    "IT": "Italy",
    "ES": "Spain",
    "CH": "Switzerland",
    "JP": "Japan",
    "KR": "South Korea",
    "CN": "China",
    "TW": "Taiwan",
    "HK": "Hong Kong",
    "SG": "Singapore",
    "IN": "India",
    "AU": "Australia",
    "NZ": "New Zealand",
}

# User time zone -> their own country (for their own public holidays).
_US_ZONES = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Anchorage",
    "America/Detroit",
    "Pacific/Honolulu",
    "America/Boise",
    "America/Indiana/Indianapolis",
)
TZ_COUNTRY: dict[str, str] = {tz: code for code, tz in COUNTRY_TZ.items()}
TZ_COUNTRY.update({tz: "US" for tz in _US_ZONES})
TZ_COUNTRY.update(
    {
        "America/Vancouver": "CA",
        "America/Edmonton": "CA",
        "America/Winnipeg": "CA",
        "America/Halifax": "CA",
        "Australia/Melbourne": "AU",
        "Australia/Brisbane": "AU",
        "Australia/Perth": "AU",
        "Australia/Adelaide": "AU",
    }
)

# A calendar entry "looks like a meeting with other people" when its text has
# one of these (links match anywhere, words on word boundaries, so "recall" is
# not "call"). Personal blocks (gym, dinner) never trigger heads-ups.
_MEETING_LINKS = (
    "zoom.us",
    "meet.google",
    "teams.microsoft",
    "teams.live",
    "webex",
    "whereby",
    "calendly",
    "cal.com",
)
_MEETING_WORDS = re.compile(r"\b(meeting|call|sync|interview|with|1:1|standup|catch[- ]?up)\b", re.IGNORECASE)


# ── gates ────────────────────────────────────────────────────────────────────


def gate_allows(tenant) -> bool:
    """Env canary gate plus the user's own opt-in. Cheap, no DB."""
    from apps.router.chat_gates import _tenant_allowed

    return bool(getattr(tenant, "proactive_signals_enabled", False)) and _tenant_allowed(tenant, GATE_SETTING)


def signals_enabled(tenant) -> bool:
    """Full readiness: gate, opt-in and a live calendar mirror."""
    if not gate_allows(tenant):
        return False
    from apps.datebook.readiness import datebook_delivery_ready

    try:
        return bool(datebook_delivery_ready(tenant))
    except Exception:
        return False


# ── facts (pure) ─────────────────────────────────────────────────────────────


def user_country(tz_name: str) -> str | None:
    return TZ_COUNTRY.get(tz_name or "")


def clean_countries(values) -> list[str]:
    """Normalize a user-supplied list to supported ISO-2 codes (order kept, no dupes)."""
    out: list[str] = []
    for value in values or []:
        code = str(value or "").strip().upper()
        if code in COUNTRY_TZ and code not in out:
            out.append(code)
    return out


@lru_cache(maxsize=256)
def _country_holidays(code: str, year: int):
    import holidays

    # Force English: without ``language`` the package follows the process
    # locale and Linux (LANG=C) yields e.g. "勤労感謝の日" / "Neujahr".
    return holidays.country_holidays(code, years=year, language="en_US")


def holiday_hits(start_utc: datetime, countries: list[str]) -> list[dict]:
    """Public holidays on the event's local date in each country."""
    hits = []
    for code in countries:
        tz_name = COUNTRY_TZ.get(code)
        if not tz_name:
            continue
        local_date = start_utc.astimezone(zoneinfo.ZoneInfo(tz_name)).date()
        try:
            name = _country_holidays(code, local_date.year).get(local_date)
        except Exception:
            logger.warning("proactive_signal holiday_lookup_failed country=%s", code)
            continue
        if name:
            hits.append({"country": code, "holiday": str(name)[:80], "date": local_date.isoformat()})
    return hits


def outside_usual_hours(start_local: datetime) -> bool:
    return start_local.hour < EARLY_HOUR or start_local.hour >= LATE_HOUR


def looks_like_meeting(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts)
    lowered = blob.lower()
    return any(link in lowered for link in _MEETING_LINKS) or bool(_MEETING_WORDS.search(blob))


# ── detection ────────────────────────────────────────────────────────────────


def _overlaps_other(tenant, event) -> bool:
    from apps.datebook.models import MirrorEvent, TimeKind

    if not event.zoned_end_at or event.zoned_end_at <= event.zoned_start_at:
        return False
    others = (
        MirrorEvent.objects.filter(
            tenant=tenant,
            active=True,
            time_kind=TimeKind.ZONED,
            zoned_start_at__lt=event.zoned_end_at,
            zoned_end_at__gt=event.zoned_start_at,
        )
        .exclude(pk=event.pk)
        # The same meeting copied into two calendars (same slot, same title)
        # is not an overlap; two different meetings in one slot still are.
        .exclude(zoned_start_at=event.zoned_start_at, zoned_end_at=event.zoned_end_at, title=event.title)
    )
    return others.exists()


def signals_for_event(tenant, event, *, now: datetime, tz_name: str) -> list[dict]:
    """Facts worth a heads-up for one calendar event. Pure apart from the overlap query."""
    from apps.datebook.models import TimeKind

    if event.time_kind != TimeKind.ZONED or not event.zoned_start_at:
        return []
    start = event.zoned_start_at
    if start <= now or start > now + timedelta(days=LOOKAHEAD_DAYS):
        return []
    if not looks_like_meeting(event.title, event.location, event.notes):
        return []

    user_tz = zoneinfo.ZoneInfo(tz_name)
    start_local = start.astimezone(user_tz)
    base = {"start": start.isoformat(), "tz": tz_name}
    out: list[dict] = []

    countries = clean_countries([user_country(tz_name), *(getattr(tenant, "work_countries", None) or [])])
    for hit in holiday_hits(start, countries):
        out.append(
            {
                "kind": "calendar_holiday",
                "facts": {**base, **hit},
                "dedup_key": f"holiday:{event.id}:{hit['date']}:{hit['country']}",
            }
        )
    if outside_usual_hours(start_local):
        out.append(
            {
                "kind": "calendar_outside_hours",
                "facts": base,
                "dedup_key": f"hours:{event.id}:{start_local:%Y-%m-%dT%H:%M}",
            }
        )
    if _overlaps_other(tenant, event):
        out.append(
            {
                "kind": "calendar_overlap",
                "facts": base,
                "dedup_key": f"overlap:{event.id}:{start_local:%Y-%m-%dT%H:%M}",
            }
        )
    return out


def detect_calendar_signals(tenant_id: str, event_ids: list[str], *, now: datetime | None = None) -> dict:
    """Create signals for changed calendar events. Idempotent per ``dedup_key``."""
    from apps.common.tenant_tz import tenant_tz_name
    from apps.datebook.models import MirrorEvent
    from apps.router.models import ProactiveSignal
    from apps.tenants.models import Tenant

    now = now or timezone.now()
    tenant = Tenant.objects.select_related("user").filter(pk=tenant_id).first()
    if tenant is None or not signals_enabled(tenant):
        return {"created": 0, "reason": "disabled"}
    tz_name = tenant_tz_name(tenant)
    created = 0
    events = MirrorEvent.objects.filter(tenant=tenant, active=True, pk__in=list(event_ids)[:BULK_CHANGE_LIMIT])
    for event in events:
        for item in signals_for_event(tenant, event, now=now, tz_name=tz_name):
            try:
                with transaction.atomic():
                    _, made = ProactiveSignal.objects.get_or_create(
                        tenant=tenant,
                        dedup_key=item["dedup_key"],
                        defaults={
                            "kind": item["kind"],
                            "event": event,
                            "facts": item["facts"],
                            "expires_at": event.zoned_start_at,
                        },
                    )
            except IntegrityError:
                made = False
            if made:
                created += 1
                logger.info("proactive_signal tenant=%s kind=%s outcome=created", str(tenant.id)[:8], item["kind"])
    return {"created": created, "reason": "ok"}


def detect_calendar_signals_task(tenant_id: str, event_ids: list | None = None) -> dict:
    """QStash entry point (``TASK_MAP['detect_calendar_signals']``). Never raises."""
    try:
        return detect_calendar_signals(str(tenant_id), [str(e) for e in (event_ids or [])])
    except Exception:
        logger.exception("proactive_signal detect_failed tenant=%s", str(tenant_id)[:8])
        return {"created": 0, "reason": "error"}


def queue_calendar_detection(
    tenant, changes: list[tuple[str, str, datetime | None]], *, previous_window_end, first_sync: bool
) -> None:
    """Called from ``commit_sync_run`` with the events it just published.

    ``changes`` holds ``(event_id, "new" | "moved", start)``. Suppresses:
    - first syncs and full re-syncs (everything looks new);
    - roll-ins (events that only entered the window because the window moved);
    - bulk runs (more than ``BULK_CHANGE_LIMIT`` changes).
    The task is published after commit, so the sync path never waits on or
    fails because of signals.
    """
    if not changes or not gate_allows(tenant):
        return
    if first_sync:
        logger.info("proactive_signal tenant=%s outcome=suppressed reason=first_sync", str(tenant.id)[:8])
        return
    candidates = [
        event_id
        for event_id, kind, start in changes
        if kind == "moved" or previous_window_end is None or start is None or start <= previous_window_end
    ]
    if not candidates:
        return
    if len(candidates) > BULK_CHANGE_LIMIT:
        logger.info(
            "proactive_signal tenant=%s outcome=suppressed reason=bulk n=%d", str(tenant.id)[:8], len(candidates)
        )
        return

    tenant_id = str(tenant.id)
    ids = list(candidates)

    def _publish():
        try:
            from apps.cron.publish import publish_task

            publish_task("detect_calendar_signals", tenant_id, ids)
        except Exception:
            logger.warning("proactive_signal tenant=%s outcome=publish_failed", tenant_id[:8])

    transaction.on_commit(_publish)


# ── delivery ─────────────────────────────────────────────────────────────────


def in_delivery_window(tenant, now_local: datetime) -> bool:
    if getattr(tenant, "heartbeat_enabled", False):
        start, length = tenant.heartbeat_start_hour, tenant.heartbeat_window_hours
    else:
        start, length = FALLBACK_WINDOW
    return now_local.hour in {(start + i) % 24 for i in range(length)}


def _when(facts: dict) -> tuple[str, str]:
    start = datetime.fromisoformat(facts["start"]).astimezone(zoneinfo.ZoneInfo(facts["tz"]))
    return f"{start:%a} {start.day} {start:%b}", f"{start:%H:%M}"


def render_line(kind: str, facts: dict) -> str:
    day, time = _when(facts)
    if kind == "calendar_holiday":
        where = COUNTRY_LABEL.get(facts.get("country", ""), facts.get("country", ""))
        return (
            f"Your {day} {time} meeting (your time) falls on {facts.get('holiday', 'a public holiday')} "
            f"in {where}. Worth checking the time still works for everyone."
        )
    if kind == "calendar_outside_hours":
        return f"Your meeting on {day} starts at {time} your time, outside your usual hours."
    if kind == "calendar_overlap":
        return f"Your meeting on {day} at {time} (your time) overlaps something else in your calendar."
    return ""


def render_message(signals) -> str:
    lines = [line for line in (render_line(s.kind, s.facts) for s in signals) if line]
    if len(lines) == 1:
        return f"Heads up: {lines[0]}"
    # Plain lines, not bullets: [earlier-from-you] reads bulleted proactive
    # messages as items the user was asked about.
    return "Heads up on your calendar:\n" + "\n".join(lines)


def still_true(tenant, signal) -> bool:
    """The fact behind a pending signal still holds on the live calendar."""
    event = signal.event
    if event is None or not event.active or not event.zoned_start_at:
        return False
    try:
        if event.zoned_start_at != datetime.fromisoformat(signal.facts.get("start", "")):
            return False
    except (TypeError, ValueError):
        return False
    if signal.kind == "calendar_overlap":
        return _overlaps_other(tenant, event)
    return True


def _local_day_start(now: datetime, tz) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def deliver_proactive_signals(*, now: datetime | None = None) -> dict:
    from apps.common.tenant_tz import tenant_tz
    from apps.router.cron_delivery import resolve_user_channel
    from apps.router.models import ProactiveOutbound, ProactiveSignal
    from apps.router.proactive_context import record_proactive_outbound

    now = now or timezone.now()
    Status = ProactiveSignal.Status
    summary = {"expired": 0, "sent": 0, "dropped": 0, "held": 0}

    # Crash recovery for claims older than STALE_CLAIM: if the heads-up row was
    # already written after the claim, the run died before marking SENT, so mark
    # it SENT now; otherwise return it to pending. Never sends twice.
    for stale in ProactiveSignal.objects.filter(status=Status.SENDING, claimed_at__lt=now - STALE_CLAIM):
        delivered = ProactiveOutbound.objects.filter(
            tenant_id=stale.tenant_id, job_name=JOB_NAME, created_at__gte=stale.claimed_at
        ).exists()
        ProactiveSignal.objects.filter(pk=stale.pk, status=Status.SENDING).update(
            **(
                {"status": Status.SENT, "handled_at": now, "outcome_reason": "recovered"}
                if delivered
                else {"status": Status.PENDING, "claimed_at": None}
            )
        )
    summary["expired"] = ProactiveSignal.objects.filter(status=Status.PENDING, expires_at__lte=now).update(
        status=Status.EXPIRED, handled_at=now, outcome_reason="event_started"
    )

    pending = (
        ProactiveSignal.objects.filter(status=Status.PENDING)
        .select_related("tenant", "tenant__user", "event")
        .order_by("tenant_id", "expires_at")
    )
    by_tenant: dict = {}
    for signal in pending:
        by_tenant.setdefault(signal.tenant_id, []).append(signal)

    for signals in by_tenant.values():
        tenant = signals[0].tenant
        tid = str(tenant.id)[:8]
        if tenant.status != tenant.Status.ACTIVE or not signals_enabled(tenant):
            n = ProactiveSignal.objects.filter(pk__in=[s.pk for s in signals], status=Status.PENDING).update(
                status=Status.DROPPED, handled_at=now, outcome_reason="disabled"
            )
            summary["dropped"] += n
            logger.info("proactive_signal tenant=%s outcome=dropped reason=disabled n=%d", tid, n)
            continue
        # Re-check each signal against the live calendar before claiming it: a
        # meeting moved or deleted since detection must not get a stale heads-up.
        fresh, stale = [], []
        for signal in signals:
            (fresh if still_true(tenant, signal) else stale).append(signal)
        if stale:
            n = ProactiveSignal.objects.filter(pk__in=[s.pk for s in stale], status=Status.PENDING).update(
                status=Status.DROPPED, handled_at=now, outcome_reason="event_changed"
            )
            summary["dropped"] += n
            logger.info("proactive_signal tenant=%s outcome=dropped reason=event_changed n=%d", tid, n)

        signals = fresh
        if not signals:
            continue
        tz = tenant_tz(tenant)
        if not in_delivery_window(tenant, now.astimezone(tz)):
            summary["held"] += len(signals)
            continue
        sent_today = ProactiveSignal.objects.filter(
            tenant=tenant, status=Status.SENT, handled_at__gte=_local_day_start(now, tz)
        ).count()
        remaining = DAILY_CAP - sent_today
        if remaining <= 0:
            summary["held"] += len(signals)
            logger.info("proactive_signal tenant=%s outcome=held reason=daily_cap", tid)
            continue
        user = getattr(tenant, "user", None)
        channel = resolve_user_channel(user) if user is not None else None
        if channel != "app":
            n = ProactiveSignal.objects.filter(pk__in=[s.pk for s in signals], status=Status.PENDING).update(
                status=Status.DROPPED, handled_at=now, outcome_reason="channel_unsupported"
            )
            summary["dropped"] += n
            logger.info("proactive_signal tenant=%s outcome=dropped reason=channel_unsupported n=%d", tid, n)
            continue

        claimed = []
        for signal in signals[:remaining]:
            if ProactiveSignal.objects.filter(pk=signal.pk, status=Status.PENDING).update(
                status=Status.SENDING, claimed_at=now
            ):
                claimed.append(signal)
        if not claimed:
            continue
        ids = [s.pk for s in claimed]
        try:
            # No outer transaction: record_proactive_outbound may call the key
            # broker (recall capture), and network I/O must never run inside a
            # transaction. A crash between the two writes is closed by the
            # stale-claim recovery above, which marks already-delivered claims SENT.
            row = record_proactive_outbound(
                tenant=tenant,
                channel="app",
                channel_user_id=str(user.id),
                message_text=render_message(claimed),
                job_name=JOB_NAME,
            )
        except Exception:
            row = None
            logger.warning("proactive_signal tenant=%s outcome=send_failed", tid, exc_info=True)
        if row is None:
            ProactiveSignal.objects.filter(pk__in=ids, status=Status.SENDING).update(
                status=Status.PENDING, claimed_at=None
            )
            continue
        ProactiveSignal.objects.filter(pk__in=ids, status=Status.SENDING).update(
            status=Status.SENT, handled_at=now, outcome_reason="ok"
        )
        summary["sent"] += len(ids)
        for signal in claimed:
            logger.info("proactive_signal tenant=%s kind=%s outcome=sent", tid, signal.kind)
    return summary


def deliver_proactive_signals_task() -> dict:
    """QStash entry point (``TASK_MAP['deliver_proactive_signals']``)."""
    try:
        return deliver_proactive_signals()
    except Exception:
        logger.exception("proactive_signal deliver_failed")
        return {"error": True}
