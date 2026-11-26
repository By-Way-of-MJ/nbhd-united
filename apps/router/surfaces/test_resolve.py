"""Live A2UI dataModel refs resolve fresh at the read seam (needs a database).

Each ref kind is resolved through the same model/serializer the REST endpoint
uses; foreign, missing and malformed refs fail soft to ``None`` with one
content-free log line. Resolved values are owner-facing and must never be
passed through rehydration again.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.common.tenant_tz import tenant_today
from apps.friends import access
from apps.friends.models import Friendship, SharedGoalMembership
from apps.fuel.models import BodyWeightLog, SleepLog, Workout, WorkoutStatus
from apps.journal.models import Task
from apps.router.surfaces import resolve as resolve_module
from apps.router.surfaces.resolve import _sleep_summary, resolve_refs
from apps.router.surfaces.store import rehydrate_surfaces
from apps.tenants.models import Tenant, User


def _tenant(name: str) -> Tenant:
    user = User.objects.create_user(username=name)
    return Tenant.objects.create(user=user, status="active")


def _surface(data_model, text="Hello"):
    return {
        "version": "1.0",
        "createSurface": {
            "surfaceId": "tonight",
            "catalogId": "nbhd/v1",
            "components": [
                {"id": "root", "component": "Column", "children": ["title"]},
                {"id": "title", "component": "Text", "text": text},
            ],
            "dataModel": data_model,
        },
    }


class ResolveRefsTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("refs-owner")
        self.other = _tenant("refs-other")
        self.today = tenant_today(self.tenant)

    def _task(self, *, tenant=None, **fields):
        fields.setdefault("title", "Buy milk")
        fields.setdefault("status", Task.Status.OPEN)
        return Task.objects.create(tenant=tenant or self.tenant, **fields)

    def _resolve(self, ref, *, tenant=None):
        return resolve_refs({"value": {"$ref": ref}}, tenant=tenant or self.tenant)["value"]

    # ── tasks ──────────────────────────────────────────────────────────

    def test_tasks_due_today(self):
        due = self._task(due_date=self.today)
        self._task(due_date=self.today, status=Task.Status.DONE)
        self._task(due_date=self.today + timedelta(days=1))
        self.assertEqual(
            self._resolve("tasks:due_today"),
            [
                {
                    "id": str(due.id),
                    "title": "Buy milk",
                    "due_date": self.today.isoformat(),
                    "status": "open",
                    "done": False,
                }
            ],
        )

    def test_tasks_overdue_excludes_done_and_skipped(self):
        late = self._task(title="Late", due_date=self.today - timedelta(days=2))
        self._task(title="Done late", due_date=self.today - timedelta(days=2), status=Task.Status.DONE)
        self._task(title="Skipped late", due_date=self.today - timedelta(days=2), status=Task.Status.SKIPPED)
        self._task(title="Today", due_date=self.today)
        self.assertEqual([row["id"] for row in self._resolve("tasks:overdue")], [str(late.id)])

    def test_tasks_overdue_includes_eight_days_ago_and_excludes_today(self):
        old = self._task(title="Old", due_date=self.today - timedelta(days=8))
        self._task(title="Today", due_date=self.today)
        self.assertEqual([row["id"] for row in self._resolve("tasks:overdue")], [str(old.id)])

    def test_tasks_this_week_is_the_local_calendar_week(self):
        monday = self.today - timedelta(days=self.today.weekday())
        inside = self._task(title="Inside", due_date=monday)
        self._task(title="Next week", due_date=monday + timedelta(days=7))
        self.assertEqual([row["id"] for row in self._resolve("tasks:this_week")], [str(inside.id)])

    def test_tasks_open_is_status_open(self):
        open_task = self._task(title="Open")
        self._task(title="In progress", status=Task.Status.IN_PROGRESS)
        self._task(title="Done", status=Task.Status.DONE)
        self.assertEqual([row["id"] for row in self._resolve("tasks:open")], [str(open_task.id)])

    def test_tasks_list_is_capped_at_ten(self):
        for index in range(12):
            self._task(title=f"Task {index}")
        self.assertEqual(len(self._resolve("tasks:open")), 10)

    def test_single_task(self):
        task = self._task(title="Call Sam", due_date=self.today, status=Task.Status.IN_PROGRESS)
        self.assertEqual(
            self._resolve(f"task:{task.id}"),
            {
                "id": str(task.id),
                "title": "Call Sam",
                "due_date": self.today.isoformat(),
                "status": "in_progress",
                "done": False,
            },
        )

    def test_foreign_task_is_none(self):
        foreign = self._task(tenant=self.other, title="Not yours", due_date=self.today)
        with self.assertLogs("apps.router.surfaces.resolve", level="INFO") as logs:
            self.assertIsNone(self._resolve(f"task:{foreign.id}"))
        self.assertIn("a2ui_ref_unresolved kind=task reason=missing", logs.output[0])
        self.assertNotIn(str(foreign.id), logs.output[0])
        self.assertNotIn("Not yours", logs.output[0])

    # ── workouts ───────────────────────────────────────────────────────

    def _workout(self, *, tenant=None, status=WorkoutStatus.PLANNED, activity="Push", **fields):
        return Workout.objects.create(
            tenant=tenant or self.tenant,
            date=fields.pop("date", self.today),
            category="strength",
            activity=activity,
            status=status,
            detail_json={
                "exercises": [
                    {"name": "Bench Press", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 60}]},
                    {"name": "Row", "sets": [{"type": "weighted_reps", "reps": 8, "weight": 40.5}]},
                ]
            },
            **fields,
        )

    def test_workout_today_picks_planned_session(self):
        self._workout(status=WorkoutStatus.DONE, activity="Old")
        planned = self._workout()
        self.assertEqual(
            self._resolve("workout:today"),
            {
                "id": str(planned.id),
                "date": self.today.isoformat(),
                "activity": "Push",
                "status": "planned",
                "exercises": [
                    {"name": "Bench Press", "sets": [{"reps": 5, "weight": 60}]},
                    {"name": "Row", "sets": [{"reps": 8, "weight": 40.5}]},
                ],
            },
        )

    def test_single_workout(self):
        done = self._workout(status=WorkoutStatus.DONE, activity="Done lift")
        self.assertEqual(self._resolve(f"workout:{done.id}")["activity"], "Done lift")

    def test_foreign_workout_is_none(self):
        foreign = self._workout(tenant=self.other)
        self.assertIsNone(self._resolve(f"workout:{foreign.id}"))

    # ── sleep ──────────────────────────────────────────────────────────

    def test_sleep_last_night(self):
        SleepLog.objects.create(tenant=self.tenant, date=self.today - timedelta(days=2), duration_hours="5.00")
        SleepLog.objects.create(
            tenant=self.tenant,
            date=self.today - timedelta(days=1),
            duration_hours="7.50",
            quality=4,
        )
        self.assertEqual(
            self._resolve("sleep:last_night"),
            {
                "date": (self.today - timedelta(days=1)).isoformat(),
                "hours": 7.5,
                "quality": 4,
                "summary": "7h 30m",
            },
        )

    def test_sleep_summary_never_prints_sixty_minutes(self):
        self.assertEqual(_sleep_summary(7.996), "8h 0m")
        self.assertEqual(_sleep_summary(7.995), "8h 0m")
        self.assertEqual(_sleep_summary(0), "0h 0m")
        for hundredths in range(0, 2401):
            summary = _sleep_summary(hundredths / 100)
            self.assertNotIn("60m", summary)
            self.assertRegex(summary, r"^\d+h (?:[0-5]?\d)m$")

    # ── weight ─────────────────────────────────────────────────────────

    def test_weight_latest(self):
        BodyWeightLog.objects.create(tenant=self.tenant, date=self.today - timedelta(days=3), weight_kg="73.00")
        BodyWeightLog.objects.create(tenant=self.tenant, date=self.today, weight_kg="72.04")
        self.assertEqual(
            self._resolve("weight:latest"),
            {"date": self.today.isoformat(), "kg": 72.04, "unit": "kg", "display": "72.0 kg"},
        )

    # ── missions ───────────────────────────────────────────────────────

    def _mission(self, *, members, title="Run together"):
        edge = Friendship.objects.create(
            requester=members[0],
            addressee=members[1],
            status=Friendship.Status.ACCEPTED,
            responded_at=timezone.now(),
        )
        goal = access.create_mission(members[0], edge, title=title)
        for member in members:
            SharedGoalMembership.objects.create(shared_goal=goal, tenant=member, user=member.user, status="active")
        return goal

    def _step(self, goal, title, *, status, order, due_date=None):
        return access.project_steps(goal).create(
            shared_goal=goal,
            title=title,
            status=status,
            order=order,
            due_date=due_date,
            created_by=self.tenant,
        )

    def test_mission(self):
        goal = self._mission(members=[self.tenant, self.other])
        self._step(goal, "Book the hall", status="done", order=0, due_date=self.today)
        nxt = self._step(
            goal, "Call the council", status="in_progress", order=1, due_date=self.today + timedelta(days=1)
        )
        self._step(goal, "Print flyers", status="open", order=2, due_date=self.today + timedelta(days=2))
        self.assertEqual(
            self._resolve(f"mission:{goal.id}"),
            {
                "id": str(goal.id),
                "title": "Run together",
                "next_step": {
                    "id": str(nxt.id),
                    "title": "Call the council",
                    "due_date": (self.today + timedelta(days=1)).isoformat(),
                },
                "steps_open": 2,
            },
        )

    def test_foreign_mission_is_none(self):
        goal = self._mission(members=[self.other, _tenant("refs-third")])
        with self.assertLogs("apps.router.surfaces.resolve", level="INFO") as logs:
            self.assertIsNone(self._resolve(f"mission:{goal.id}"))
        self.assertIn("a2ui_ref_unresolved kind=mission reason=not_owned", logs.output[0])
        self.assertNotIn(str(goal.id), logs.output[0])

    # ── failure + caching ──────────────────────────────────────────────

    def test_unknown_ref_is_none_with_one_content_free_log(self):
        with self.assertLogs("apps.router.surfaces.resolve", level="INFO") as logs:
            self.assertIsNone(self._resolve("bogus:thing"))
        self.assertEqual(
            logs.output, ["INFO:apps.router.surfaces.resolve:a2ui_ref_unresolved kind=unknown reason=unknown"]
        )

    def test_missing_ref_is_none(self):
        with self.assertLogs("apps.router.surfaces.resolve", level="INFO") as logs:
            self.assertIsNone(self._resolve("weight:latest"))
        self.assertIn("a2ui_ref_unresolved kind=weight reason=missing", logs.output[0])

    def test_same_ref_resolves_once_per_call(self):
        SleepLog.objects.create(tenant=self.tenant, date=self.today, duration_hours="8.00")
        with patch.object(resolve_module, "_resolve_ref", wraps=resolve_module._resolve_ref) as inner:
            result = resolve_refs(
                {"a": {"$ref": "sleep:last_night"}, "b": {"$ref": "sleep:last_night"}},
                tenant=self.tenant,
            )
        inner.assert_called_once()
        self.assertEqual(result["a"], result["b"])

    def test_input_is_not_mutated_and_output_is_a_new_dict(self):
        data_model = {"tasks": {"$ref": "tasks:open"}, "heading": "Today"}
        original = {"tasks": {"$ref": "tasks:open"}, "heading": "Today"}
        result = resolve_refs(data_model, tenant=self.tenant)
        self.assertIsNot(result, data_model)
        self.assertEqual(data_model, original)
        self.assertEqual(result["heading"], "Today")
        self.assertIsInstance(result["tasks"], list)

    def test_resolved_values_are_not_rehydrated_again(self):
        self._task(title="Buy milk")
        with patch(
            "apps.router.reply_text.finalize_outbound_text",
            side_effect=lambda text, *args, **kwargs: text.upper(),
        ):
            [surface] = rehydrate_surfaces(
                [_surface({"tasks": {"$ref": "tasks:open"}, "note": "static"})],
                {},
                tenant_id=str(self.tenant.id),
                tenant=self.tenant,
            )
        body = surface["createSurface"]
        by_id = {component["id"]: component for component in body["components"]}
        self.assertEqual(by_id["title"]["text"], "HELLO")
        self.assertEqual(body["dataModel"]["note"], "STATIC")
        self.assertEqual(body["dataModel"]["tasks"][0]["title"], "Buy milk")
