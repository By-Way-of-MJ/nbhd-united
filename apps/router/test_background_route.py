"""Offline routing, privacy, worker delivery, and eval contracts."""

import json
from copy import deepcopy
from io import StringIO
from threading import Event
from time import monotonic
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from pydantic import TypeAdapter

from apps.common import jev
from apps.pii.redactor import RedactionOutcome
from apps.router.background_route import (
    BACKGROUND_MIN,
    BACKGROUND_ROUTING_NOTE,
    DECISIONS,
    QUESTIONS,
    BackgroundRouteResponse,
    Decision,
    route_background,
)
from apps.router.management.commands.jev_background_eval import FIXTURE, Case


def answer(choice="background", p=0.9, confidence=0.0):
    return SimpleNamespace(
        answers={
            "route": jev.ChoiceAnswer(
                type="choice",
                choice=choice,
                probabilities={choice: p, next(x for x in DECISIONS if x != choice): 1 - p},
                confidence=confidence,
            )
        }
    )


@override_settings(SUBAGENT_TENANT_IDS="*")
class BackgroundRouteTests(SimpleTestCase):
    def setUp(self):
        self.tenant = SimpleNamespace(
            id=uuid4(),
            model_tier="starter",
            pii_entity_map={"PERSON_1": "private name"},
            pii_type_counters={"PERSON": 1},
            pii_denylist={},
        )
        self.redact = self.enterContext(
            patch(
                "apps.pii.ephemeral.redact_texts_ephemeral_checked",
                return_value=[RedactionOutcome("[PERSON_1] research", True, "redacted")],
            )
        )
        self.decide = self.enterContext(patch("apps.common.jev.decide", return_value=answer()))

    def test_one_typed_question_and_probability_boundary(self):
        self.assertEqual(get_args(Decision), DECISIONS)
        self.assertEqual(tuple(QUESTIONS), ("route",))
        self.assertEqual(tuple(QUESTIONS["route"].criteria), DECISIONS)
        self.assertEqual(BackgroundRouteResponse.model_json_schema()["properties"]["decision"]["enum"], list(DECISIONS))
        for p in (BACKGROUND_MIN - 0.001, BACKGROUND_MIN, BACKGROUND_MIN + 0.001):
            with self.subTest(p=p):
                self.decide.return_value = answer(p=p)
                result = route_background("private name research", self.tenant)
                self.assertEqual(result.decision, "background" if p >= BACKGROUND_MIN else "inline")
                self.assertEqual(result.reason, "ok" if p >= BACKGROUND_MIN else "low_confidence")
                self.assertEqual(result.p, p)
        self.decide.return_value = answer("inline", 0.51, 1.0)
        self.assertEqual(route_background("weather", self.tenant).decision, "inline")

    def test_redacted_state_deadline_snapshot_and_content_free_log(self):
        with self.assertLogs("apps.router.background_route", level="INFO") as logs:
            result = route_background("private name research", self.tenant)
        self.assertEqual(result.decision, "background")
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private name", logs.output[0])
        self.assertIn("decision=background p=0.900", logs.output[0])
        self.assertIn("latency_ms=", logs.output[0])
        self.decide.assert_called_once()
        self.assertEqual(self.decide.call_args.args, ({"latest_message": "[PERSON_1] research"}, QUESTIONS))
        self.assertEqual(self.redact.call_args.args[0], ["private name research"])
        snapshot = self.redact.call_args.args[1]
        self.assertIsNot(snapshot, self.tenant)
        snapshot.pii_entity_map.clear()
        self.assertTrue(self.tenant.pii_entity_map)
        self.assertEqual(self.redact.call_args.kwargs["deadline"], self.decide.call_args.kwargs["deadline"])

    def test_gate_attachments_and_invalid_inputs_do_no_work(self):
        for allowed in ("", str(uuid4())):
            with override_settings(SUBAGENT_TENANT_IDS=allowed):
                self.assertEqual(route_background("research", self.tenant).reason, "disabled")
        self.assertEqual(route_background("research", None).reason, "disabled")
        self.assertEqual(route_background("research", self.tenant, has_attachment=True).reason, "attachment")
        for text in ("", "x" * 32001, None, 123):
            self.assertEqual(route_background(text, self.tenant).reason, "invalid_input")
        self.redact.assert_not_called()
        self.decide.assert_not_called()

    def test_unconfirmed_redaction_never_reaches_jev(self):
        for outcomes in (
            [],
            [RedactionOutcome("private", False, "failed")],
            [RedactionOutcome("safe", True, "ok")] * 2,
        ):
            self.redact.return_value = outcomes
            result = route_background("private", self.tenant)
            self.assertEqual((result.decision, result.reason), ("inline", "redaction_unconfirmed"))
        self.redact.side_effect = RuntimeError("private")
        self.assertEqual(route_background("private", self.tenant).reason, "redaction_unconfirmed")
        self.decide.assert_not_called()

    def test_errors_malformed_answers_and_expired_deadline_fail_inline(self):
        for error in (TimeoutError("private"), jev.JevUnavailable("private")):
            self.decide.side_effect = error
            self.assertEqual(route_background("private", self.tenant).reason, "unavailable")
        self.decide.side_effect = None
        self.decide.return_value = SimpleNamespace(answers={})
        self.assertEqual(route_background("private", self.tenant).reason, "unavailable")
        self.redact.reset_mock()
        self.decide.reset_mock()
        self.assertEqual(route_background("private", self.tenant, deadline=monotonic() - 1).reason, "unavailable")
        self.redact.assert_not_called()
        self.decide.assert_not_called()

    def test_deadline_holds_slot_until_redaction_finishes_and_no_late_jev(self):
        entered, release, finished = Event(), Event(), Event()

        def slow(*args, **kwargs):
            entered.set()
            release.wait(2)
            finished.set()
            return [RedactionOutcome("safe", True, "ok")]

        self.redact.side_effect = slow
        with patch("apps.router.background_route._BACKGROUND_SLOTS") as slots:
            slots.acquire.return_value = True
            slot_released = Event()
            slots.release.side_effect = slot_released.set
            try:
                started = monotonic()
                result = route_background("private", self.tenant, deadline=started + 0.05)
                self.assertTrue(entered.is_set())
                self.assertLess(monotonic() - started, 0.5)
                self.assertEqual(result.decision, "inline")
                slots.release.assert_not_called()
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
                self.assertTrue(slot_released.wait(1))
        self.decide.assert_not_called()

    def test_saturation_and_submit_failure_release_correctly(self):
        with patch("apps.router.background_route._BACKGROUND_SLOTS") as slots:
            slots.acquire.return_value = False
            self.assertEqual(route_background("private", self.tenant).reason, "unavailable")
            slots.release.assert_not_called()
            self.redact.assert_not_called()
            slots.acquire.return_value = True
            with patch("apps.router.background_route._BACKGROUND_WORKERS.submit", side_effect=RuntimeError):
                self.assertEqual(route_background("private", self.tenant).reason, "unavailable")
            slots.release.assert_called_once()


