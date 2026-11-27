from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.datebook.models import MirrorEvent
from apps.datebook.tests import DatebookAPIMixin, _ready_tenant, _zoned_event
from apps.router import proactive_signals as ps
from apps.router.models import ProactiveOutbound, ProactiveSignal

# Thursday 2026-11-26 is US Thanksgiving. 15:00 UTC = 10:00 New York = Fri 00:00 Tokyo.
THANKSGIVING_10AM_NY = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)
BEFORE = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)  # "now" for detection tests


class PureFactTests(SimpleTestCase):
    def test_us_thanksgiving_hit_in_country_local_date(self):
        hits = ps.holiday_hits(THANKSGIVING_10AM_NY, ["US", "JP"])
        self.assertEqual([h["country"] for h in hits], ["US"])
        self.assertIn("Thanksgiving", hits[0]["holiday"])
        self.assertEqual(hits[0]["date"], "2026-11-26")

    def test_holiday_names_are_english(self):
        # 2026-11-23 is Labor Thanksgiving Day in Japan; never the Japanese name.
        jp = ps.holiday_hits(datetime(2026, 11, 23, 1, 0, tzinfo=UTC), ["JP"])
        self.assertEqual(jp[0]["holiday"], "Labor Thanksgiving Day")

    def test_user_country_and_clean_countries(self):
        self.assertEqual(ps.user_country("Asia/Tokyo"), "JP")
        self.assertEqual(ps.user_country("America/Los_Angeles"), "US")
        self.assertIsNone(ps.user_country("Mars/Base"))
        self.assertEqual(ps.clean_countries(["us", "US", "zz", None, " gb "]), ["US", "GB"])

    def test_outside_hours_and_meeting_markers(self):
        tokyo = datetime(2026, 11, 27, 6, 30)
        self.assertTrue(ps.outside_usual_hours(tokyo))
        self.assertFalse(ps.outside_usual_hours(tokyo.replace(hour=9)))
        self.assertTrue(ps.outside_usual_hours(tokyo.replace(hour=22)))
        self.assertTrue(ps.looks_like_meeting("Call with Sam", "", ""))
        self.assertTrue(ps.looks_like_meeting("Catch up", "https://zoom.us/j/1", ""))
        self.assertFalse(ps.looks_like_meeting("Gym", "Gold's", "legs"))

    def test_render_holiday_line_is_fixed_template(self):
        facts = {
            "start": THANKSGIVING_10AM_NY.isoformat(),
            "tz": "Asia/Tokyo",
            "country": "US",
            "holiday": "Thanksgiving Day",
        }
        line = ps.render_line("calendar_holiday", facts)
        self.assertEqual(
            line,
            "Your Fri 27 Nov 00:00 meeting (your time) falls on Thanksgiving Day in the US. "
            "Worth checking the time still works for everyone.",
        )

    def test_delivery_window_uses_heartbeat_or_fallback(self):
        class T:
            heartbeat_enabled = True
            heartbeat_start_hour = 22
            heartbeat_window_hours = 4

        self.assertTrue(ps.in_delivery_window(T, datetime(2026, 1, 1, 1, 0)))
        self.assertFalse(ps.in_delivery_window(T, datetime(2026, 1, 1, 9, 0)))
        T.heartbeat_enabled = False
        self.assertTrue(ps.in_delivery_window(T, datetime(2026, 1, 1, 9, 0)))
        self.assertFalse(ps.in_delivery_window(T, datetime(2026, 1, 1, 21, 0)))


def _event(tenant, *, title="Call with Sam", start=THANKSGIVING_10AM_NY, minutes=30, seed="e1", **kw):
    return MirrorEvent.objects.create(
        tenant=tenant,
        source_key=f"key-{seed}",
        time_kind="zoned",
        zoned_start_at=start,
        zoned_end_at=start + timedelta(minutes=minutes),
        tz_id="Asia/Tokyo",
        title=title,
        active=True,
        content_hash=f"hash-{seed}",
        first_seen_generation=1,
        last_seen_generation=1,
        **kw,
    )


