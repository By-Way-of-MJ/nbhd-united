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

    def save_detail(self, detail, detector, **updates):
        serializer = WorkoutSerializer(
            self.workout,
            data={"detail_json": deepcopy(detail), **updates},
            partial=True,
            context={"tenant": self.tenant},
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
        self.assertEqual(
            [call.args[0] for call in detector.call_args_list],
            [exercise["name"] for exercise in detail["exercises"]],
        )

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

    def test_legacy_machine_values_save_and_are_scanned_like_origin_main(self):
        # The serializer is byte-identical to origin/main. Disabling exclusions
        # replays its old authoring traversal for the same stored legacy row.
        cases = (
            (("role",), "legacy-primary"),
            (("catalog_ref", "slug"), "retired-exercise-v0"),
            (("catalog_ref", "matched_by"), "legacy-exact"),
            (("sets", 0, "type"), "legacy-weighted"),
        )
        for container in ("exercises", "skills"):
            for suffix, legacy in cases:
                for invalid in ("Alice Morgan", legacy):
                    with self.subTest(container=container, suffix=suffix, value=invalid):
                        detail = live_detail()
                        detail[container] = detail.pop("exercises")
                        node = detail[container][0]
                        for part in suffix[:-1]:
                            node = node[part]
                        node[suffix[-1]] = invalid
                        self.workout.detail_json = deepcopy(detail)
                        self.workout.save(update_fields=["detail_json"])
                        detector = Mock(return_value=[])
                        with patch("apps.pii.store_registry.PlaceholderStore.nested_json_exclusions", return_value=()):
                            baseline = self.save_detail(detail, detector, status="done")
                        expected = deepcopy(baseline.detail_json)
                        receipt = deepcopy(baseline.pii_receipts)
                        self.assertIn(invalid, [call.args[0] for call in detector.call_args_list])
                        detector.reset_mock()
                        saved = self.save_detail(detail, detector, status="done")
                        self.assertEqual(saved.status, "done")
                        self.assertEqual(saved.detail_json, expected)
                        self.assertEqual(saved.detail_json, detail)
                        self.assertEqual(saved.pii_receipts, receipt)
                        self.assertIn(invalid, [call.args[0] for call in detector.call_args_list])
                        self.assertEqual(detector.call_count, 6)

    def test_unknown_catalog_values_also_save_when_newly_supplied(self):
        for key in ("slug", "matched_by"):
            for invalid in ("Alice Morgan", "unknown-v0"):
                with self.subTest(key=key, value=invalid):
                    self.workout.detail_json = live_detail()
                    detail = live_detail()
                    detail["exercises"][0]["catalog_ref"][key] = invalid
                    detector = Mock(return_value=[])
                    saved = self.save_detail(detail, detector)
                    self.assertEqual(saved.detail_json, detail)
                    self.assertIn(invalid, [call.args[0] for call in detector.call_args_list])

    def test_existing_logged_timestamp_errors_remain_visible_with_unknown_slug(self):
        # origin/main already rejects these, even on otherwise unchanged rows.
        # Removing the new guards must not weaken that contract or mask its errors.
        for container in ("exercises", "skills"):
            for invalid in ("Alice Morgan", "2026-10-05T21:00:00+09:00"):
                with self.subTest(container=container, value=invalid):
                    detail = live_detail()
                    detail[container] = detail.pop("exercises")
                    detail[container][0]["catalog_ref"]["slug"] = "unknown-v0"
                    detail[container][0]["sets"][0]["logged"]["at"] = invalid
                    self.workout.detail_json = deepcopy(detail)
                    serializer = WorkoutSerializer(self.workout, data={"detail_json": detail}, partial=True)
                    self.assertFalse(serializer.is_valid())
                    self.assertIn("logged.at", str(serializer.errors))
                    self.assertNotIn("catalog_ref", str(serializer.errors))

    def test_legacy_invalid_timestamps_are_scanned_when_authoring_without_serializer(self):
        from apps.fuel.authoring import author_store_fields

        for container in ("exercises", "skills"):
            for invalid in ("Alice Morgan", "2026-10-05T21:00:00+09:00"):
                with self.subTest(container=container, value=invalid):
                    detail = live_detail()
                    detail[container] = detail.pop("exercises")
                    detail[container][0]["sets"][0]["logged"]["at"] = invalid
                    detector = Mock(return_value=[])
                    with (
                        patch("apps.pii.engine.get_pii_pipeline", return_value=detector),
                        patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
                    ):
                        authored, _ = author_store_fields(
                            self.tenant,
                            {"detail_json": detail},
                            model_label="fuel.Workout",
                            seam="test.legacy-workout",
                            writer="owner",
                        )
                    self.assertEqual(authored["detail_json"], detail)
                    self.assertIn(invalid, [call.args[0] for call in detector.call_args_list])
                    self.assertEqual(detector.call_count, 6)

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

    def test_version_change_restarts_partial_repair_but_never_clean_rows(self):
        from apps.pii.repair_sweep import _json_digest, repair_tenant

        self.workout.pii_receipts = {
            "detail_json": {
                "state": "unconfirmed",
                "reason": "repair-batch-partial",
                "repair_progress": {
                    "cursor": 4,
                    "source_digest": _json_digest(self.workout.detail_json),
                    "traversal_version": "45134d543a8f7514:logged-actuals-v1",
                    "aggregate": {"state": "placeholder", "writer": "background", "redactions": []},
                },
            }
        }
        self.workout.save(update_fields=["pii_receipts"])
        detector = Mock(return_value=[])
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            # The hourly sweep grants at most four text leaves per tenant.
            first = repair_tenant(self.tenant, max_texts=4, alert=False)
            self.assertEqual(first["texts_authored"], 4)
            self.assertEqual(detector.call_count, 8)
            second = repair_tenant(self.tenant, max_texts=4, alert=False)
            self.assertEqual(second["texts_authored"], 1)
            self.assertEqual(detector.call_count, 10)
            self.workout.refresh_from_db()
            self.assertEqual(self.workout.pii_receipts["detail_json"]["state"], "placeholder")
            with patch("apps.pii.repair_sweep.CARDIO_TRAVERSAL_VERSION", "another-version"):
                third = repair_tenant(self.tenant, max_texts=4, alert=False)
            self.assertEqual(third["rows_seen"], 0)
            self.assertEqual(detector.call_count, 10)
