from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from apps.common import degradation
from apps.common.operator_alert import send_operator_alert

HOUR = 3600
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "degradation-tests"}}


@override_settings(CACHES=LOCMEM)
class DegradationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        # A fixed "now" at the start of hour 1000; buckets 997-999 are the window.
        self.now = 1000 * HOUR + 60

    def _record(self, ok: int, fail: int, *, hours_ago: int = 1, reason: str | None = "ValidationError"):
        at = self.now - hours_ago * HOUR
        for _ in range(ok):
            degradation.record_outcome("jev", True, now=at)
        for _ in range(fail):
            degradation.record_outcome("jev", False, reason=reason, now=at)

    def test_counts_only_complete_hours_in_window(self):
        self._record(2, 3, hours_ago=1)
        self._record(1, 0, hours_ago=3)
        self._record(9, 9, hours_ago=0)  # current, incomplete hour: excluded
        self._record(9, 9, hours_ago=4)  # older than the window: excluded
        self.assertEqual(degradation.window_counts("jev", now=self.now), (3, 3, "ValidationError"))

    def test_degraded_needs_min_calls_and_ratio(self):
        self._record(0, 4)
        self.assertEqual(degradation.degraded_features(now=self.now), [])  # 4 < MIN_CALLS
        self._record(0, 1, hours_ago=2)
        self.assertEqual(degradation.degraded_features(now=self.now)[0]["fail"], 5)

    def test_healthy_ratio_is_not_degraded(self):
        self._record(6, 4)
        self.assertEqual(degradation.degraded_features(now=self.now), [])

    def test_unknown_feature_and_unsafe_reason_are_dropped(self):
        degradation.record_outcome("not-a-feature", False, now=self.now - HOUR)
        degradation.record_outcome("jev", False, reason="has spaces: user text", now=self.now - HOUR)
        self.assertEqual(degradation.window_counts("jev", now=self.now), (0, 1, ""))
        self.assertIsNone(cache.get(degradation._key("not-a-feature", 999, "fail")))

    def test_record_never_raises(self):
        with patch.object(degradation.cache, "add", side_effect=RuntimeError("redis down")):
            degradation.record_outcome("jev", True, now=self.now)

    @patch("apps.common.operator_alert.send_operator_alert", return_value="delivered")
    def test_alerts_once_then_cools_down(self, send):
        self._record(0, 6)
        first = degradation.check_degradation(now=self.now)
        self.assertEqual(first["alerted"], ["jev"])
        message = send.call_args.args[0]
        self.assertIn("jev: 6/6 calls failed (ValidationError)", message)
        second = degradation.check_degradation(now=self.now)
        self.assertEqual(second["alerted"], [])
        self.assertEqual(send.call_count, 1)

    @patch("apps.common.operator_alert.send_operator_alert", return_value="transient")
    def test_transient_send_retries_next_tick(self, send):
        self._record(0, 6)
        degradation.check_degradation(now=self.now)
        degradation.check_degradation(now=self.now)
        self.assertEqual(send.call_count, 2)

    @patch("apps.common.operator_alert.send_operator_alert")
    def test_no_alert_when_healthy(self, send):
        self._record(10, 0)
        self.assertEqual(degradation.check_degradation(now=self.now)["alerted"], [])
        send.assert_not_called()


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PLATFORM_OWNER_EMAIL="owner@example.com",
)
class OperatorAlertTests(SimpleTestCase):
    @patch("apps.cron.views._send_alert_via_pushover", return_value="delivered")
    def test_pushover_delivered_skips_email(self, _push):
        self.assertEqual(send_operator_alert("m"), "delivered")
        self.assertEqual(len(mail.outbox), 0)

    @patch("apps.cron.views._send_alert_via_pushover", return_value="undeliverable")
    def test_pushover_dead_falls_back_to_email(self, _push):
        self.assertEqual(send_operator_alert("hello", subject="S"), "delivered")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@example.com"])
        self.assertEqual(mail.outbox[0].subject, "S")

    @override_settings(PLATFORM_OWNER_EMAIL="")
    @patch("apps.cron.views._send_alert_via_pushover", return_value="timeout")
    def test_both_down_returns_pushover_status(self, _push):
        self.assertEqual(send_operator_alert("m"), "timeout")

    @patch("apps.cron.views._send_alert_via_pushover", side_effect=RuntimeError("boom"))
    def test_pushover_raising_still_emails(self, _push):
        self.assertEqual(send_operator_alert("m"), "delivered")
        self.assertEqual(len(mail.outbox), 1)