@override_settings(PROACTIVE_SIGNALS_TENANT_IDS="*")
class DetectionTests(TestCase):
    def setUp(self):
        self.tenant = _ready_tenant(930001, timezone_name="Asia/Tokyo")
        self.tenant.proactive_signals_enabled = True
        self.tenant.work_countries = ["US"]
        self.tenant.save(update_fields=["proactive_signals_enabled", "work_countries"])

    def test_thanksgiving_meeting_creates_one_holiday_signal_idempotently(self):
        event = _event(self.tenant)
        first = ps.detect_calendar_signals(str(self.tenant.id), [str(event.id)], now=BEFORE)
        again = ps.detect_calendar_signals(str(self.tenant.id), [str(event.id)], now=BEFORE)
        # 10:00 New York = 00:00 Tokyo: a holiday clash AND outside the user's usual hours.
        self.assertEqual(first["created"], 2)
        self.assertEqual(again["created"], 0)
        self.assertEqual(
            set(ProactiveSignal.objects.filter(tenant=self.tenant).values_list("kind", flat=True)),
            {"calendar_holiday", "calendar_outside_hours"},
        )
        signal = ProactiveSignal.objects.get(tenant=self.tenant, kind="calendar_holiday")
        self.assertEqual(signal.kind, "calendar_holiday")
        self.assertEqual(signal.facts["country"], "US")
        self.assertEqual(signal.expires_at, event.zoned_start_at)
        self.assertNotIn("Sam", str(signal.facts))  # no titles at rest

    def test_personal_block_past_event_and_far_future_are_ignored(self):
        gym = _event(self.tenant, title="Gym", seed="gym")
        past = _event(self.tenant, start=BEFORE - timedelta(days=1), seed="past")
        far = _event(self.tenant, start=BEFORE + timedelta(days=90), seed="far")
        result = ps.detect_calendar_signals(str(self.tenant.id), [str(gym.id), str(past.id), str(far.id)], now=BEFORE)
        self.assertEqual(result["created"], 0)

    def test_outside_hours_and_overlap(self):
        start = datetime(2026, 11, 24, 21, 30, tzinfo=UTC)  # 06:30 Tokyo
        early = _event(self.tenant, title="Sync with team", start=start, minutes=60, seed="early")
        _event(self.tenant, title="Standup call", start=start + timedelta(minutes=30), minutes=30, seed="other")
        # Same meeting copied into a second calendar: not an overlap by itself.
        _event(self.tenant, title="Sync with team", start=start, minutes=60, seed="copy")
        ps.detect_calendar_signals(str(self.tenant.id), [str(early.id)], now=BEFORE)
        kinds = set(ProactiveSignal.objects.filter(event=early).values_list("kind", flat=True))
        self.assertEqual(kinds, {"calendar_outside_hours", "calendar_overlap"})

    def test_same_slot_different_meeting_is_an_overlap(self):
        start = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)  # 10:00 Tokyo
        mine = _event(self.tenant, title="Call with Sam", start=start, minutes=60, seed="mine")
        _event(self.tenant, title="Interview call", start=start, minutes=60, seed="theirs")
        ps.detect_calendar_signals(str(self.tenant.id), [str(mine.id)], now=BEFORE)
        self.assertTrue(ProactiveSignal.objects.filter(event=mine, kind="calendar_overlap").exists())

    def test_disabled_user_or_gate_creates_nothing(self):
        event = _event(self.tenant)
        self.tenant.proactive_signals_enabled = False
        self.tenant.save(update_fields=["proactive_signals_enabled"])
        self.assertEqual(
            ps.detect_calendar_signals(str(self.tenant.id), [str(event.id)], now=BEFORE)["reason"], "disabled"
        )
        self.tenant.proactive_signals_enabled = True
        self.tenant.save(update_fields=["proactive_signals_enabled"])
        with override_settings(PROACTIVE_SIGNALS_TENANT_IDS=""):
            self.assertEqual(ps.detect_calendar_signals(str(self.tenant.id), [str(event.id)], now=BEFORE)["created"], 0)


@override_settings(PROACTIVE_SIGNALS_TENANT_IDS="*")
class QueueSuppressionTests(TestCase):
    def setUp(self):
        self.tenant = _ready_tenant(930002)
        self.tenant.proactive_signals_enabled = True
        self.tenant.save(update_fields=["proactive_signals_enabled"])
        self.window_end = timezone.now() + timedelta(days=180)

    def _queue(self, changes, *, first_sync=False):
        with patch("apps.cron.publish.publish_task") as publish, self.captureOnCommitCallbacks(execute=True):
            ps.queue_calendar_detection(
                self.tenant, changes, previous_window_end=self.window_end, first_sync=first_sync
            )
        return publish

    def test_publishes_new_and_moved_after_commit(self):
        soon = timezone.now() + timedelta(days=3)
        publish = self._queue([("a", "new", soon), ("b", "moved", soon)])
        publish.assert_called_once_with("detect_calendar_signals", str(self.tenant.id), ["a", "b"])

    def test_first_sync_roll_in_and_bulk_are_suppressed(self):
        soon = timezone.now() + timedelta(days=3)
        self._queue([("a", "new", soon)], first_sync=True).assert_not_called()
        roll_in = self.window_end + timedelta(days=1)
        self._queue([("a", "new", roll_in)]).assert_not_called()
        bulk = [(str(i), "new", soon) for i in range(ps.BULK_CHANGE_LIMIT + 1)]
        self._queue(bulk).assert_not_called()

    def test_not_opted_in_never_publishes(self):
        self.tenant.proactive_signals_enabled = False
        soon = timezone.now() + timedelta(days=3)
        self._queue([("a", "new", soon)]).assert_not_called()


