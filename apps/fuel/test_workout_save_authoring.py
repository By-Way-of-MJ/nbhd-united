"""Synthetic live-workout saves exercise the real serializer and PII author."""

from copy import deepcopy
from time import perf_counter, sleep
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase

from apps.fuel.models import Workout
from apps.fuel.serializers import WorkoutSerializer
from apps.tenants.models import Tenant, User


def live_detail():
    exercises = []
    for index, (name, slug, count) in enumerate(
        (
            ("Bench Press", "bench-press", 3),
            ("Back Squat", "squat", 2),
            ("Deadlift", "deadlift", 2),
            ("Hammer Curl", "hammer-curl", 2),
            ("Overhead Press", "overhead-press", 2),
        )
    ):
        exercises.append(
            {
                "name": name,
                "role": "primary" if index < 3 else "accessory",
                "catalog_ref": {"slug": slug, "version": 1, "matched_by": "canonical"},
                "sets": [
                    {
                        "type": "weighted_reps",
                        "reps": 8,
                        "weight": 40,
                        "logged": {"reps": 8, "weight": 42.5, "at": f"2026-10-05T21:{index * 3 + j:02}:00Z"},
                    }
                    for j in range(count)
                ],
            }
        )
    return {"exercises": exercises}


class WorkoutSaveAuthoringTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            user=User.objects.create_user(username="workout-save", password="x"),
            layer1_placeholder_writes=True,
        )
        self.workout = Workout.objects.create(
            tenant=self.tenant, date="2026-10-05", category="strength", status="planned", detail_json=live_detail()
        )

    def save_detail(self, detail, detector):
        serializer = WorkoutSerializer(
            self.workout, data={"detail_json": deepcopy(detail)}, partial=True, context={"tenant": self.tenant}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            return serializer.save()

    def test_live_save_detector_call_budget(self):
        detector = Mock(return_value=[])
        detail = live_detail()
        detail["exercises"][0]["sets"][0]["logged"]["weight"] = 45
        # Replay the original traversal too: this strength payload has no
        # cardio exclusions. author_text and receipt folding are unchanged.
        with patch("apps.pii.store_registry.PlaceholderStore.nested_json_exclusions", return_value=()):
            baseline = self.save_detail(detail, detector)
        self.assertEqual(detector.call_count, 42)
        baseline_receipt = deepcopy(baseline.pii_receipts)
        detector.reset_mock()
        saved = self.save_detail(detail, detector)
        self.assertEqual(saved.detail_json, detail)
        self.assertEqual(saved.pii_receipts, baseline_receipt)
        self.assertEqual(detector.call_count, 5)  # origin/main: 42

    def test_free_text_and_receipt_match_baseline(self):
        # Pin the origin/main result before changing exclusions. This exercises
        # detection and binding creation, not a mocked author_text result.
        def detect(text):
            start = text.find("Alice Morgan")
            return (
                [{"entity_group": "FIRSTNAME", "start": start, "end": start + 12, "score": 0.99}] if start >= 0 else []
            )

        detail = live_detail()
        detail["exercises"][0]["name"] = "Alice Morgan stretch"
        detail["notes"] = "Train with Alice Morgan"
        detail["exercises"][1]["cues"] = "Ask Alice Morgan"
        detail["blocks"] = [{"label": "Alice Morgan warmup"}]
        saved = self.save_detail(detail, Mock(side_effect=detect))
        expected = deepcopy(detail)
        expected["exercises"][0]["name"] = "[PERSON_1] stretch"
        expected["notes"] = "Train with [PERSON_1]"
        expected["exercises"][1]["cues"] = "Ask [PERSON_1]"
        expected["blocks"][0]["label"] = "[PERSON_1] warmup"
        self.assertEqual(saved.detail_json, expected)
        self.assertEqual(
            saved.pii_receipts["detail_json"],
            {"state": "placeholder", "redactions": [{"placeholder": "[PERSON_1]"}], "writer": "owner"},
        )

    def test_excluded_leaves_reject_non_machine_input_even_if_unchanged(self):
        from apps.fuel.machine_fields import WORKOUT_MACHINE_PATHS

        cases = (
            ("role",),
            ("catalog_ref", "slug"),
            ("catalog_ref", "matched_by"),
            ("sets", 0, "type"),
            ("sets", 0, "logged", "at"),
        )
        covered = set()
        for container in ("exercises", "skills"):
            for suffix in cases:
                path = (container, 0, *suffix)
                covered.add(".".join(str(p) if p != 0 else "*" for p in path).replace(".*", "[]"))
                for invalid in ("Alice Morgan", {"text": "Alice Morgan"}, ["Alice Morgan"]):
                    for unchanged in (False, True):
                        with self.subTest(path=path, invalid=invalid, unchanged=unchanged):
                            detail = live_detail()
                            detail[container] = detail.pop("exercises")
                            node = detail
                            for part in path[:-1]:
                                node = node[part]
                            node[path[-1]] = invalid
                            self.workout.detail_json = deepcopy(detail) if unchanged else {}
                            serializer = WorkoutSerializer(self.workout, data={"detail_json": detail}, partial=True)
                            self.assertFalse(serializer.is_valid())
                            self.assertIn("detail_json", serializer.errors)
        self.assertEqual(covered, set(WORKOUT_MACHINE_PATHS))

    def test_exact_slug_and_real_utc_date_required(self):
        for suffix, bad in (
            (("catalog_ref", "slug"), "Bench Press"),
            (("catalog_ref", "slug"), "bench-press-Alice"),
            (("sets", 0, "logged", "at"), "2026-02-30T21:00:00Z"),
            (("sets", 0, "logged", "at"), "2026-10-05T21:00:00+09:00"),
        ):
            with self.subTest(suffix=suffix, bad=bad):
                detail = live_detail()
                node = detail["exercises"][0]
                for part in suffix[:-1]:
                    node = node[part]
                node[suffix[-1]] = bad
                serializer = WorkoutSerializer(self.workout, data={"detail_json": detail}, partial=True)
                self.assertFalse(serializer.is_valid())

    def test_save_non_db_budget_with_simulated_detector_latency(self):
        db_seconds = 0

        def measure_db(execute, sql, params, many, context):
            nonlocal db_seconds
            started = perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                db_seconds += perf_counter() - started

        def detect(_text):
            sleep(0.25)  # Synthetic shared-client latency, no network or model.
            return []

        detector = Mock(side_effect=detect)
        started = perf_counter()
        with connection.execute_wrapper(measure_db):
            self.save_detail(live_detail(), detector)
        non_db_seconds = perf_counter() - started - db_seconds
        self.assertEqual(detector.call_count, 5)
        self.assertLess(non_db_seconds, 2)
        print(f"Synthetic workout save: detector_calls={detector.call_count} non_db_ms={non_db_seconds * 1000:.1f}")
