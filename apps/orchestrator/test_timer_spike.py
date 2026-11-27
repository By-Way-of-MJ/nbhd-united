from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.orchestrator import timer_spike
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _response(code, content="OK"):
    resp = MagicMock(status_code=code)
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


@override_settings(TIMER_SPIKE_TENANT_IDS="*")
class TimerSpikeTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Spike", telegram_chat_id=940001)
        Tenant.objects.filter(pk=self.tenant.pk).update(
            container_fqdn="oc-spike.example.com", status=Tenant.Status.ACTIVE
        )
        self.tenant.refresh_from_db()
        self.clock = FakeClock()

    def _run(self, responses, *, mode="ping", hibernated=False):
        if hibernated:
            Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=timezone.now())
        calls = []

        def post(url, *, json, headers, timeout):
            calls.append((url, json, headers))
            self.clock.advance(3)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        wake = MagicMock(return_value=True, side_effect=lambda t: self.clock.advance(2) or True)
        with patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="tok"):
            result = timer_spike.run_spike(
                str(self.tenant.id), mode, post=post, wake=wake, clock=self.clock, pause=self.clock.advance
            )
        return result, calls, wake

    def test_warm_ping_isolated_session_and_timings(self):
        result, calls, wake = self._run([_response(200)])
        wake.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertEqual((result["ready_s"], result["turn_s"], result["attempts"]), (0.0, 3.0, 1))
        url, body, headers = calls[0]
        self.assertEqual(url, "https://oc-spike.example.com/v1/chat/completions")
        self.assertTrue(body["user"].startswith("timer-spike:ping:"))  # isolated session, not the chat
        self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_cold_wake_retries_until_container_answers(self):
        result, calls, wake = self._run([httpx.ConnectError("down"), _response(503), _response(200)], hibernated=True)
        wake.assert_called_once()
        self.assertTrue(result["hibernated"])
        self.assertEqual(result["attempts"], 3)
        # wake 2s + (3s post + 5s pause) x2 before the good attempt starts
        self.assertEqual(result["ready_s"], 18.0)
        self.assertEqual(result["status"], "ok")

    def test_gives_up_after_deadline(self):
        responses = [_response(503) for _ in range(200)]
        result, _, _ = self._run(responses, hibernated=True)
        self.assertEqual(result["status"], "not_ready")
        self.assertLessEqual(result["total_s"], timer_spike.DEADLINE_SECONDS + 10)

    def test_briefing_mode_uses_real_cron_prompt_with_test_label(self):
        with (
            patch("apps.orchestrator.config_generator._build_morning_briefing_prompt", return_value="PROMPT"),
            patch("apps.orchestrator.config_generator._build_cron_message", return_value="CRON:PROMPT") as build,
        ):
            _, calls, _ = self._run([_response(200)], mode="briefing")
        build.assert_called_once()
        self.assertEqual(build.call_args.args[1], "Morning Briefing")
        content = calls[0][1]["messages"][0]["content"]
        self.assertTrue(content.startswith(timer_spike.BRIEFING_PREFIX))
        self.assertTrue(content.endswith("CRON:PROMPT"))

    @override_settings(TIMER_SPIKE_TENANT_IDS="")
    def test_gate_closed_does_nothing(self):
        result, calls, wake = self._run([_response(200)])
        self.assertEqual(result["reason"], "not_allowed")
        self.assertEqual(calls, [])
        wake.assert_not_called()

    def test_task_rejects_bad_mode_and_starts_thread(self):
        self.assertFalse(timer_spike.timer_spike_task(str(self.tenant.id), "bogus")["started"])
        with patch("apps.orchestrator.timer_spike.threading.Thread") as thread:
            self.assertTrue(timer_spike.timer_spike_task(str(self.tenant.id), "ping")["started"])
        thread.return_value.start.assert_called_once()
