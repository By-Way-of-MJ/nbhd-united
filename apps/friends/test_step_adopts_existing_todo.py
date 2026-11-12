"""A project step that becomes yours adopts your open to-do with the same name."""

from datetime import date, timedelta
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.journal.models import Goal, Task

from . import project_services as projects
from . import services
from .models import SharedGoalMembership, SharedGoalStepAssignment, SharedGoalUpdate
from .test_pr6 import _edge, _profile, _tenant

TITLE = "Renew passport + gather docs"
NOTES = "Fees: 100 dollars."


def _step_ref(step):
    return {"pillar": "neighborhood", "object_type": "SharedGoalStep", "object_id": str(step.id)}


# Fixture style copied from MissionLifecycleTests.setUp in
# apps/friends/test_mission_lifecycle_be2.py (tenants/profiles/edge from test_pr6,
# create_mission + join_mission, and projects.add_owned_step as at lines 293/321)
# and the ask/accept helpers in ProjectAPITests in apps/friends/test_projects_v2.py.
@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class StepAdoptsExistingTodoTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner = _tenant("adopt_owner")
        self.member = _tenant("adopt_member")
        _profile(self.owner, "adopt_owner")
        _profile(self.member, "adopt_member")
        self.edge = _edge(self.owner, self.member)
        self.goal = services.create_mission(
            self.owner, self.owner.user, member_friendship_ids=[str(self.edge.id)], title="Citizenship"
        )
        services.join_mission(self.member, self.member.user, self.goal.id)
        self.member_row = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.member)
        self.owner_row = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.owner)

    def todo(self, *, tenant=None, **overrides):
        values = {"tenant": tenant or self.owner, "title": TITLE, "description": NOTES, **overrides}
        return Task.objects.create(**values)

    def tasks_with_title(self, *, tenant=None):
        """Rows whose title matches TITLE — compared in Python, never in the DB.

        Task.title is registered for phase-3 encryption-at-rest, so a raw
        ``title=`` predicate would silently stop matching once it is
        ciphertext (see scripts/check_encrypted_column_predicates.py).
        """
        rows = Task.objects.filter(tenant=tenant or self.owner)
        return [row for row in rows if row.title == TITLE]

    def open_with_title(self, *, tenant=None):
        return [row for row in self.tasks_with_title(tenant=tenant) if row.status in ("open", "in_progress")]

    def test_add_owned_step_adopts_open_todo_with_same_title(self):
        existing = self.todo()

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)

        self.assertEqual(len(self.open_with_title()), 1)
        self.assertEqual(task.id, existing.id)
        self.assertEqual(task.related_ref, _step_ref(step))
        self.assertEqual(task.description, NOTES)
        assignment = SharedGoalStepAssignment.objects.get(step=step, membership=self.owner_row)
        self.assertEqual(assignment.task_id, existing.id)

    def test_asked_step_answered_yes_keeps_private_todo_unlinked(self):
        existing = self.todo(tenant=self.member)
        step = projects.create_step(self.owner, self.owner.user, self.goal.id, {"title": TITLE})
        self.assertEqual(step.created_by_id, self.owner.id)
        projects.ask(self.owner, self.owner.user, self.goal.id, step.id, [str(self.member_row.id)])

        assignment = projects.respond(self.member, self.member.user, self.goal.id, step.id, {"answer": "yes"})

        self.assertEqual(len(self.open_with_title(tenant=self.member)), 2)
        self.assertNotEqual(assignment.task_id, existing.id)
        existing.refresh_from_db()
        self.assertIsNone(existing.related_ref)
        self.assertEqual(existing.description, NOTES)
        minted = Task.objects.get(id=assignment.task_id)
        self.assertEqual(minted.related_ref, _step_ref(step))
        self.assertEqual(minted.description, "")

    def test_adopted_row_keeps_id_description_created_at_and_title(self):
        existing = self.todo()
        stamp = timezone.now() - timedelta(days=3)
        Task.objects.filter(id=existing.id).update(created_at=stamp)
        existing.refresh_from_db()
        before = (existing.title, existing.description, dict(existing.pii_receipts), existing.created_at)

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)

        task.refresh_from_db()
        self.assertEqual(task.id, existing.id)
        self.assertEqual((task.title, task.description, task.pii_receipts, task.created_at), before)
        self.assertEqual(task.related_ref, _step_ref(step))

    def test_done_task_with_same_title_is_not_adopted(self):
        done = self.todo(status="done", completed_at=timezone.now())

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)

        self.assertNotEqual(task.id, done.id)
        self.assertEqual(len(self.tasks_with_title()), 2)
        done.refresh_from_db()
        self.assertEqual((done.status, done.related_ref), ("done", None))
        self.assertEqual(task.related_ref, _step_ref(step))

    def test_task_that_already_has_a_related_ref_is_not_adopted(self):
        linked = self.todo(
            related_ref={"pillar": "gravity", "object_type": "FinanceAccount", "object_id": str(uuid4())}
        )

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)

        self.assertNotEqual(task.id, linked.id)
        linked.refresh_from_db()
        self.assertEqual(linked.related_ref["object_type"], "FinanceAccount")
        self.assertEqual(len(self.tasks_with_title()), 2)
        self.assertEqual(task.related_ref, _step_ref(step))

    def test_different_title_is_not_adopted(self):
        other = self.todo(title="Renew driver's licence")

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)

        self.assertNotEqual(task.id, other.id)
        other.refresh_from_db()
        self.assertIsNone(other.related_ref)
        self.assertEqual(task.title, TITLE)
        self.assertEqual(task.related_ref, _step_ref(step))

    def test_subtask_is_not_adopted(self):
        parent = Task.objects.create(tenant=self.owner, title="Move house")
        sub = self.todo(parent_task=parent)

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)

        self.assertNotEqual(task.id, sub.id)
        sub.refresh_from_db()
        self.assertIsNone(sub.related_ref)
        self.assertEqual(len(self.tasks_with_title()), 2)
        self.assertEqual(task.related_ref, _step_ref(step))

    def test_title_match_normalises_case_and_whitespace(self):
        existing = self.todo(title="  renew   PASSPORT + gather docs ")

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)

        self.assertEqual(task.id, existing.id)
        task.refresh_from_db()
        self.assertEqual(task.title, "  renew   PASSPORT + gather docs ")
        self.assertEqual(task.related_ref, _step_ref(step))

    def test_oldest_matching_todo_is_adopted(self):
        older = self.todo()
        newer = self.todo()
        Task.objects.filter(id=older.id).update(created_at=timezone.now() - timedelta(days=2))
        Task.objects.filter(id=newer.id).update(created_at=timezone.now() - timedelta(days=1))

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)

        self.assertEqual(task.id, older.id)
        newer.refresh_from_db()
        self.assertIsNone(newer.related_ref)
        self.assertEqual(task.related_ref, _step_ref(step))

    def test_adoption_keeps_existing_due_date_and_parent_goal(self):
        other_goal = Goal.objects.create(tenant=self.owner, title="Someone else's goal")
        linked_goal = Goal.objects.create(tenant=self.owner, title="Citizenship goal")
        SharedGoalMembership.objects.filter(id=self.owner_row.id).update(linked_goal_id=linked_goal.id)
        kept_due = date(2026, 12, 1)
        existing = self.todo(due_date=kept_due, parent_goal=other_goal)

        _step, task = projects.add_owned_step(
            self.owner, self.owner.user, self.goal.id, title=TITLE, due_date=date(2026, 11, 1)
        )

        self.assertEqual(task.id, existing.id)
        self.assertEqual(task.due_date, kept_due)
        self.assertEqual(task.parent_goal_id, other_goal.id)

    def test_adoption_fills_missing_due_date_and_parent_goal(self):
        linked_goal = Goal.objects.create(tenant=self.owner, title="Citizenship goal")
        SharedGoalMembership.objects.filter(id=self.owner_row.id).update(linked_goal_id=linked_goal.id)
        existing = self.todo()
        step_due = date(2026, 11, 1)

        _step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE, due_date=step_due)

        self.assertEqual(task.id, existing.id)
        self.assertEqual(task.due_date, step_due)
        self.assertEqual(task.parent_goal_id, linked_goal.id)

    def test_ticking_adopted_todo_completes_step_and_reopen_mirrors(self):
        # Mirrors test_journal_done_is_idempotent_and_api_reopen_mirrors in
        # apps/friends/test_projects_v2.py (line 489).
        self.todo()

        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title=TITLE)
        task.complete()
        task.save()

        step.refresh_from_db()
        self.assertEqual(step.status, "done")
        self.assertEqual(step.completed_by_id, self.owner.id)
        self.assertEqual(SharedGoalUpdate.objects.filter(kind="step_done").count(), 1)

        projects.complete(self.owner, self.owner.user, self.goal.id, step.id, reopen=True)
        task.refresh_from_db()
        self.assertEqual((task.status, task.completed_at), ("open", None))