@override_settings(SUBAGENT_TENANT_IDS="*")
class BackgroundDrainTests(SimpleTestCase):
    def setUp(self):
        from apps.router.test_ios_chat import _ok_chat_response

        self.tenant = SimpleNamespace(id=uuid4(), pii_entity_map={})
        self.decide = self.enterContext(patch("apps.common.jev.decide", return_value=answer()))
        self.redact = self.enterContext(
            patch(
                "apps.pii.ephemeral.redact_texts_ephemeral_checked",
                side_effect=(lambda texts, *args, **kwargs: [RedactionOutcome(texts[0], True, "ok")]),
            )
        )
        self.enterContext(patch("apps.router.proactive_context.surface_proactive_context", return_value="recap\n"))
        self.enterContext(patch("apps.router.thread_recap.build_thread_recap_block", return_value=""))
        self.enterContext(patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="test"))
        self.enterContext(patch("apps.router.pending_queue.gateway_base_url", return_value="http://offline"))
        for name in ("_store_ios_turn_reply", "_record_usage_safe", "_schedule_ios_digest_refresh"):
            self.enterContext(patch("apps.router.pending_queue." + name, return_value=0))
        self.post = self.enterContext(patch("apps.router.pending_queue.httpx.post", return_value=_ok_chat_response()))

    def row(self, text="research", **flags):
        from django.utils import timezone

        return SimpleNamespace(
            user_text=text,
            channel_user_id="thread",
            created_at=timezone.now(),
            payload={"message_text": "[Chat]\n" + text, "client_msg_id": "id", **flags},
        )

    def drain(self, rows):
        from apps.router.pending_queue import _drain_ios_batch

        original = deepcopy(rows)
        _drain_ios_batch(self.tenant, rows, 10)
        self.assertEqual(rows, original)
        return self.post.call_args.kwargs["json"]["messages"][0]["content"]

    def test_note_only_on_llm_bound_text_after_annotation(self):
        with patch("apps.pii.redactor.annotate_model_context", side_effect=lambda text, _: text) as annotate:
            content = self.drain([self.row()])
        self.assertTrue(content.endswith("\n" + BACKGROUND_ROUTING_NOTE))
        self.assertNotIn(BACKGROUND_ROUTING_NOTE, annotate.call_args.args[0])
        self.assertEqual(self.decide.call_args.args[0], {"latest_message": "research"})
        self.assertNotIn(BACKGROUND_ROUTING_NOTE, self.redact.call_args.args[0][0])

    def test_inline_disabled_attachments_and_non_user_turns_have_no_note(self):
        for flag in ("is_image", "is_document"):
            self.assertNotIn(BACKGROUND_ROUTING_NOTE, self.drain([self.row(**{flag: True})]))
        row = self.row()
        del row.payload["client_msg_id"]
        self.assertNotIn(BACKGROUND_ROUTING_NOTE, self.drain([row]))
        with override_settings(SUBAGENT_TENANT_IDS=""):
            self.assertNotIn(BACKGROUND_ROUTING_NOTE, self.drain([self.row()]))
        self.decide.assert_not_called()
        self.decide.return_value = answer("inline")
        self.assertNotIn(BACKGROUND_ROUTING_NOTE, self.drain([self.row()]))

    def test_coalesced_latest_request_only_and_retry_does_not_duplicate_note(self):
        rows = [self.row("earlier"), self.row("latest research")]
        for _ in range(2):
            content = self.drain(rows)
            self.assertEqual(content.count(BACKGROUND_ROUTING_NOTE), 1)
            self.assertEqual(self.decide.call_args.args[0], {"latest_message": "latest research"})
            self.assertIn("earlier", content)


