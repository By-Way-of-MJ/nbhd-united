"""Offline proofs of the strict Decisions transport and content-free failures."""

import copy
import json
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings
from pydantic import ValidationError

from apps.common import jev

LIVE_RESPONSE = """
{"model":"typesafe/jev-1.13-20260917","answers":{"rain":{"type":"choice","choice":"yes","probabilities":{"yes":0.99,"no":0.01},"confidence":0.98}},"usage":{"input_tokens":312,"output_tokens":31,"cost":0.000013104,"is_byok":false,"cost_details":{"upstream_inference_cost":0.000013104,"upstream_inference_prompt_cost":0.000013104,"upstream_inference_completions_cost":0}},"id":"gen-dec-1795747018-azfMsOaHHQ5HKWWnsEzE","provider":"TypeSafe"}
"""


def envelope(answers):
    return {
        "model": jev.JEV_MODEL + "-20260917",
        "id": "test",
        "provider": "TypeSafe",
        "answers": answers,
        "usage": {"input_tokens": 100, "output_tokens": 20, "cost": 0.00001},
    }


def choice_answer(criteria, choice, confidence=1.0, probabilities=None):
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities or {key: float(key == choice) for key in criteria},
    }


def score_answer(criteria, score=2.0):
    return {
        "type": "score",
        "score": score,
        "confidence": 1.0,
        "legend": {str(i): value for i, value in enumerate(criteria)},
        "probabilities": {"0": 1 - score / 2, "1": 0.0, "2": score / 2},
    }


