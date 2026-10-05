"""Real authoring/signals, with only neural inference and external pushes stubbed."""

import json
from functools import partial
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.test import APIClient

from apps.journal.models import Goal, PendingExtraction, Task

from . import access, services
from . import project_assistant as assistant
from . import project_services as projects
from .models import PendingProjectAction, SharedGoalStep
from .project_contracts import ProjectDraftSpec, ProjectProposalSpec
from .project_flags import projects_v2_enabled
from .test_pr6 import _edge, _profile, _tenant


def legacy_approve(tenant, user, proposal_id):
    """Pre-optimization orchestration, retained as the equivalence oracle."""
    with transaction.atomic():
        row = access.my_project_proposals(tenant).select_for_update().get(id=proposal_id)
        row.status, row.resolved_at = "approved", timezone.now()
        row.save(update_fields=["status", "resolved_at"])
    goal, member = assistant._project_for(tenant, row.shared_goal_id)
    results = []
    for change in ProjectProposalSpec.model_validate(row.payload).changes:
        try:
            assistant._check_change(tenant, goal, change)
            outcome = assistant._apply(tenant, user, goal, member, change)
            results.append({"change": change.kind, "outcome": outcome})
        except APIException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            results.append({"change": change.kind, "outcome": "skipped", "reason": detail[:200]})
    row.status = "approved" if all(r["outcome"] == "applied" for r in results) else "partial"
    row.result = {"changes": results}
    row.save(update_fields=["status", "result"])
    return {"proposal_id": str(row.id), "status": row.status, "changes": results}


