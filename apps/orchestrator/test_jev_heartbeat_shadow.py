"""Shadow-only Jev heartbeat check: gates, redaction, state, logging, no sends."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import get_args
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings

from apps.common import jev
from apps.common.tenant_tz import tenant_today
from apps.datebook.models import MirrorEvent
from apps.journal.models import Document
from apps.orchestrator.jev_heartbeat_shadow import (
    Worth,
    jev_heartbeat_shadow_task,
    run_shadow_for_tenant,
)
from apps.pii.redactor import RedactionOutcome
from apps.tenants.models import Tenant, User

FLAG = "JEV_HEARTBEAT_SHADOW_TENANT_IDS"
EVENT_TITLE = "Dentist for Mia"
PEOPLE_TEXT = "Mia is my daughter; she loves strawberries."
OTHER_TEXT = "Prefers quiet mornings and hates telephone calls."


def answer(choice: str = "prep_upcoming", p: float = 0.82):
    probabilities = {option: (1 - p) / (len(get_args(Worth)) - 1) for option in get_args(Worth)}
    probabilities[choice] = p
    return type(
        "MockDecision",
        (),
        {
            "answers": {
                "worth": jev.ChoiceAnswer(
                    type="choice",
                    choice=choice,
                    probabilities=probabilities,
                    confidence=0.0,
                )
            }
        },
    )()


class JevHeartbeatShadowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username=f"shadow-{uuid4()}", password="x")
        self.user.timezone = "UTC"
        self.user.save(update_fields=["timezone"])
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        self.now = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0)
        self.clock = self.enterContext(
            patch("apps.orchestrator.jev_heartbeat_shadow.timezone.now", return_value=self.now)
        )
        self.decide = self.enterContext(patch("apps.common.jev.decide", return_value=answer()))
        self.redact = self.enterContext(
            patch(
                "apps.pii.ephemeral.redact_texts_ephemeral_checked",
                side_effect=lambda texts, *args, **kwargs: [RedactionOutcome(text, True, "ok") for text in texts],
            )
        )

    def _state(self):
        day = tenant_today(self.tenant) + timedelta(days=1)
        MirrorEvent.objects.create(
            tenant=self.tenant,
            source_key=f"event-{uuid4()}",
            content_hash="a" * 64,
            time_kind="all_day",
            all_day_start_date=day,
            all_day_end_date_exclusive=day + timedelta(days=1),
            title=EVENT_TITLE,
            location="Clinic",
            notes="Bring the form",
            active=True,
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.MEMORY,
            slug="long-term",
            title="Memory",
            markdown=(f"# Memory\n\n## People & Context\n{PEOPLE_TEXT}\n\n## Preferences\n{OTHER_TEXT}\n"),
        )

    def test_empty_flag_does_nothing(self):
        with override_settings(**{FLAG: ""}):
            self.assertEqual(jev_heartbeat_shadow_task(), {"tenants": 0})
        self.decide.assert_not_called()
        self.redact.assert_not_called()

    def test_outside_local_hours_skips_jev(self):
        self.clock.return_value = self.now.replace(hour=3)
        with override_settings(**{FLAG: "*"}):
            result = run_shadow_for_tenant(self.tenant)
        self.assertEqual(result.reason, "outside_hours")
        self.decide.assert_not_called()

    def test_unconfirmed_redaction_skips_jev(self):
        self._state()
        self.redact.side_effect = lambda texts, *args, **kwargs: [
            RedactionOutcome(text, False, "failed") for text in texts
        ]
        result = run_shadow_for_tenant(self.tenant)
        self.assertEqual(result.reason, "redaction_unconfirmed")
        self.decide.assert_not_called()

    def test_happy_path_uses_redacted_state_and_mocked_answer(self):
        self._state()
        result = run_shadow_for_tenant(self.tenant)
        self.assertEqual(result.reason, "ok")
        self.assertEqual(result.decision, "prep_upcoming")
        self.assertEqual(result.p, 0.82)
        self.assertEqual(result.events, 1)
        self.assertGreater(result.people_chars, 0)

        state = self.decide.call_args.args[0]
        self.assertEqual(
            set(state),
            {"local_time", "calendar", "commitments", "people"},
        )
        self.assertIn(EVENT_TITLE, state["calendar"])
        self.assertIn(PEOPLE_TEXT, state["people"])
        self.assertNotIn(OTHER_TEXT, state["people"])
        self.assertNotIn(OTHER_TEXT, state["calendar"])
        self.assertNotIn(OTHER_TEXT, state["commitments"])

    def test_calendar_reads_today_and_tomorrow(self):
        self._state()
        with patch("apps.datebook.agenda.agenda_items", return_value=([], False)) as agenda:
            result = run_shadow_for_tenant(self.tenant)
        self.assertEqual(result.reason, "ok")
        agenda.assert_called_once()
        self.assertEqual(agenda.call_args.kwargs["days_ahead"], 1)

    def test_in_heartbeat_window_matches_cron_hours(self):
        self.tenant.heartbeat_enabled = True
        self.tenant.heartbeat_start_hour = 8
        self.tenant.heartbeat_window_hours = 6
        self.tenant.save(update_fields=["heartbeat_enabled", "heartbeat_start_hour", "heartbeat_window_hours"])
        self._state()

        self.assertTrue(run_shadow_for_tenant(self.tenant).in_heartbeat_window)

        self.clock.return_value = self.now.replace(hour=15)
        self.assertFalse(run_shadow_for_tenant(self.tenant).in_heartbeat_window)

        self.tenant.heartbeat_enabled = False
        self.tenant.save(update_fields=["heartbeat_enabled"])
        self.clock.return_value = self.now
        self.assertFalse(run_shadow_for_tenant(self.tenant).in_heartbeat_window)

    def test_decide_failure_is_unavailable_and_never_escapes_task(self):
        self._state()
        self.decide.side_effect = RuntimeError("private")
        self.assertEqual(run_shadow_for_tenant(self.tenant).reason, "unavailable")
        with override_settings(**{FLAG: "*"}):
            result = jev_heartbeat_shadow_task()
        self.assertEqual(result["tenants"], 1)
        self.assertEqual(result["unavailable"], 1)

    def test_task_never_sends(self):
        self._state()
        with (
            override_settings(**{FLAG: "*"}),
            patch("apps.router.system_notify.send_system_notification") as send,
        ):
            result = jev_heartbeat_shadow_task()
        self.assertEqual(result["ok"], 1)
        self.decide.assert_called_once()
        send.assert_not_called()

    def test_log_line_is_content_free(self):
        self._state()
        with (
            override_settings(**{FLAG: "*"}),
            self.assertLogs("apps.orchestrator.jev_heartbeat_shadow", level="INFO") as logs,
        ):
            jev_heartbeat_shadow_task()
        output = "\n".join(logs.output)
        self.assertIn("jev_heartbeat_shadow tenant=", output)
        self.assertIn("in_hb_window=1", output)
        self.assertIn("reason=ok", output)
        self.assertNotIn(EVENT_TITLE, output)
        self.assertNotIn(PEOPLE_TEXT, output)
        self.assertNotIn(OTHER_TEXT, output)