@override_settings(OPENROUTER_API_KEY="test-only", TEST_MODE=True)
class JevTests(SimpleTestCase):
    def setUp(self):
        self.questions = {
            "choice": jev.ChoiceQuestion(instructions="Choose", criteria={"yes": "Yes", "none": "None"}),
            "score": jev.ScoreQuestion(instructions="Help?", criteria=["None", "Some", "Much"]),
            "noul": jev.NoulQuestion(instructions="Change?"),
        }
        self.data = envelope(
            {
                "choice": choice_answer(self.questions["choice"].criteria, "yes"),
                "score": score_answer(self.questions["score"].criteria),
                "noul": {"type": "noul", "noul": 0.05},
            }
        )

    def call(self, data):
        with patch("apps.common.jev._post", return_value=Mock(status_code=200, text=json.dumps(data))) as post:
            result = jev.decide({"latest_message": "[PERSON_1]"}, self.questions)
        return result, post

    def test_one_post_typed_question_schema_and_timeout(self):
        result, post = self.call(self.data)
        self.assertEqual(result.answers["score"].score, 2.0)
        self.assertEqual(result.answers["noul"].noul, 0.05)
        post.assert_called_once_with(
            jev.DECISIONS_URL,
            headers={"Authorization": "Bearer test-only"},
            json={
                "model": jev.JEV_MODEL,
                "state": {"latest_message": "[PERSON_1]"},
                "questions": {key: value.model_dump(exclude_none=True) for key, value in self.questions.items()},
                "provider": {"zdr": True, "data_collection": "deny"},
            },
            timeout=(2, 4),
            allow_redirects=False,
        )

    def test_live_response_with_new_usage_fields_parses(self):
        parsed = jev.DecisionResponse.model_validate_json(LIVE_RESPONSE)
        self.assertEqual(parsed.answers["rain"].choice, "yes")
        self.assertEqual(parsed.usage.input_tokens, 312)
        questions = {"rain": jev.ChoiceQuestion(instructions="Rain?", criteria={"yes": "Yes", "no": "No"})}
        with patch("apps.common.jev._post", return_value=Mock(status_code=200, text=LIVE_RESPONSE)):
            result = jev.decide({"latest_message": "[PERSON_1]"}, questions)
        self.assertEqual(result.answers["rain"].choice, "yes")

    def test_unknown_top_level_and_answer_fields_are_ignored(self):
        data = copy.deepcopy(self.data)
        data["unknown_top_level"] = {"raw": "ignored"}
        data["usage"].update(is_byok=False, cost_details={"upstream_inference_cost": 0.00001})
        data["answers"]["choice"]["unknown_answer_field"] = {"raw": "ignored"}
        result, _ = self.call(data)
        self.assertEqual(result.answers["choice"].choice, "yes")

    def test_question_models_still_reject_extra_keys(self):
        with self.assertRaises(ValidationError):
            jev.ChoiceQuestion(instructions="x", criteria={"yes": "Yes", "no": "No"}, raw="private sentinel")
        with self.assertRaises(ValidationError):
            jev.NoulQuestion(instructions="x", raw="private sentinel")
        with self.assertRaises(ValidationError):
            jev.ScoreQuestion(instructions="x", criteria=["None", "Some"], raw="private sentinel")

    def test_validation_failure_logs_class_and_paths_without_state(self):
        data = copy.deepcopy(self.data)
        data["answers"]["choice"]["probabilities"] = {"yes": 1.0, "none": 0.5}
        with (
            patch("apps.common.jev._post", return_value=Mock(status_code=200, text=json.dumps(data))),
            self.assertLogs("apps.common.jev", level="WARNING") as logs,
            self.assertRaises(jev.JevUnavailable),
        ):
            jev.decide("SECRET_STATE_VALUE", self.questions)
        output = "\n".join(logs.output)
        self.assertIn("ValidationError", output)
        self.assertIn("answers.choice", output)
        self.assertNotIn("SECRET_STATE_VALUE", output)

    def test_invalid_responses_fail_closed(self):
        mutations = {
            "missing": lambda d: d["answers"].pop("noul"),
            "extra answer": lambda d: d["answers"].update(extra=d["answers"]["noul"]),
            "wrong type": lambda d: d["answers"].update(choice={"type": "noul", "noul": 1.0}),
            "string float": lambda d: d["answers"]["choice"].update(confidence="1.0"),
            "bool float": lambda d: d["answers"]["noul"].update(noul=True),
            "nan": lambda d: d["answers"]["choice"]["probabilities"].update(yes=float("nan")),
            "infinity": lambda d: d["answers"]["noul"].update(noul=float("inf")),
            "sum": lambda d: d["answers"]["choice"]["probabilities"].update(none=0.5),
            "wrong option": lambda d: d["answers"]["choice"].update(choice="private sentinel"),
            "not maximum": lambda d: d["answers"]["choice"].update(choice="none"),
            "missing option": lambda d: d["answers"]["choice"]["probabilities"].pop("none"),
            "extra option": lambda d: d["answers"]["choice"]["probabilities"].update(extra=0.0),
            "score mismatch": lambda d: d["answers"]["score"].update(score=0.0),
            "score sum": lambda d: d["answers"]["score"]["probabilities"].update({"0": 0.2}),
            "legend": lambda d: d["answers"]["score"]["legend"].update({"1": "wrong"}),
            "model": lambda d: d.update(model="wrong"),
            "cost": lambda d: d["usage"].update(cost=-1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = copy.deepcopy(self.data)
                mutate(data)
                with self.assertRaisesMessage(jev.JevUnavailable, "Jev unavailable") as caught:
                    self.call(data)
                self.assertNotIn("private sentinel", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_bad_questions_never_reach_transport(self):
        for questions in ({}, {"x": {"type": "choice", "instructions": "x", "criteria": {"x": "x"}}}):
            with self.subTest(questions=questions), patch("apps.common.jev._post") as post:
                with self.assertRaises(jev.JevUnavailable):
                    jev.decide("state", questions)
                post.assert_not_called()
        with self.assertRaises(ValidationError):
            jev.NoulQuestion(instructions=12)

    def test_failures_never_retry_or_expose_content(self):
        for failure in (requests.Timeout("private sentinel"), requests.HTTPError("private sentinel"), None):
            with self.subTest(failure=type(failure).__name__), patch("apps.common.jev._post") as post:
                post.side_effect = failure
                post.return_value = Mock(status_code=200, text="private sentinel malformed JSON")
                with self.assertRaisesMessage(jev.JevUnavailable, "Jev unavailable"):
                    jev.decide("private sentinel", self.questions)
                post.assert_called_once()

    def test_redirect_and_missing_key_fail_closed(self):
        with patch("apps.common.jev._post", return_value=Mock(status_code=302)) as post:
            with self.assertRaises(jev.JevUnavailable):
                jev.decide("state", self.questions)
            post.assert_called_once()
        with override_settings(OPENROUTER_API_KEY=""), patch("apps.common.jev._post") as post:
            with self.assertRaises(jev.JevUnavailable):
                jev.decide("state", self.questions)
            post.assert_not_called()

    def test_test_mode_and_runner_block_unmocked_transport(self):
        # Patch beneath requests.post: if the guard regresses, fail without egress.
        for test_mode in (True, False):
            with override_settings(TEST_MODE=test_mode), patch("requests.sessions.Session.request") as network:
                with self.assertRaises(jev.JevUnavailable):
                    jev.decide("state", self.questions)
                network.assert_not_called()

    def test_wrapping_spies_cannot_enter_real_transport(self):
        for boundary in ("requests.post", "apps.common.jev._post"):
            original = requests.post if boundary == "requests.post" else jev._post
            with (
                self.subTest(boundary=boundary),
                patch(boundary, wraps=original),
                patch("requests.sessions.Session.request") as network,
            ):
                with self.assertRaises(jev.JevUnavailable):
                    jev.decide("private sentinel", self.questions)
                network.assert_not_called()

    @override_settings(TEST_MODE=False)
    def test_plain_unittest_without_django_marker_is_offline(self):
        with (
            patch.object(jev.sys, "argv", ["offline-harness.py"]),
            patch.dict(jev.os.environ, {}, clear=True),
            patch("requests.sessions.Session.request") as network,
        ):
            with self.assertRaises(jev.JevUnavailable):
                jev.decide("state", self.questions)
            network.assert_not_called()

    def test_shared_deadline_caps_transport_and_rejects_late_result(self):
        from time import monotonic

        deadline = monotonic() + 0.5
        with patch("apps.common.jev._post", return_value=Mock(status_code=200, text=json.dumps(self.data))) as post:
            jev.decide("state", self.questions, deadline=deadline)
            timeout = post.call_args.kwargs["timeout"]
            self.assertGreater(timeout.total, 0)
            self.assertLessEqual(timeout.total, 0.5)
            with self.assertRaises(jev.JevUnavailable):
                jev.decide("state", self.questions, deadline=monotonic() - 1)
            post.assert_called_once()
        with (
            patch("apps.common.jev.monotonic", side_effect=[1.0, 3.0]),
            patch("apps.common.jev._post", return_value=Mock(status_code=200, text=json.dumps(self.data))),
            self.assertRaises(jev.JevUnavailable),
        ):
            jev.decide("state", self.questions, deadline=2.0)