@override_settings(NBHD_INTERNAL_API_KEY="test", SUBAGENT_TENANT_IDS="*")
class BackgroundIngressTests(TestCase):
    def test_ingress_stores_original_and_does_not_run_jev(self):
        from apps.router.chat_views import enqueue_tenant_turn
        from apps.router.models import ChatThread
        from apps.router.test_ios_chat import _make_tenant, _make_user

        user = _make_user()
        tenant = _make_tenant(user)
        thread = ChatThread.objects.create(tenant=tenant, user=user, is_main=True)
        with (
            patch("apps.router.chat_views.check_budget", return_value=None),
            patch("apps.router.chat_views.enqueue_message_for_tenant") as enqueue,
            patch("apps.pii.redactor.redact_user_message_checked", return_value=RedactionOutcome("safe", True, "ok")),
            patch("apps.pii.provisional.record_provisional_sightings"),
            patch("apps.common.jev.decide") as decide,
        ):
            turn, created = enqueue_tenant_turn(
                tenant=tenant,
                user=user,
                text="private research",
                thread=thread,
                client_msg_id=str(uuid4()),
            )
        self.assertTrue(created)
        turn.refresh_from_db()
        self.assertEqual(turn.user_text, "private research")
        self.assertNotIn(BACKGROUND_ROUTING_NOTE, enqueue.call_args.kwargs["payload"]["message_text"])
        self.assertEqual(enqueue.call_args.kwargs["user_text_excerpt"], "safe")
        decide.assert_not_called()


class EvalTests(SimpleTestCase):
    def test_fixture_balance_and_languages(self):
        cases = TypeAdapter(list[Case]).validate_json(FIXTURE.read_text())
        self.assertEqual(len(cases), 30)
        self.assertEqual(sum(case.language == "en" for case in cases), 20)
        self.assertEqual(sum(case.language == "ja" for case in cases), 10)
        self.assertEqual(sum(case.expected == "background" for case in cases), 15)

    @override_settings(OPENROUTER_API_KEY="offline")
    def test_command_metrics_and_unavailable_not_counted_correct(self):
        output = StringIO()
        with (
            patch("apps.common.jev.decide", side_effect=jev.JevUnavailable("private")) as decide,
            patch(
                "apps.pii.ephemeral.redact_texts_ephemeral_checked", return_value=[RedactionOutcome("safe", True, "ok")]
            ),
        ):
            call_command("jev_background_eval", stdout=output)
        metrics = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(decide.call_count, 28)
        self.assertEqual(metrics["evaluated"], 2)
        self.assertAlmostEqual(metrics["accuracy"], 2 / 30)
        self.assertEqual(
            metrics["confusion_expected_actual"],
            {
                "background": {"background": 0, "inline": 15},
                "inline": {"background": 0, "inline": 15},
            },
        )
        self.assertIn("p50_latency_ms", metrics)
        self.assertNotIn("private", output.getvalue())