@override_settings(PROACTIVE_SIGNALS_TENANT_IDS="*")
class CommitHookTests(DatebookAPIMixin, TestCase):
    """End to end through the real sync endpoints: changes are reported to signals."""

    chat_id = 930003

    def setUp(self):
        super().setUp()
        self.tenant.proactive_signals_enabled = True
        self.tenant.save(update_fields=["proactive_signals_enabled"])

    def _sync(self, run_name, items):
        opened = self.open_run(run_name)
        page = self.stage_page(opened.data["run_id"], events=items)
        self.assertEqual(page.status_code, 200, page.data)
        with patch("apps.router.proactive_signals.queue_calendar_detection") as queue:
            committed = self.commit(opened.data["run_id"], events=self.manifest(items))
        self.assertEqual(committed.status_code, 200, committed.data)
        return queue.call_args

    def test_first_sync_then_new_then_moved(self):
        start = timezone.now().astimezone(UTC).replace(microsecond=0) + timedelta(days=2)
        base = _zoned_event("base", title="Planning", start=start)
        call = self._sync("run-1", [base])
        self.assertTrue(call.kwargs["first_sync"])

        new = _zoned_event("new", title="Call with Sam", start=start + timedelta(hours=3))
        call = self._sync("run-2", [base, new])
        self.assertFalse(call.kwargs["first_sync"])
        changes = call.args[1]
        new_row = MirrorEvent.objects.get(source_key=new["source_key"])
        self.assertEqual([(c[0], c[1]) for c in changes], [(str(new_row.id), "new")])

        moved = _zoned_event("new", title="Call with Sam", start=start + timedelta(hours=5))
        call = self._sync("run-3", [base, moved])
        self.assertEqual([(c[0], c[1]) for c in call.args[1]], [(str(new_row.id), "moved")])

        call = self._sync("run-4", [base, moved])
        self.assertEqual(call.args[1], [])

    def test_full_snapshot_resync_counts_as_first_sync(self):
        start = timezone.now().astimezone(UTC).replace(microsecond=0) + timedelta(days=2)
        base = _zoned_event("base", title="Planning", start=start)
        self._sync("run-1", [base])
        from apps.datebook.models import DatebookGateway

        DatebookGateway.objects.filter(tenant=self.tenant).update(events_full_snapshot_required=True)
        call = self._sync("run-2", [base])
        self.assertTrue(call.kwargs["first_sync"])


