"""Tests for session-auth typed write endpoints + the markdown-PATCH guard.

The write-path fix: GET re-synthesizes tasks/goal docs from typed rows, so a
markdown PATCH would be silently discarded. These verify (a) the typed write
endpoints update the rows correctly, and (b) DocumentDetailView.patch now
rejects (409) markdown writes to flag-on tasks/goal docs instead of losing them.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.friends.models import SharedGoal, SharedGoalMembership, SharedGoalStep, SharedGoalStepAssignment
from apps.journal.lifecycle_views import _search_variants
from apps.journal.models import Document, Goal, PendingTaskAction, Task
from apps.pii.testsupport import neural_ran
from apps.tenants.management.commands.disable_rls import RLS_KEEP_ENABLED
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key


class TypedWriteEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="writeuser", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active", experimental_typed_journal_lifecycle=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_complete_sets_status_and_timestamp(self):
        task = Task.objects.create(tenant=self.tenant, title="Pay loan", status=Task.Status.OPEN)
        resp = self.client.post(f"/api/v1/journal/tasks/{task.id}/complete/")
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertIsNotNone(task.completed_at)

    def test_reopen_clears_timestamp(self):
        task = Task.objects.create(tenant=self.tenant, title="Pay loan", status=Task.Status.OPEN)
        task.complete()
        resp = self.client.post(f"/api/v1/journal/tasks/{task.id}/reopen/")
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.OPEN)
        self.assertIsNone(task.completed_at)

    def test_patch_task_title(self):
        task = Task.objects.create(tenant=self.tenant, title="old", status=Task.Status.OPEN)
        resp = self.client.patch(f"/api/v1/journal/tasks/{task.id}/", {"title": "new"}, format="json")
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.title, "new")

    def test_goal_achieve_and_abandon(self):
        goal = Goal.objects.create(tenant=self.tenant, title="Debt-free", status=Goal.Status.ACTIVE)
        resp = self.client.post(f"/api/v1/journal/goals/{goal.id}/achieve/")
        self.assertEqual(resp.status_code, 200)
        goal.refresh_from_db()
        self.assertEqual(goal.status, Goal.Status.ACHIEVED)
        self.assertIsNotNone(goal.achieved_at)

        resp = self.client.post(f"/api/v1/journal/goals/{goal.id}/abandon/")
        self.assertEqual(resp.status_code, 200)
        goal.refresh_from_db()
        self.assertEqual(goal.status, Goal.Status.ABANDONED)

    def test_requires_auth(self):
        task = Task.objects.create(tenant=self.tenant, title="t", status=Task.Status.OPEN)
        anon = APIClient()
        resp = anon.post(f"/api/v1/journal/tasks/{task.id}/complete/")
        self.assertIn(resp.status_code, (401, 403))

    def test_tenant_isolation(self):
        other_user = User.objects.create_user(username="other_w", password="x")
        other_tenant = Tenant.objects.create(user=other_user, status="active")
        task = Task.objects.create(tenant=other_tenant, title="theirs", status=Task.Status.OPEN)
        resp = self.client.post(f"/api/v1/journal/tasks/{task.id}/complete/")
        self.assertEqual(resp.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.OPEN)  # untouched


class TaskDeleteTests(TestCase):
    """DELETE /tasks/<uuid>/ — the owner's irreversible endpoint.

    ``Task.parent_task`` is CASCADE, so the delete must re-parent the direct
    children up one level before removing the row. A task's shared-goal
    assignment is SET_NULL and must survive with no task.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="deleteuser", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.other_user = User.objects.create_user(username="deleteother", password="x")
        self.other_tenant = Tenant.objects.create(user=self.other_user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _delete(self, task_id):
        return self.client.delete(f"/api/v1/journal/tasks/{task_id}/")

    def test_owner_deletes_own_task(self):
        task = Task.objects.create(tenant=self.tenant, title="Cancel the gym")

        resp = self._delete(task.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "deleted")
        self.assertEqual(resp.data["id"], str(task.id))
        self.assertEqual(resp.data["subtasks_kept"], 0)
        self.assertFalse(Task.objects.filter(id=task.id).exists())

    def test_another_tenants_task_is_404_and_survives(self):
        task = Task.objects.create(tenant=self.other_tenant, title="Not yours")

        resp = self._delete(task.id)

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data["error"], "not_found")
        self.assertTrue(Task.objects.filter(id=task.id).exists())

    def test_delete_requires_auth(self):
        task = Task.objects.create(tenant=self.tenant, title="Keep me")
        anon = APIClient()

        resp = anon.delete(f"/api/v1/journal/tasks/{task.id}/")

        self.assertIn(resp.status_code, (401, 403))
        self.assertTrue(Task.objects.filter(id=task.id).exists())

    def test_direct_subtasks_move_up_to_the_grandparent(self):
        grandparent = Task.objects.create(tenant=self.tenant, title="Grandparent")
        parent = Task.objects.create(tenant=self.tenant, title="Parent", parent_task=grandparent)
        first = Task.objects.create(tenant=self.tenant, title="First", parent_task=parent)
        second = Task.objects.create(tenant=self.tenant, title="Second", parent_task=parent)

        resp = self._delete(parent.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["subtasks_kept"], 2)
        self.assertFalse(Task.objects.filter(id=parent.id).exists())
        for child in (first, second):
            child.refresh_from_db()
            self.assertEqual(child.parent_task_id, grandparent.id)
        self.assertTrue(Task.objects.filter(id=grandparent.id).exists())

    def test_top_level_task_subtask_becomes_top_level(self):
        task = Task.objects.create(tenant=self.tenant, title="Top")
        child = Task.objects.create(tenant=self.tenant, title="Child", parent_task=task)

        resp = self._delete(task.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["subtasks_kept"], 1)
        child.refresh_from_db()
        self.assertIsNone(child.parent_task_id)

    def test_cross_tenant_subtask_refuses_the_delete_and_keeps_both_rows(self):
        task = Task.objects.create(tenant=self.tenant, title="Mine")
        foreign_child = Task.objects.create(
            tenant=self.other_tenant,
            title="Someone else's",
            parent_task=task,
        )

        resp = self._delete(task.id)

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "integrity_error")
        self.assertTrue(Task.objects.filter(id=task.id).exists())
        self.assertTrue(Task.objects.filter(id=foreign_child.id).exists())

    def test_delete_reports_the_tasks_own_pending_actions(self):
        task = Task.objects.create(tenant=self.tenant, title="Auto-completed")
        PendingTaskAction.objects.create(
            tenant=self.tenant,
            kind=PendingTaskAction.Kind.TASK_COMPLETE,
            task=task,
            source_date=date(2026, 8, 8),
        )

        resp = self._delete(task.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["pending_actions_deleted"], 1)
        self.assertFalse(Task.objects.filter(id=task.id).exists())

    def test_shared_goal_step_assignment_survives_with_no_task(self):
        task = Task.objects.create(tenant=self.tenant, title="Hand off")
        goal = SharedGoal.objects.create(title="Garden", created_by=self.tenant)
        membership = SharedGoalMembership.objects.create(
            shared_goal=goal,
            tenant=self.tenant,
            user=self.user,
            role="owner",
            status="active",
        )
        step = SharedGoalStep.objects.create(shared_goal=goal, title="Dig", created_by=self.tenant)
        assignment = SharedGoalStepAssignment.objects.create(
            step=step,
            membership=membership,
            asked_by=self.tenant,
            status="accepted",
            task=task,
        )

        resp = self._delete(task.id)

        self.assertEqual(resp.status_code, 200)
        assignment.refresh_from_db()
        self.assertIsNone(assignment.task_id)
        self.assertTrue(SharedGoalStepAssignment.objects.filter(id=assignment.id).exists())

    def test_deleting_project_copy_hands_the_step_to_its_twin(self):
        twin = Task.objects.create(
            tenant=self.tenant,
            title="Book flights",
            description="Family trip — four seats, window preferred.",
        )
        goal = SharedGoal.objects.create(title="Family visit", created_by=self.tenant)
        membership = SharedGoalMembership.objects.create(
            shared_goal=goal,
            tenant=self.tenant,
            user=self.user,
            role="owner",
            status="active",
        )
        step = SharedGoalStep.objects.create(shared_goal=goal, title="Book flights", created_by=self.tenant)
        related_ref = {"pillar": "neighborhood", "object_type": "SharedGoalStep", "object_id": str(step.id)}
        minted = Task.objects.create(tenant=self.tenant, title="Book flights", related_ref=related_ref)
        assignment = SharedGoalStepAssignment.objects.create(
            step=step,
            membership=membership,
            asked_by=self.tenant,
            status="accepted",
            task=minted,
        )

        resp = self._delete(minted.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["step_moved_to"], str(twin.id))
        twin.refresh_from_db()
        self.assertEqual(twin.related_ref, related_ref)
        self.assertEqual(twin.description, "Family trip — four seats, window preferred.")
        assignment.refresh_from_db()
        self.assertEqual(assignment.task_id, twin.id)
        self.assertFalse(Task.objects.filter(id=minted.id).exists())

        # Ticking the surviving private row still completes the shared step.
        twin.complete()
        twin.save()
        step.refresh_from_db()
        self.assertEqual(step.status, "done")
        self.assertEqual(step.completed_by_id, self.tenant.id)

    def test_deleting_project_copy_without_a_twin_leaves_the_step_unassigned(self):
        task = Task.objects.create(tenant=self.tenant, title="Book flights")
        goal = SharedGoal.objects.create(title="Family visit", created_by=self.tenant)
        membership = SharedGoalMembership.objects.create(
            shared_goal=goal,
            tenant=self.tenant,
            user=self.user,
            role="owner",
            status="active",
        )
        step = SharedGoalStep.objects.create(shared_goal=goal, title="Book flights", created_by=self.tenant)
        assignment = SharedGoalStepAssignment.objects.create(
            step=step,
            membership=membership,
            asked_by=self.tenant,
            status="accepted",
            task=task,
        )

        resp = self._delete(task.id)

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["step_moved_to"])
        assignment.refresh_from_db()
        self.assertIsNone(assignment.task_id)


class MarkdownPatchGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="guarduser", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active", experimental_typed_journal_lifecycle=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_markdown_patch_to_tasks_doc_rejected_when_flag_on(self):
        resp = self.client.patch(
            "/api/v1/journal/documents/tasks/tasks/",
            {"markdown": "# hand edit\n- [ ] sneaky"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "typed_lifecycle_readonly")

    def test_markdown_patch_to_goal_doc_rejected_when_flag_on(self):
        Document.objects.create(tenant=self.tenant, kind="goal", slug="g", title="G", markdown="# G")
        resp = self.client.patch(
            "/api/v1/journal/documents/goal/g/",
            {"markdown": "# hand edit"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_title_only_patch_to_tasks_doc_allowed(self):
        resp = self.client.patch(
            "/api/v1/journal/documents/tasks/tasks/",
            {"title": "My Tasks"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_markdown_patch_to_daily_doc_allowed(self):
        Document.objects.create(tenant=self.tenant, kind="daily", slug="2026-06-04", title="D", markdown="# D")
        resp = self.client.patch(
            "/api/v1/journal/documents/daily/2026-06-04/",
            {"markdown": "# edited daily log"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("edited daily log", resp.data["markdown"])

    def test_markdown_patch_to_tasks_doc_allowed_when_flag_off(self):
        self.tenant.experimental_typed_journal_lifecycle = False
        self.tenant.save(update_fields=["experimental_typed_journal_lifecycle"])
        resp = self.client.patch(
            "/api/v1/journal/documents/tasks/tasks/",
            {"markdown": "# legacy editable tasks"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("legacy editable tasks", resp.data["markdown"])


class TaskGoalListCreateTests(TestCase):
    """GET (list) + POST (create) for /tasks/ and /goals/ — the collection
    endpoints the connected iOS client uses to enumerate and add typed rows."""

    def setUp(self):
        self.user = User.objects.create_user(username="lcuser", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.other_user = User.objects.create_user(username="lcother", password="x")
        self.other_tenant = Tenant.objects.create(user=self.other_user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # ── Tasks ────────────────────────────────────────────────────────

    def test_create_task(self):
        resp = self.client.post(
            "/api/v1/journal/tasks/",
            {"title": "Pay loan", "pillar": "gravity", "due_date": "2026-06-20"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["title"], "Pay loan")
        self.assertEqual(resp.data["status"], Task.Status.OPEN)
        task = Task.objects.get(id=resp.data["id"])
        self.assertEqual(task.tenant_id, self.tenant.id)
        self.assertEqual(task.title, "Pay loan")
        self.assertEqual(
            task.pii_receipts["title"],
            {"state": "bypass", "mode": "legacy-redact", "writer": "owner"},
        )

    def test_flag_on_create_stores_placeholder_and_owner_receipt(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", side_effect=neural_ran([])):
            resp = self.client.post(
                "/api/v1/journal/tasks/",
                {"title": "Call Alice"},
                format="json",
            )

        self.assertEqual(resp.status_code, 201)
        task = Task.objects.get(id=resp.data["id"])
        self.assertEqual(task.title, "Call [PERSON_1]")
        self.assertEqual(resp.data["title"], "Call Alice")
        self.assertEqual(task.pii_receipts["title"]["state"], "placeholder")
        self.assertEqual(task.pii_receipts["title"]["writer"], "owner")
        self.assertEqual(
            task.pii_receipts["title"]["redactions"],
            [{"placeholder": "[PERSON_1]"}],
        )
        self.assertEqual(
            resp.data["pii_receipts"]["title"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

    def test_create_task_requires_title(self):
        resp = self.client.post("/api/v1/journal/tasks/", {"pillar": "gravity"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_list_tasks_scoped_to_tenant(self):
        Task.objects.create(tenant=self.tenant, title="mine")
        Task.objects.create(tenant=self.other_tenant, title="theirs")
        resp = self.client.get("/api/v1/journal/tasks/")
        self.assertEqual(resp.status_code, 200)
        titles = [t["title"] for t in resp.data]
        self.assertEqual(titles, ["mine"])

    def test_list_tasks_filter_by_status(self):
        Task.objects.create(tenant=self.tenant, title="open one", status=Task.Status.OPEN)
        Task.objects.create(tenant=self.tenant, title="done one", status=Task.Status.DONE)
        resp = self.client.get("/api/v1/journal/tasks/?status=done")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t["title"] for t in resp.data], ["done one"])

    def test_list_tasks_filter_due_before(self):
        Task.objects.create(tenant=self.tenant, title="soon", due_date="2026-06-10")
        Task.objects.create(tenant=self.tenant, title="later", due_date="2026-07-10")
        resp = self.client.get("/api/v1/journal/tasks/?due_before=2026-06-30")
        self.assertEqual([t["title"] for t in resp.data], ["soon"])

    def test_list_tasks_invalid_due_before_returns_400(self):
        resp = self.client.get("/api/v1/journal/tasks/?due_before=nope")
        self.assertEqual(resp.status_code, 400)

    def test_list_tasks_q_name_search_case_insensitive(self):
        Task.objects.create(tenant=self.tenant, title="Call the Dentist")
        Task.objects.create(tenant=self.tenant, title="Buy groceries")
        resp = self.client.get("/api/v1/journal/tasks/?q=dentist")
        self.assertEqual(resp.status_code, 200)
        titles = [t["title"] for t in resp.data]
        self.assertEqual(titles, ["Call the Dentist"])
        # Every result carries the stable UUID id the EntityQuery keys on.
        self.assertIn("id", resp.data[0])

    def test_task_search_real_name_finds_placeholder_stored_row(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        Task.objects.create(tenant=self.tenant, title="Call [PERSON_1]")

        resp = self.client.get("/api/v1/journal/tasks/?q=ALICE")

        self.assertEqual([task["title"] for task in resp.data], ["Call Alice"])

    def test_task_search_cross_product_finds_fully_redacted_multi_name_title(self):
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Alex"},
            "[PERSON_2]": {"name": "Bob"},
        }
        self.tenant.save(update_fields=["pii_entity_map"])
        Task.objects.create(tenant=self.tenant, title="Call [PERSON_1] about [PERSON_2]")

        resp = self.client.get("/api/v1/journal/tasks/?q=call%20Alex%20about%20Bob")

        self.assertEqual([task["title"] for task in resp.data], ["Call Alex about Bob"])

    def test_search_variants_skip_short_values_and_cap_with_log(self):
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Li"},
            **{f"[PERSON_{index + 2}]": {"name": f"Name{index:02d}"} for index in range(25)},
        }
        self.assertEqual(_search_variants(self.tenant, "Call Li"), ["Call Li"])

        query = " ".join(f"Name{index:02d}" for index in range(25))
        with self.assertLogs("apps.journal.lifecycle_views", level="WARNING") as logs:
            variants = _search_variants(self.tenant, query)

        self.assertEqual(len(variants), 20)
        self.assertEqual(variants[0], query)
        self.assertIn("pii_search_variants_capped", logs.output[0])

    def test_list_tasks_q_no_match_returns_empty(self):
        Task.objects.create(tenant=self.tenant, title="Call the Dentist")
        resp = self.client.get("/api/v1/journal/tasks/?q=zzz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_list_tasks_q_scoped_to_tenant(self):
        Task.objects.create(tenant=self.tenant, title="Renew passport")
        Task.objects.create(tenant=self.other_tenant, title="Renew passport")
        resp = self.client.get("/api/v1/journal/tasks/?q=passport")
        self.assertEqual([t["title"] for t in resp.data], ["Renew passport"])

    def test_list_tasks_q_capped_at_20(self):
        for i in range(25):
            Task.objects.create(tenant=self.tenant, title=f"Review doc {i}")
        resp = self.client.get("/api/v1/journal/tasks/?q=review")
        self.assertEqual(len(resp.data), 20)

    def test_get_task_detail(self):
        task = Task.objects.create(tenant=self.tenant, title="read me")
        resp = self.client.get(f"/api/v1/journal/tasks/{task.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["title"], "read me")

    def test_get_task_detail_other_tenant_404(self):
        task = Task.objects.create(tenant=self.other_tenant, title="theirs")
        resp = self.client.get(f"/api/v1/journal/tasks/{task.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_create_task_cross_tenant_parent_goal_rejected(self):
        their_goal = Goal.objects.create(tenant=self.other_tenant, title="their goal")
        resp = self.client.post(
            "/api/v1/journal/tasks/",
            {"title": "sneaky", "parent_goal_id": str(their_goal.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_requires_auth(self):
        anon = APIClient()
        resp = anon.post("/api/v1/journal/tasks/", {"title": "x"}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    # ── Goals ────────────────────────────────────────────────────────

    def test_create_goal(self):
        resp = self.client.post(
            "/api/v1/journal/goals/",
            {"title": "Debt-free", "pillar": "gravity", "target_date": "2027-01-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], Goal.Status.ACTIVE)
        self.assertEqual(Goal.objects.get(id=resp.data["id"]).tenant_id, self.tenant.id)

    def test_list_goals_scoped_to_tenant(self):
        Goal.objects.create(tenant=self.tenant, title="mine")
        Goal.objects.create(tenant=self.other_tenant, title="theirs")
        resp = self.client.get("/api/v1/journal/goals/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([g["title"] for g in resp.data], ["mine"])

    def test_list_goals_filter_by_status(self):
        Goal.objects.create(tenant=self.tenant, title="active one", status=Goal.Status.ACTIVE)
        Goal.objects.create(tenant=self.tenant, title="achieved one", status=Goal.Status.ACHIEVED)
        resp = self.client.get("/api/v1/journal/goals/?status=achieved")
        self.assertEqual([g["title"] for g in resp.data], ["achieved one"])

    def test_list_goals_q_name_search_case_insensitive(self):
        Goal.objects.create(tenant=self.tenant, title="Run a Marathon")
        Goal.objects.create(tenant=self.tenant, title="Learn piano")
        resp = self.client.get("/api/v1/journal/goals/?q=marathon")
        self.assertEqual([g["title"] for g in resp.data], ["Run a Marathon"])
        self.assertIn("id", resp.data[0])

    def test_goal_search_real_name_finds_placeholder_stored_row(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        Goal.objects.create(tenant=self.tenant, title="Support [PERSON_1]")

        resp = self.client.get("/api/v1/journal/goals/?q=alice")

        self.assertEqual([goal["title"] for goal in resp.data], ["Support Alice"])

    def test_list_goals_q_scoped_to_tenant(self):
        Goal.objects.create(tenant=self.tenant, title="Save for a house")
        Goal.objects.create(tenant=self.other_tenant, title="Save for a house")
        resp = self.client.get("/api/v1/journal/goals/?q=house")
        self.assertEqual([g["title"] for g in resp.data], ["Save for a house"])

    def test_list_goals_q_capped_at_20(self):
        for i in range(25):
            Goal.objects.create(tenant=self.tenant, title=f"Milestone {i}")
        resp = self.client.get("/api/v1/journal/goals/?q=milestone")
        self.assertEqual(len(resp.data), 20)


class TaskProjectResolutionTests(TestCase):
    """Owner task responses carry the project a ``related_ref`` points at."""

    def setUp(self):
        self.user = User.objects.create_user(username="projectuser", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.other_user = User.objects.create_user(username="projectother", password="x")
        self.other_tenant = Tenant.objects.create(user=self.other_user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _project(self, title="Garden"):
        goal = SharedGoal.objects.create(title=title, created_by=self.tenant)
        SharedGoalMembership.objects.create(
            shared_goal=goal,
            tenant=self.tenant,
            user=self.user,
            role="owner",
            status="active",
        )
        step = SharedGoalStep.objects.create(shared_goal=goal, title="Dig", created_by=self.tenant)
        return goal, step

    def _step_ref(self, step):
        return {"pillar": "neighborhood", "object_type": "SharedGoalStep", "object_id": str(step.id)}

    def _goal_ref(self, goal):
        return {"pillar": "friends", "object_type": "shared_goal", "object_id": str(goal.id)}

    def _listed(self, task):
        resp = self.client.get("/api/v1/journal/tasks/")
        self.assertEqual(resp.status_code, 200)
        return next(row for row in resp.data if row["id"] == str(task.id))

    def test_list_step_pointer_resolves_project_and_step(self):
        goal, step = self._project()
        task = Task.objects.create(tenant=self.tenant, title="Dig the beds", related_ref=self._step_ref(step))

        row = self._listed(task)

        self.assertEqual(row["project"], {"id": str(goal.id), "title": "Garden", "step_id": str(step.id)})

    def test_list_shared_goal_pointer_has_no_step_id(self):
        goal, _ = self._project()
        task = Task.objects.create(tenant=self.tenant, title="Plan the beds", related_ref=self._goal_ref(goal))

        row = self._listed(task)

        self.assertEqual(row["project"], {"id": str(goal.id), "title": "Garden", "step_id": None})

    def test_list_plain_task_has_no_project(self):
        task = Task.objects.create(tenant=self.tenant, title="Call the dentist")

        row = self._listed(task)

        self.assertIsNone(row["project"])

    def test_list_missing_step_has_no_project(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="Orphaned step",
            related_ref={"pillar": "neighborhood", "object_type": "SharedGoalStep", "object_id": str(uuid.uuid4())},
        )

        row = self._listed(task)

        self.assertIsNone(row["project"])

    def test_list_project_without_membership_has_no_project(self):
        goal = SharedGoal.objects.create(title="Not mine", created_by=self.other_tenant)
        SharedGoalMembership.objects.create(
            shared_goal=goal,
            tenant=self.other_tenant,
            user=self.other_user,
            role="owner",
            status="active",
        )
        step = SharedGoalStep.objects.create(shared_goal=goal, title="Dig", created_by=self.other_tenant)
        task = Task.objects.create(tenant=self.tenant, title="Someone else's step", related_ref=self._step_ref(step))

        row = self._listed(task)

        self.assertIsNone(row["project"])

    def test_detail_gets_same_project_as_list(self):
        goal, step = self._project()
        task = Task.objects.create(tenant=self.tenant, title="Dig the beds", related_ref=self._step_ref(step))

        listed = self._listed(task)
        resp = self.client.get(f"/api/v1/journal/tasks/{task.id}/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["project"], listed["project"])

    def test_runtime_list_omits_project_key_while_owner_list_keeps_it(self):
        goal, step = self._project()
        task = Task.objects.create(tenant=self.tenant, title="Dig the beds", related_ref=self._step_ref(step))

        owner_row = self._listed(task)
        self.assertIn("project", owner_row)
        self.assertEqual(owner_row["project"]["id"], str(goal.id))

        seed_internal_key(self.tenant, key="runtime-project-key")
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/",
            HTTP_X_NBHD_INTERNAL_KEY="runtime-project-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

        self.assertEqual(resp.status_code, 200)
        runtime_row = next(row for row in resp.data["tasks"] if row["id"] == str(task.id))
        self.assertNotIn("project", runtime_row)

    def _list_query_count(self, task_count):
        Task.objects.filter(tenant=self.tenant).delete()
        goal, _ = self._project()
        for i in range(task_count):
            step = SharedGoalStep.objects.create(shared_goal=goal, title=f"Step {i}", created_by=self.tenant)
            Task.objects.create(tenant=self.tenant, title=f"Task {i}", related_ref=self._step_ref(step))

        with CaptureQueriesContext(connection) as captured:
            resp = self.client.get("/api/v1/journal/tasks/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), task_count)
        self.assertTrue(all(row["project"] is not None for row in resp.data))
        return len(captured)

    def test_list_query_count_is_flat_across_project_linked_tasks(self):
        one = self._list_query_count(1)
        five = self._list_query_count(5)

        self.assertEqual(one, five)


class TaskProjectRealRlsTests(TransactionTestCase):
    """The owner task list resolves a project under production-like FORCE RLS.

    Django connects as a BYPASSRLS superuser locally, so ``SET LOCAL ROLE
    app_user`` recreates the bound runtime role. The request middleware's SET is
    patched out because it is session-scoped in production: over the
    transaction-mode pooler it is not visible to later statements, which is the
    exact failure this accessor's transaction fixes.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="project_rls_user", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        goal = SharedGoal.objects.create(title="Garden", created_by=self.tenant)
        SharedGoalMembership.objects.create(
            shared_goal=goal,
            tenant=self.tenant,
            user=self.user,
            role="owner",
            status="active",
        )
        step = SharedGoalStep.objects.create(shared_goal=goal, title="Dig", created_by=self.tenant)
        self.task = Task.objects.create(
            tenant=self.tenant,
            title="Dig the beds",
            related_ref={"pillar": "neighborhood", "object_type": "SharedGoalStep", "object_id": str(step.id)},
        )
        self.goal = goal
        self.step = step
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @contextmanager
    def _app_user(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND rowsecurity")
                for (table,) in cursor.fetchall():
                    if table not in RLS_KEEP_ENABLED:
                        cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} DISABLE ROW LEVEL SECURITY")
                cursor.execute("GRANT USAGE ON SCHEMA public TO app_user")
                cursor.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO app_user")
                cursor.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app_user")
                cursor.execute("SET LOCAL ROLE app_user")
            try:
                yield
            finally:
                transaction.set_rollback(True)

    def test_list_resolves_project_under_real_app_user_rls(self):
        with (
            patch("apps.tenants.middleware.TenantContextMiddleware.process_request", return_value=None),
            self._app_user(),
        ):
            resp = self.client.get("/api/v1/journal/tasks/")

            self.assertEqual(resp.status_code, 200)
            row = next(row for row in resp.data if row["id"] == str(self.task.id))
            self.assertEqual(
                row["project"],
                {"id": str(self.goal.id), "title": "Garden", "step_id": str(self.step.id)},
            )
