"""Workout exclusions remain value- and path-guarded outside ingress."""

from copy import deepcopy

from django.test import SimpleTestCase

from apps.fuel.test_workout_save_authoring import live_detail
from apps.pii.store_registry import registered_store, rewrite_json_path


class WorkoutMachinePathTests(SimpleTestCase):
    def rewrite(self, value):
        return rewrite_json_path(
            value,
            ("**",),
            lambda text: f"SCANNED({text})",
            exclude_paths=registered_store("fuel.Workout").nested_json_exclusions("detail_json"),
        )[0]

    def test_only_valid_machine_leaves_are_exempt_and_input_is_unchanged(self):
        for container in ("exercises", "skills"):
            with self.subTest(container=container):
                value = live_detail()
                value[container] = value.pop("exercises")
                before = deepcopy(value)
                expected = deepcopy(value)
                for exercise in expected[container]:
                    exercise["name"] = f"SCANNED({exercise['name']})"
                self.assertEqual(self.rewrite(value), expected)
                self.assertEqual(value, before)

    def test_legacy_invalid_values_and_objects_cannot_hide_text(self):
        for container in ("exercises", "skills"):
            for suffix in (
                ("role",),
                ("catalog_ref", "slug"),
                ("catalog_ref", "matched_by"),
                ("sets", 0, "type"),
                ("sets", 0, "logged", "at"),
            ):
                for invalid in ("Alice Morgan", {"nested": "Alice Morgan"}, ["Alice Morgan"]):
                    with self.subTest(container=container, suffix=suffix, invalid=invalid):
                        value = live_detail()
                        value[container] = value.pop("exercises")
                        node = value[container][0]
                        for part in suffix[:-1]:
                            node = node[part]
                        node[suffix[-1]] = invalid
                        rewritten = self.rewrite(value)[container][0]
                        for part in suffix:
                            rewritten = rewritten[part]
                        self.assertIn("SCANNED(Alice Morgan)", str(rewritten))

    def test_machine_vocabulary_at_free_text_and_extension_paths_is_scanned(self):
        value = live_detail()
        exercise = value["exercises"][0]
        exercise["name"] = "canonical"
        exercise["notes"] = "weighted_reps"
        exercise["catalog_ref"]["extra"] = "Alice Morgan"
        exercise["sets"][0]["notes"] = "primary"
        value["notes"] = "bench-press"
        value["blocks"] = [{"label": "accessory"}]
        rewritten = self.rewrite(value)
        exercise = rewritten["exercises"][0]
        self.assertEqual(exercise["name"], "SCANNED(canonical)")
        self.assertEqual(exercise["notes"], "SCANNED(weighted_reps)")
        self.assertEqual(exercise["catalog_ref"]["extra"], "SCANNED(Alice Morgan)")
        self.assertEqual(exercise["sets"][0]["notes"], "SCANNED(primary)")
        self.assertEqual(rewritten["notes"], "SCANNED(bench-press)")
        self.assertEqual(rewritten["blocks"][0]["label"], "SCANNED(accessory)")

    def test_plan_and_template_do_not_inherit_unvalidated_exclusions(self):
        for label, field in (("fuel.WorkoutPlan", "schedule_json"), ("fuel.WorkoutTemplate", "detail_json")):
            value = live_detail()
            if field == "schedule_json":
                value = {"0": {"detail_json": value}}
            store = registered_store(label)
            rewritten, _ = rewrite_json_path(
                value, ("**",), str.upper, exclude_paths=store.nested_json_exclusions(field)
            )
            detail = rewritten["0"]["detail_json"] if field == "schedule_json" else rewritten
            self.assertEqual(detail["exercises"][0]["sets"][0]["type"], "WEIGHTED_REPS")