@override_settings(PROACTIVE_SIGNALS_TENANT_IDS="*")
class DeliveryTests(TestCase):
    def setUp(self):
        self.tenant = _ready_tenant(930004, timezone_name="Asia/Tokyo")
        self.tenant.proactive_signals_enabled = True
        self.tenant.heartbeat_enabled = True
        self.tenant.heartbeat_start_hour = 6
        self.tenant.heartbeat_window_hours = 6
        self.tenant.save(
            update_fields=[
                "proactive_signals_enabled",
                "heartbeat_enabled",
                "heartbeat_start_hour",
                "heartbeat_window_hours",
            ]
        )
        # 2026-11-20 08:00 Tokyo, inside the 06:00-12:00 window.
        self.in_window = datetime(2026, 11, 19, 23, 0, tzinfo=UTC)
        self.event = _event(self.tenant)

    def _signal(self, key, *, expires=THANKSGIVING_10AM_NY, kind="calendar_holiday", event=None):
        return ProactiveSignal.objects.create(
            tenant=self.tenant,
            kind=kind,
            event=event or self.event,
            dedup_key=key,
            expires_at=expires,
            facts={
                "start": THANKSGIVING_10AM_NY.isoformat(),
                "tz": "Asia/Tokyo",
                "country": "US",
                "holiday": "Thanksgiving Day",
            },
        )

    def _deliver(self, now):
        with patch("apps.router.cron_delivery.resolve_user_channel", return_value="app"):
            return ps.deliver_proactive_signals(now=now)

    def test_sends_one_message_in_window_and_marks_sent(self):
        self._signal("a")
        summary = self._deliver(self.in_window)
        self.assertEqual(summary["sent"], 1)
        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(row.job_name, ps.JOB_NAME)
        self.assertTrue(row.message_text.startswith("Heads up: Your Fri 27 Nov 00:00 meeting"))
        self.assertEqual(ProactiveSignal.objects.get(dedup_key="a").status, "sent")

    def test_outside_window_holds(self):
        self._signal("a")
        summary = self._deliver(self.in_window + timedelta(hours=8))  # 16:00 Tokyo
        self.assertEqual(summary["held"], 1)
        self.assertEqual(ProactiveSignal.objects.get(dedup_key="a").status, "pending")

    def test_daily_cap_of_three(self):
        for key in "abcde":
            self._signal(key)
        self.assertEqual(self._deliver(self.in_window)["sent"], 3)
        self.assertEqual(self._deliver(self.in_window + timedelta(minutes=15))["sent"], 0)
        self.assertEqual(ProactiveSignal.objects.filter(status="pending").count(), 2)
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=self.tenant).count(), 1)
        # Next local day the rest go out.
        self.assertEqual(self._deliver(self.in_window + timedelta(days=1))["sent"], 2)

    def test_expired_and_disabled_and_wrong_channel(self):
        self._signal("old", expires=self.in_window - timedelta(minutes=1))
        self.assertEqual(self._deliver(self.in_window)["expired"], 1)
        self._signal("b")
        with patch("apps.router.cron_delivery.resolve_user_channel", return_value="telegram"):
            ps.deliver_proactive_signals(now=self.in_window)
        self.assertEqual(ProactiveSignal.objects.get(dedup_key="b").outcome_reason, "channel_unsupported")
        self._signal("c")
        with override_settings(PROACTIVE_SIGNALS_TENANT_IDS=""):
            self._deliver(self.in_window)
        self.assertEqual(ProactiveSignal.objects.get(dedup_key="c").status, "dropped")

    def test_moved_or_deleted_event_drops_signal(self):
        self._signal("a")
        self.event.zoned_start_at = THANKSGIVING_10AM_NY + timedelta(hours=2)
        self.event.zoned_end_at = self.event.zoned_start_at + timedelta(minutes=30)
        self.event.save()
        self.assertEqual(self._deliver(self.in_window)["sent"], 0)
        self.assertEqual(ProactiveSignal.objects.get(dedup_key="a").outcome_reason, "event_changed")
        other = _event(self.tenant, seed="gone")
        self._signal("b", event=other)
        other.active = False
        other.save()
        self._deliver(self.in_window)
        self.assertEqual(ProactiveSignal.objects.get(dedup_key="b").outcome_reason, "event_changed")
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=self.tenant).exists())

    def test_stale_claim_is_recovered_and_sent_once(self):
        signal = self._signal("a")
        ProactiveSignal.objects.filter(pk=signal.pk).update(
            status="sending", claimed_at=self.in_window - timedelta(minutes=30)
        )
        self.assertEqual(self._deliver(self.in_window)["sent"], 1)
        self.assertEqual(self._deliver(self.in_window + timedelta(minutes=15))["sent"], 0)
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=self.tenant).count(), 1)

    def test_multi_line_message_has_no_bullets(self):
        self._signal("a")
        self._signal("b", kind="calendar_outside_hours")
        self._deliver(self.in_window)
        text = ProactiveOutbound.objects.get(tenant=self.tenant).message_text
        self.assertTrue(text.startswith("Heads up on your calendar:\n"))
        self.assertNotIn("•", text)

    def test_failed_record_returns_signal_to_pending(self):
        self._signal("a")
        with patch("apps.router.proactive_context.record_proactive_outbound", return_value=None):
            self._deliver(self.in_window)
        signal = ProactiveSignal.objects.get(dedup_key="a")
        self.assertEqual(signal.status, "pending")
        self.assertIsNone(signal.claimed_at)


@override_settings(PROACTIVE_SIGNALS_TENANT_IDS="")
class SettingsApiTests(TestCase):
    def setUp(self):
        self.tenant = _ready_tenant(930005)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def test_get_and_patch(self):
        got = self.client.get("/api/v1/tenants/settings/proactive/")
        self.assertEqual(got.status_code, 200, got.content)
        self.assertFalse(got.data["enabled"])
        self.assertFalse(got.data["available"])
        self.assertIn({"code": "US", "label": "the US"}, got.data["supported_countries"])
        patched = self.client.patch(
            "/api/v1/tenants/settings/proactive/", {"enabled": True, "work_countries": ["us", "gb"]}, format="json"
        )
        self.assertEqual(patched.status_code, 200, patched.content)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.proactive_signals_enabled)
        self.assertEqual(self.tenant.work_countries, ["US", "GB"])

    def test_rejects_bad_input(self):
        bad = self.client.patch("/api/v1/tenants/settings/proactive/", {"work_countries": ["ZZ"]}, format="json")
        self.assertEqual(bad.status_code, 400)
        bad = self.client.patch("/api/v1/tenants/settings/proactive/", {"enabled": "yes"}, format="json")
        self.assertEqual(bad.status_code, 400)