def legacy_publish_draft(tenant, user, draft_id, extra_member_friendship_ids=None) -> dict:
    """The HUMAN starts the project from a draft: invites the suggested owners (and
    any extra neighbors), lays out milestones/steps/dependencies, asks each owner.
    Owners still answer every ask; "me" steps are taken by the human."""
    draft = access.my_project_drafts(tenant).filter(id=draft_id).first()
    if draft is None:
        raise AssertionError("Missing fixture draft")
    spec = ProjectDraftSpec.model_validate(draft.payload)
    assistant._validate_draft_people(tenant, spec)
    neighbors = access.my_neighbor_edges_by_handle(tenant)
    owner_handles = {(s.owner or "").lstrip("@").lower() for s in spec.steps} - {"", "me"}
    friendship_ids = {str(neighbors[h].id) for h in owner_handles}
    friendship_ids |= {str(f) for f in (extra_member_friendship_ids or [])}
    mine: list[str] = []
    skipped: list[str] = []
    with transaction.atomic():
        goal = services.create_mission(
            tenant, user, member_friendship_ids=sorted(friendship_ids), title=spec.title, description=spec.goal
        )
        milestone_ids = {}
        for index, m in enumerate(spec.milestones):
            row = projects.milestone_write(
                tenant,
                goal.id,
                {"title": m.title, "target_date": m.target_date.isoformat() if m.target_date else None, "order": index},
            )
            milestone_ids[m.key] = row.id
        step_ids = {}
        for index, s in enumerate(spec.steps):
            row = projects.create_step(
                tenant,
                user,
                goal.id,
                {
                    "title": s.title,
                    "description": s.description,
                    "start_date": s.start_date.isoformat() if s.start_date else None,
                    "due_date": s.due_date.isoformat() if s.due_date else None,
                    "milestone_id": str(milestone_ids[s.milestone_key]) if s.milestone_key else None,
                    "order": index,
                },
            )
            step_ids[s.key] = row.id
        for s in spec.steps:
            for dep in s.depends_on:
                projects.dependency_write(
                    tenant, goal.id, {"blocker_id": str(step_ids[dep]), "blocked_id": str(step_ids[s.key])}
                )
        members = {
            str(m.tenant_id): m for m in access.mission_memberships().filter(shared_goal=goal).select_related("tenant")
        }
        by_handle = {
            h: members.get(str(e.addressee_id if e.requester_id == tenant.id else e.requester_id))
            for h, e in neighbors.items()
        }
        me = members[str(tenant.id)]
        for s in spec.steps:
            owner = (s.owner or "").lstrip("@").lower()
            if not owner:
                continue
            if owner == "me":
                projects.ask(tenant, user, goal.id, step_ids[s.key], [str(me.id)])
                mine.append(str(step_ids[s.key]))
                continue
            member = by_handle.get(owner)
            if member is None or not projects_v2_enabled(member.tenant):
                skipped.append(f"@{owner}: {s.title}")
                continue
            projects.ask(tenant, user, goal.id, step_ids[s.key], [str(member.id)])
        draft.published_goal = goal
        draft.save(update_fields=["published_goal"])
    # Taking my own steps authors a journal Task — outside the transaction (lease rule).
    for step_id in mine:
        try:
            projects.respond(tenant, user, goal.id, step_id, {"answer": "yes"})
        except APIException:
            pass
    return {"mission_id": str(goal.id), "not_asked": skipped}


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ApprovalPerformanceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = _tenant("approval_perf")
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        self.goal = self.new_project()

    def new_project(self):
        return services.create_mission(
            self.tenant, self.tenant.user, member_friendship_ids=[], title="Passport Renewal"
        )

    def changes(self, count):
        return [
            {
                "kind": "add_step",
                "title": f"Renewal step {index}",
                "owner": "me",
                "start_date": "2026-10-06",
                "due_date": "2026-10-10",
            }
            for index in range(count)
        ]

    def proposal(self, changes, goal=None):
        return assistant.propose(self.tenant, (goal or self.goal).id, {"summary": "Renew", "changes": changes})[
            "proposal_id"
        ]

    def measure(self, fn):
        detector = Mock(return_value=[])
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            patch("apps.orchestrator.workspace_envelope.push_user_md") as push,
            CaptureQueriesContext(connection) as queries,
            self.captureOnCommitCallbacks(execute=True) as callbacks,
        ):
            result = fn()
        return result, len(queries), detector.call_count, push.call_count, len(callbacks)

    def approve(self, proposal_id):
        return assistant.approve(self.tenant, self.tenant.user, proposal_id)

    def test_eight_own_steps_have_bounded_cost_and_small_query_slope(self):
        counts = []
        for count in (1, 4, 8):
            proposal_id = self.proposal(self.changes(count))
            result, queries, detector, pushes, scheduled = self.measure(partial(self.approve, proposal_id))
            self.assertEqual(result["status"], "approved")
            self.assertLessEqual(queries, 20 + 7 * count)
            self.assertEqual(detector, count)
            self.assertEqual(pushes, 1)
            self.assertEqual(scheduled, 1)
            counts.append(queries)
            print(f"APPROVAL {count}: queries={queries} detector={detector} pushes={pushes} scheduled={scheduled}")
        self.assertLessEqual(counts[-1] - counts[0], 7 * 7)
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 13)

    def test_duplicate_titles_reuse_inference_only_inside_this_approval(self):
        changes = self.changes(8)
        for change in changes:
            change["title"] = "Collect paperwork"
        for _ in range(2):
            proposal_id = self.proposal(changes)
            _, _, detector, pushes, _ = self.measure(partial(self.approve, proposal_id))
            self.assertEqual(detector, 1)
            self.assertEqual(pushes, 1)

    def test_draft_publishing_uses_the_same_bounded_path(self):
        draft = assistant.create_draft(
            self.tenant,
            {
                "title": "Passport Renewal",
                "steps": [
                    {"key": f"s{i}", **{k: v for k, v in c.items() if k != "kind"}, "description": "Shared only"}
                    for i, c in enumerate(self.changes(8))
                ],
            },
        )
        result, queries, detector, pushes, scheduled = self.measure(
            lambda: assistant.publish_draft(self.tenant, self.tenant.user, draft["draft_id"])
        )
        self.assertLessEqual(queries, 90)
        self.assertEqual((detector, pushes, scheduled), (8, 1, 1))
        goal = access.get_mission(result["mission_id"])
        self.assertEqual(access.project_assignments(goal).filter(status="accepted").count(), 8)
        self.assertEqual(set(Task.objects.filter(tenant=self.tenant).values_list("description", flat=True)), {""})
        print(f"PUBLISH 8: queries={queries} detector={detector} pushes={pushes} scheduled={scheduled}")

    def snapshot(self, goal, proposal_id=None):
        """Compare every persisted value, normalizing generated IDs and timestamps."""
        steps = list(access.project_steps(goal).order_by("title"))
        members = dict(access.mission_memberships().filter(shared_goal=goal).values_list("id", "tenant_id"))
        ids = {str(s.id): s.title for s in steps} | {str(k): str(v) for k, v in members.items()}
        ids.update({str(m.id): m.title for m in access.project_milestones(goal)})
        ids[str(goal.id)] = "project"
        assignments = list(access.project_assignments(goal).select_related("task"))
        for assignment in assignments:
            if assignment.task_id:
                ids[str(assignment.task_id)] = "task:" + ids[str(assignment.step_id)]

        def normalize(value):
            if isinstance(value, dict):
                return {k: normalize(v) for k, v in value.items()}
            if isinstance(value, list):
                return [normalize(v) for v in value]
            return ids.get(str(value), value)

        def rows(qs, ignored, *, chronological=False):
            if chronological:
                qs = qs.order_by("created_at", "id")
            values = [normalize({k: v for k, v in row.items() if k not in ignored}) for row in qs.values()]
            return (
                values
                if chronological
                else sorted(values, key=lambda row: json.dumps(row, default=str, sort_keys=True))
            )

        # Non-null timestamps are also asserted below; clock instants naturally differ.
        for a in assignments:
            self.assertIsNotNone(a.asked_at)
            if a.status == "accepted":
                self.assertIsNotNone(a.responded_at)
        proposal = PendingProjectAction.objects.get(id=proposal_id) if proposal_id else None
        return {
            "steps": rows(access.project_steps(goal), {"id"}),
            "assignments": rows(access.project_assignments(goal), {"id", "asked_at", "responded_at"}),
            "dependencies": rows(access.project_dependencies(goal), {"id"}),
            "milestones": rows(access.project_milestones(goal), {"id", "reached_at"}),
            "tasks": rows(
                Task.objects.filter(id__in=[a.task_id for a in assignments if a.task_id]),
                {"id", "created_at", "updated_at"},
            ),
            "audit": rows(access.mission_updates().filter(shared_goal=goal), {"id", "created_at"}, chronological=True),
            "proposal": (proposal.status, proposal.result, proposal.resolved_at is not None) if proposal else None,
        }

    def test_rows_privacy_receipts_and_result_match_legacy_with_dependencies(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        linked_goal = Goal.objects.create(tenant=self.tenant, title="Travel")
        snapshots, measurements = [], []
        for apply in (legacy_approve, assistant.approve):
            goal = self.new_project()
            projects.set_linked_goal(self.tenant, goal.id, linked_goal.id)
            blocker = projects.create_step(self.tenant, self.tenant.user, goal.id, {"title": "Existing blocker"})
            changes = self.changes(8)
            changes[0]["title"] = "Ask Alice for forms"
            for change in changes:
                change["waits_on"] = [str(blocker.id)]
            proposal_id = self.proposal(changes, goal)
            _, queries, detector, pushes, _ = self.measure(partial(apply, self.tenant, self.tenant.user, proposal_id))
            snapshots.append(self.snapshot(goal, proposal_id))
            measurements.append((queries, detector, pushes))
            self.assertEqual(access.project_assignments(goal).filter(status="accepted").count(), 8)
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertIn("[PERSON_1]", str(snapshots[1]["tasks"]))
        self.assertTrue(all("Alice" not in row["title"] for row in snapshots[1]["tasks"]))
        self.assertLessEqual(measurements[1][0], 90)
        self.assertEqual(measurements[1][2], 1)
        print(f"EQUIVALENCE with 8 dependencies: legacy={measurements[0]} batch={measurements[1]}")

    def test_refused_change_stays_isolated_and_claim_cannot_be_replayed(self):
        changes = self.changes(3)
        proposal_id = self.proposal(changes)
        row = PendingProjectAction.objects.get(id=proposal_id)
        row.payload["changes"][1]["waits_on"] = [str(uuid4())]
        row.save(update_fields=["payload"])
        result, *_ = self.measure(partial(self.approve, proposal_id))
        self.assertEqual(result["status"], "partial")
        self.assertEqual([r["outcome"] for r in result["changes"]], ["applied", "skipped", "applied"])
        self.assertEqual(access.project_steps(self.goal).count(), 2)
        with self.assertRaises(ValidationError):
            self.approve(proposal_id)

    def test_step_cap_refuses_only_excess(self):
        SharedGoalStep.objects.bulk_create(
            [SharedGoalStep(shared_goal=self.goal, created_by=self.tenant, title=f"Existing {i}") for i in range(59)]
        )
        proposal_id = self.proposal(self.changes(3))
        result, *_ = self.measure(partial(self.approve, proposal_id))
        self.assertEqual([r["outcome"] for r in result["changes"]], ["applied", "skipped", "skipped"])
        self.assertEqual(access.project_steps(self.goal).count(), 60)

    def test_authoring_refusal_preserves_created_step_and_unanswered_ask(self):
        original = services._prepare_member_task

        def prepare(tenant, title, description):
            if title == "Renewal step 1":
                raise ValidationError("Cannot author this task")
            return original(tenant, title, description)

        proposal_id = self.proposal(self.changes(3))
        with patch("apps.friends.services._prepare_member_task", side_effect=prepare):
            result, *_ = self.measure(partial(self.approve, proposal_id))
        self.assertEqual([r["outcome"] for r in result["changes"]], ["applied", "skipped", "applied"])
        self.assertEqual(access.project_steps(self.goal).count(), 3)
        self.assertEqual(access.project_assignments(self.goal).filter(status="asked", task=None).count(), 1)

    def test_membership_is_rechecked_after_authoring(self):
        proposal_id = self.proposal(self.changes(2))
        original = projects.prepare_step_tasks

        def prepare(tenant, items):
            prepared = original(tenant, items)
            access.mission_memberships().filter(shared_goal=self.goal, tenant=tenant).update(status="left")
            return prepared

        with patch("apps.friends.project_services.prepare_step_tasks", side_effect=prepare):
            result, *_ = self.measure(partial(self.approve, proposal_id))
        self.assertEqual(result["status"], "partial")
        self.assertTrue(all(r["outcome"] == "skipped" for r in result["changes"]))
        self.assertEqual(access.project_steps(self.goal).count(), 0)

    def test_journal_save_receivers_still_resolve_pending_extractions(self):
        pending = PendingExtraction.objects.create(
            tenant=self.tenant, kind="task", text="Renewal step 0", expires_at=timezone.now() + assistant.DRAFT_TTL
        )
        proposal_id = self.proposal(self.changes(1))
        self.measure(partial(self.approve, proposal_id))
        pending.refresh_from_db()
        self.assertEqual(pending.status, "approved")

    def test_final_refresh_includes_other_active_members_without_self_notifications(self):
        other = _tenant("approval_other")
        edge = _edge(self.tenant, other)
        projects.add_members(self.tenant, self.goal.id, [str(edge.id)])
        services.join_mission(other, other.user, self.goal.id)
        proposal_id = self.proposal(self.changes(8))
        with patch("apps.router.push_views._push_to_user_devices") as notify:
            _, _, _, pushes, scheduled = self.measure(partial(self.approve, proposal_id))
        self.assertEqual((pushes, scheduled), (2, 2))
        notify.assert_not_called()

    def test_mixed_proposal_relinks_existing_tasks_and_uses_new_link_for_later_tasks(self):
        linked = Goal.objects.create(tenant=self.tenant, title="Travel")
        first, second = self.changes(2)
        proposal_id = self.proposal([first, {"kind": "link_goal", "goal_id": str(linked.id)}, second])
        result, *_ = self.measure(partial(self.approve, proposal_id))
        self.assertEqual(result["status"], "approved")
        self.assertEqual(Task.objects.get(tenant=self.tenant, title=first["title"]).parent_goal_id, linked.id)
        self.assertEqual(Task.objects.get(tenant=self.tenant, title=second["title"]).parent_goal_id, linked.id)

    def test_milestone_audit_transitions_match_legacy(self):
        snapshots = []
        for apply in (legacy_approve, assistant.approve):
            goal = self.new_project()
            milestone = projects.milestone_write(self.tenant, goal.id, {"title": "Forms ready"})
            # A stale, unreached milestone whose existing step is done. The first
            # unrelated addition reaches it; the second addition opens it again.
            SharedGoalStep.objects.create(
                shared_goal=goal, created_by=self.tenant, title="Finished", status="done", milestone=milestone
            )
            changes = self.changes(2)
            changes[1]["milestone_id"] = str(milestone.id)
            proposal_id = self.proposal(changes, goal)
            self.measure(partial(apply, self.tenant, self.tenant.user, proposal_id))
            milestone.refresh_from_db()
            self.assertIsNone(milestone.reached_at)
            self.assertEqual(access.mission_updates().filter(shared_goal=goal, kind="milestone_reached").count(), 1)
            snapshots.append(self.snapshot(goal, proposal_id))
        self.assertEqual(*snapshots)

    def test_dependency_cap_keeps_legacy_partial_step_and_continues(self):
        snapshots = []
        for apply in (legacy_approve, assistant.approve):
            goal = self.new_project()
            steps = [SharedGoalStep(shared_goal=goal, created_by=self.tenant, title=f"Existing {i}") for i in range(16)]
            access.project_steps(goal).bulk_create(steps)
            edge_model = access.project_dependencies(goal).model
            access.project_dependencies(goal).bulk_create(
                [edge_model(blocker=a, blocked=b) for i, a in enumerate(steps) for b in steps[i + 1 :]]
            )
            changes = self.changes(2)
            changes[0]["waits_on"] = [str(steps[0].id)]
            proposal_id = self.proposal(changes, goal)
            result, *_ = self.measure(partial(apply, self.tenant, self.tenant.user, proposal_id))
            self.assertEqual([r["outcome"] for r in result["changes"]], ["skipped", "applied"])
            self.assertEqual(access.project_steps(goal).count(), 18)
            self.assertEqual(access.project_assignments(goal).count(), 1)
            snapshots.append(self.snapshot(goal, proposal_id))
        self.assertEqual(*snapshots)

    def test_privacy_authoring_precedes_write_transactions_in_both_paths(self):
        original = services._prepare_member_task
        depth = len(connection.atomic_blocks)

        def prepare(*args):
            self.assertEqual(len(connection.atomic_blocks), depth)
            return original(*args)

        proposal_id = self.proposal(self.changes(1))
        draft = assistant.create_draft(
            self.tenant,
            {
                "title": "Draft",
                "steps": [{"key": "a", "title": "Prepare forms", "owner": "me"}],
            },
        )
        with patch("apps.friends.services._prepare_member_task", side_effect=prepare):
            self.measure(partial(self.approve, proposal_id))
            self.measure(partial(assistant.publish_draft, self.tenant, self.tenant.user, draft["draft_id"]))

    def test_publish_keeps_unanswered_ask_when_private_authoring_is_refused(self):
        draft = assistant.create_draft(
            self.tenant,
            {
                "title": "Draft",
                "steps": [{"key": "a", "title": "Prepare forms", "owner": "me"}],
            },
        )
        with patch("apps.friends.services._prepare_member_task", side_effect=ValidationError("refused")):
            result, *_ = self.measure(
                partial(assistant.publish_draft, self.tenant, self.tenant.user, draft["draft_id"])
            )
        goal = access.get_mission(result["mission_id"])
        assignment = access.project_assignments(goal).get()
        self.assertEqual(assignment.status, "asked")
        self.assertIsNone(assignment.task_id)

    def test_refresh_is_after_commit_and_observes_all_accepted_assignments(self):
        proposal_id = self.proposal(self.changes(8))
        detector = Mock(return_value=[])
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            patch("apps.orchestrator.workspace_envelope.push_user_md") as push,
            self.captureOnCommitCallbacks(execute=False) as callbacks,
        ):
            self.approve(proposal_id)
            push.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        with patch("apps.orchestrator.workspace_envelope.push_user_md") as push:
            callbacks[0]()
            push.assert_called_once()
        self.assertEqual(access.project_assignments(self.goal).filter(status="accepted").count(), 8)

    def test_approval_endpoint_cost(self):
        proposal_id = self.proposal(self.changes(8))
        client = APIClient()
        client.force_authenticate(user=self.tenant.user)
        response, queries, detector, pushes, scheduled = self.measure(
            partial(client.post, f"/api/v1/friends/project-proposals/{proposal_id}/approve/", {}, format="json")
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "approved")
        self.assertLessEqual(queries, 90)
        self.assertEqual((detector, pushes, scheduled), (8, 1, 1))
        print(f"APPROVAL POST 8: queries={queries} detector={detector} pushes={pushes}")

    def test_eight_step_approval_order_and_counts_match_legacy(self):
        snapshots, measurements = [], []
        for apply in (legacy_approve, assistant.approve):
            goal = self.new_project()
            proposal_id = self.proposal(self.changes(8), goal)
            _, queries, detector, pushes, _ = self.measure(partial(apply, self.tenant, self.tenant.user, proposal_id))
            snapshots.append(self.snapshot(goal, proposal_id))
            measurements.append((queries, detector, pushes))
        self.assertEqual(*snapshots)
        kinds = [r["kind"] for r in snapshots[1]["audit"] if r["kind"].startswith("step_")]
        self.assertEqual(kinds, ["step_added", "step_assigned", "step_answered"] * 8)
        print(f"APPROVAL EQUIVALENCE 8: legacy={measurements[0]} batch={measurements[1]}")

    def test_unexpected_acceptance_failure_keeps_other_items_and_unanswered_ask(self):
        proposal_id = self.proposal(self.changes(3))
        mint = services._mint_member_task
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            task = mint(*args, **kwargs)
            if calls == 2:
                raise RuntimeError("private synthetic error after Task save")
            return task

        with patch.object(services, "_mint_member_task", side_effect=fail_second):
            result, *_ = self.measure(partial(self.approve, proposal_id))
        self.assertEqual(result["status"], "partial")
        self.assertEqual([r["outcome"] for r in result["changes"]], ["applied", "skipped", "applied"])
        self.assertTrue(result["changes"][1]["reason"])
        self.assertNotIn("private synthetic", str(result))
        self.assertEqual(access.project_steps(self.goal).count(), 3)
        self.assertEqual(access.project_assignments(self.goal).count(), 3)
        self.assertEqual(access.project_assignments(self.goal).filter(status="accepted").count(), 2)
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 2)
        asked = access.project_assignments(self.goal).get(status="asked")
        self.assertIsNone(asked.task_id)
        self.assertEqual(asked.step.title, "Renewal step 1")
        self.assertEqual(PendingProjectAction.objects.get(id=proposal_id).result, {"changes": result["changes"]})

    def test_bulk_audit_timestamp_ties_keep_approval_order(self):
        proposal_id = self.proposal(self.changes(3))
        timestamp = timezone.now()
        field = access.mission_updates().model._meta.get_field("created_at")

        def stamp(instance, add):
            instance.created_at = timestamp
            return timestamp

        with patch.object(field, "pre_save", side_effect=stamp):
            self.measure(partial(self.approve, proposal_id))
        updates = list(
            access.mission_updates()
            .filter(shared_goal=self.goal, kind__startswith="step_")
            .order_by("created_at", "id")
        )
        self.assertEqual([u.kind for u in updates], ["step_added", "step_assigned", "step_answered"] * 3)
        self.assertTrue(all(a.created_at < b.created_at for a, b in zip(updates, updates[1:])))

    def test_unexpected_item_write_failure_rolls_back_only_that_item(self):
        proposal_id = self.proposal(self.changes(3))
        create = QuerySet.create

        def fail_second_step(qs, **kwargs):
            row = create(qs, **kwargs)
            if qs.model is SharedGoalStep and row.title == "Renewal step 1":
                raise RuntimeError("synthetic item write failure")
            return row

        with patch.object(QuerySet, "create", new=fail_second_step):
            result, *_ = self.measure(partial(self.approve, proposal_id))
        self.assertEqual([r["outcome"] for r in result["changes"]], ["applied", "skipped", "applied"])
        self.assertEqual(access.project_steps(self.goal).count(), 2)
        self.assertEqual(access.project_assignments(self.goal).filter(status="accepted").count(), 2)
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 2)
        self.assertEqual(access.mission_updates().filter(shared_goal=self.goal, kind="step_added").count(), 2)

    def test_whole_batch_rollback_records_honest_partial_result(self):
        proposal_id = self.proposal(self.changes(3))
        bulk_create = QuerySet.bulk_create

        def fail_audits(qs, *args, **kwargs):
            rows = bulk_create(qs, *args, **kwargs)
            if qs.model is access.mission_updates().model:
                raise RuntimeError("synthetic batch persistence failure")
            return rows

        with patch.object(QuerySet, "bulk_create", new=fail_audits):
            result, *_ = self.measure(partial(self.approve, proposal_id))
        self.assertEqual(result["status"], "partial")
        self.assertEqual([r["outcome"] for r in result["changes"]], ["skipped"] * 3)
        self.assertTrue(all(r["reason"] for r in result["changes"]))
        self.assertEqual(access.project_steps(self.goal).count(), 0)
        self.assertEqual(access.project_assignments(self.goal).count(), 0)
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 0)
        self.assertFalse(access.mission_updates().filter(shared_goal=self.goal, kind="step_added").exists())
        row = PendingProjectAction.objects.get(id=proposal_id)
        self.assertEqual(row.status, "partial")
        self.assertEqual(row.result, {"changes": result["changes"]})

    def test_publication_rows_and_audit_order_match_legacy(self):
        for mixed in (False, True):
            if mixed:
                other = _tenant("publication_neighbor")
                _profile(other, "neighbor")
                _edge(self.tenant, other)
            snapshots, measurements = [], []
            for publish in (legacy_publish_draft, assistant.publish_draft):
                steps = [
                    {"key": f"s{i}", **{k: v for k, v in c.items() if k != "kind"}}
                    for i, c in enumerate(self.changes(8))
                ]
                payload = {"title": "Passport Renewal", "steps": steps}
                if mixed:
                    steps[1]["owner"] = "neighbor"
                    steps[3]["owner"] = None
                    steps[6]["depends_on"] = ["s0"]
                    payload["milestones"] = [{"key": "forms", "title": "Forms ready"}]
                    steps[0]["milestone_key"] = "forms"
                draft = assistant.create_draft(self.tenant, payload)
                result, queries, detector, pushes, _ = self.measure(
                    partial(publish, self.tenant, self.tenant.user, draft["draft_id"])
                )
                goal = access.get_mission(result["mission_id"])
                snapshots.append(self.snapshot(goal))
                measurements.append((queries, detector, pushes))
            self.assertEqual(
                [(r["kind"], r["payload"]) for r in snapshots[0]["audit"]],
                [(r["kind"], r["payload"]) for r in snapshots[1]["audit"]],
            )
            self.assertEqual(*snapshots)
            kinds = [r["kind"] for r in snapshots[1]["audit"] if r["kind"].startswith("step_")]
            self.assertEqual(kinds, sorted(kinds, key={"step_added": 0, "step_assigned": 1, "step_answered": 2}.get))
            print(f"PUBLISH EQUIVALENCE mixed={mixed}: legacy={measurements[0]} batch={measurements[1]}")

    def test_notification_delivers_once_to_other_member_and_never_self(self):
        other = _tenant("notification_neighbor")
        friendship = _edge(self.tenant, other)
        goal = services.create_mission(
            self.tenant, self.tenant.user, member_friendship_ids=[str(friendship.id)], title="Renewal"
        )
        step = projects.create_step(self.tenant, self.tenant.user, goal.id, {"title": "Forms"})
        members = list(access.mission_memberships().filter(shared_goal=goal).values_list("id", flat=True))
        with (
            patch("apps.common.apns.apns_configured", return_value=True),
            patch("apps.router.push_views._push_to_user_devices") as deliver,
            self.captureOnCommitCallbacks(execute=True),
        ):
            projects.ask(self.tenant, self.tenant.user, goal.id, step.id, [str(pk) for pk in members])
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args.args[0].id, other.user_id)


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class PublicationCommitTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.tenant = _tenant("publish_commit")
        self.other = _tenant("publish_commit_other")
        self.friendship = _edge(self.tenant, self.other)
        self.draft = assistant.create_draft(
            self.tenant, {"title": "Renewal", "steps": [{"key": "a", "title": "Forms", "owner": "me"}]}
        )

    def publish(self):
        return assistant.publish_draft(self.tenant, self.tenant.user, self.draft["draft_id"], [str(self.friendship.id)])

    def test_failing_commit_callback_still_refreshes_every_published_member(self):
        assign = projects.assign_published_steps

        def register_failure(*args, **kwargs):
            assign(*args, **kwargs)

            def fail():
                raise RuntimeError("commit callback failed")

            transaction.on_commit(fail)

        with (
            patch.object(projects, "assign_published_steps", side_effect=register_failure),
            patch("apps.friends.envelope._schedule_recipient_push") as refresh,
            self.assertRaisesMessage(RuntimeError, "commit callback failed"),
        ):
            self.publish()
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual({str(c.args[0]) for c in refresh.call_args_list}, {str(self.tenant.id), str(self.other.id)})
        self.assertFalse(access.my_project_drafts(self.tenant).filter(id=self.draft["draft_id"]).exists())
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 1)

    def test_rolled_back_publication_does_not_schedule_refresh(self):
        with (
            patch.object(projects, "assign_published_steps", side_effect=RuntimeError("rollback")),
            patch("apps.friends.envelope._schedule_recipient_push") as refresh,
            self.assertRaisesMessage(RuntimeError, "rollback"),
        ):
            self.publish()
        refresh.assert_not_called()
        self.assertTrue(access.my_project_drafts(self.tenant).filter(id=self.draft["draft_id"]).exists())
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 0)

    @override_settings(NBHD_DISABLE_BACKGROUND_THREADS=False)
    def test_notification_thread_start_failure_is_best_effort(self):
        with (
            patch("apps.friends.project_notifications.threading.Thread") as thread,
            patch("apps.friends.envelope._schedule_recipient_push") as refresh,
            self.assertLogs("apps.friends.project_notifications", level="ERROR") as logs,
        ):
            thread.return_value.start.side_effect = RuntimeError("thread start failed")
            result = self.publish()
        self.assertTrue(result["mission_id"])
        self.assertEqual(refresh.call_count, 2)
        self.assertTrue(any("project push thread start failed" in line for line in logs.output))
