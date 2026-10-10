"""Calendar math contracts: pure snapshots, no database or journal access."""

from copy import deepcopy
from datetime import UTC, date, datetime

from django.test import SimpleTestCase

from .plan_projection import build_plan
from .project_hygiene import clean_text

TODAY = date(2026, 10, 1)


def step(id, start=None, due=None, **extra):
    return dict(
        id=id,
        start_date=date(2026, 10, start) if start else None,
        due_date=date(2026, 10, due) if due else None,
        status="open",
        milestone_id=None,
        **extra,
    )


def snapshot(steps=(), edges=(), milestones=(), **extra):
    return dict(
        mission_id="goal",
        title="Garden",
        target_date=None,
        members=[],
        assignments=[],
        steps=list(steps),
        edges=[dict(id=str(i), blocker_id=a, blocked_id=b) for i, (a, b) in enumerate(edges)],
        milestones=list(milestones),
        **extra,
    )


def result(data, sid):
    return next(s for s in build_plan(data, today=TODAY)["steps"] if s["id"] == sid)


class PlanProjectionTests(SimpleTestCase):
    def test_empty_project(self):
        plan = build_plan(snapshot(), today=TODAY)
        self.assertEqual((plan["health"], plan["done_count"], plan["total"]), ("on_track", 0, 0))

    def test_undated_step_has_no_slack_or_finish(self):
        row = result(snapshot([step("a")]), "a")
        self.assertIsNone(row["slack_days"])
        self.assertIsNone(row["projected_date"])
        self.assertFalse(row["on_critical_path"])
        self.assertTrue(row["ready"])

    def test_unconstrained_dated_step_has_null_slack(self):
        self.assertIsNone(result(snapshot([step("a", 1, 3)]), "a")["slack_days"])

    def test_buffer_and_one_day_impact(self):
        data = snapshot([step("a", 1, 3), step("b", 5, 7)], [("a", "b")])
        self.assertEqual(result(data, "a")["slack_days"], 2)
        self.assertEqual(result(data, "a")["moves_if_late"], [])
        self.assertEqual(result(data, "b")["blocked_by_open"], ["a"])
        self.assertFalse(result(data, "b")["ready"])

    def test_zero_buffer_critical_chain(self):
        data = snapshot([step("a", 1, 3), step("b", 3, 5), step("c", 5, 6)], [("a", "b"), ("b", "c")])
        row = result(data, "a")
        self.assertEqual(row["slack_days"], 0)
        self.assertTrue(row["on_critical_path"])
        self.assertEqual(row["moves_if_late"], ["b", "c"])
        self.assertEqual(build_plan(data, today=TODAY)["health"], "at_risk")

    def test_negative_slack_and_projected_delay(self):
        data = snapshot([step("a", 1, 5), step("b", 3, 6)], [("a", "b")])
        self.assertEqual(result(data, "a")["slack_days"], -2)
        self.assertEqual(result(data, "b")["projected_date"], "2026-10-08")

    def test_diamond_uses_longest_incoming_finish(self):
        data = snapshot([step("a", 1, 3), step("b", 1, 5), step("c", 2, 4)], [("a", "c"), ("b", "c")])
        self.assertEqual(result(data, "c")["projected_date"], "2026-10-07")
        self.assertEqual(result(data, "a")["moves_if_late"], [])
        self.assertEqual(result(data, "b")["moves_if_late"], ["c"])

    def test_closed_blockers_do_not_delay(self):
        for status in ["done", "skipped"]:
            a = step("a", 1, 9)
            a["status"] = status
            data = snapshot([a, step("b", 2, 3)], [("a", "b")])
            self.assertTrue(result(data, "b")["ready"])
            self.assertEqual(result(data, "b")["projected_date"], "2026-10-03")
            self.assertIsNone(result(data, "a")["slack_days"])
            self.assertEqual(result(data, "a")["moves_if_late"], [])

    def test_unknown_blocker_makes_finish_unknown(self):
        data = snapshot([step("a"), step("b", 2, 3)], [("a", "b")])
        self.assertIsNone(result(data, "b")["projected_date"])

    def test_milestone_target_propagates_backwards(self):
        a, b = step("a", 1, 3), step("b", 3, 6)
        b["milestone_id"] = "m"
        data = snapshot([a, b], [("a", "b")], [dict(id="m", target_date=date(2026, 10, 5))])
        self.assertEqual(result(data, "a")["slack_days"], -1)
        self.assertEqual(result(data, "b")["slack_days"], -1)
        milestone = build_plan(data, today=TODAY)["milestones"][0]
        self.assertEqual(milestone["projected_date"], "2026-10-06")
        self.assertEqual((milestone["done_count"], milestone["total"]), (0, 1))

    def test_goal_deadline_constrains_terminal_step(self):
        data = snapshot([step("a", 1, 4)])
        data["target_date"] = date(2026, 10, 6)
        self.assertEqual(result(data, "a")["slack_days"], 2)

    def test_overdue_health_takes_precedence(self):
        data = snapshot([dict(id="a", start_date=None, due_date=date(2026, 9, 30), status="open")])
        self.assertEqual(build_plan(data, today=TODAY)["health"], "late")
        self.assertEqual(result(data, "a")["projected_date"], "2026-10-01")

    def test_completed_project_is_on_track_and_uses_completion_date(self):
        a = step("a", 1, 3, completed_at=datetime(2026, 9, 30, tzinfo=UTC))
        a.update(status="done", milestone_id="m")
        plan = build_plan(snapshot([a], milestones=[dict(id="m", target_date=date(2026, 9, 29))]), today=TODAY)
        self.assertEqual(plan["done_count"], 1)
        self.assertEqual(plan["health"], "on_track")
        self.assertEqual(plan["milestones"][0]["projected_date"], "2026-09-30")

    def test_milestone_unknown_if_one_step_undated(self):
        a, b = step("a", 1, 3), step("b")
        a["milestone_id"] = b["milestone_id"] = "m"
        plan = build_plan(snapshot([a, b], milestones=[dict(id="m", target_date=None)]), today=TODAY)
        self.assertIsNone(plan["milestones"][0]["projected_date"])

    def test_cycle_and_foreign_edge_fail_explicitly(self):
        for edges in [[("a", "b"), ("b", "a")], [("a", "c")], [("a", "a")]]:
            with self.assertRaises(ValueError):
                build_plan(snapshot([step("a"), step("b")], edges), today=TODAY)

    def test_no_input_mutation(self):
        data = snapshot([step("a", 1, 2)])
        before = deepcopy(data)
        build_plan(data, today=TODAY)
        self.assertEqual(data, before)

    def test_assignment_states_and_only_active_accepted_owners(self):
        data = snapshot([step("a")])
        data["members"] = [
            dict(id=str(i), status="active" if i < 4 else "left", handle=f"p{i}", display_name="Name", hue=10)
            for i in range(5)
        ]
        data["assignments"] = [
            dict(step_id="a", membership_id=str(i), status=status)
            for i, status in enumerate(["accepted", "asked", "declined", "countered", "accepted"])
        ]
        row = result(data, "a")
        self.assertEqual(len(row["assignments"]), 5)
        self.assertEqual([m["handle"] for m in row["owners"]], ["p0"])

    def test_sixty_step_chain_is_deterministic(self):
        data = snapshot([step(str(i), 1, 1) for i in range(60)], [(str(i), str(i + 1)) for i in range(59)])
        self.assertEqual(len(result(data, "0")["moves_if_late"]), 59)


class TextHygieneTests(SimpleTestCase):
    def test_control_zero_width_bidi_and_whitespace(self):
        chars = (
            "\x00\x01\x7f\x85\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"
            + "".join(chr(i) for i in range(0x202A, 0x202F))
            + "".join(chr(i) for i in range(0x2066, 0x206A))
        )
        self.assertEqual(clean_text("  Hello" + chars + "\n\t world  "), "Hello world")

    def test_caps_after_normalization(self):
        from rest_framework.exceptions import ValidationError

        for cap in [120, 200, 500]:
            self.assertEqual(clean_text(" x " + "y" * (cap - 2), limit=cap), "x " + "y" * (cap - 2))
            with self.assertRaises(ValidationError):
                clean_text("x" * (cap + 1), limit=cap)

    def test_types_empty_and_unicode(self):
        from rest_framework.exceptions import ValidationError

        for value in [[], {}, 1, True]:
            with self.assertRaises(ValidationError):
                clean_text(value)
        with self.assertRaises(ValidationError):
            clean_text("\u200b", required=True)
        self.assertEqual(clean_text("  一緒に 🌱  "), "一緒に 🌱")
        self.assertEqual(clean_text(None), "")
