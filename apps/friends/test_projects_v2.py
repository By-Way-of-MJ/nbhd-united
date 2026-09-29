"""P1a API authorization, consent, mirror, limit, migration and RLS contracts."""

import importlib
import uuid
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.journal.models import Task
from apps.tenants.serializers import TenantSerializer

from . import access, services
from . import project_services as projects
from .models import SharedGoalMembership, SharedGoalStep, SharedGoalStepAssignment, SharedGoalUpdate
from .test_pr6 import _edge, _profile, _tenant


@override_settings(PROJECTS_V2_TENANT_IDS="*")
class ProjectAPITests(TestCase):
    def setUp(self):
        self.a, self.b, self.c = [_tenant("proj_" + n) for n in "abc"]
        for tenant, handle in [(self.a, "aya"), (self.b, "ben"), (self.c, "cleo")]:
            _profile(tenant, handle)
        self.edge = _edge(self.a, self.b)
        self.goal = services.create_mission(
            self.a, self.a.user, member_friendship_ids=[str(self.edge.id)], title="Garden"
        )
        services.join_mission(self.b, self.b.user, self.goal.id)
        self.member_a = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.a)
        self.member_b = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.b)
        self.base = f"/api/v1/friends/missions/{self.goal.id}/"
        self.client = self.jwt(self.a)

    def jwt(self, tenant):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(tenant.user).access_token}")
        return client

    def step(self, **data):
        return projects.create_step(self.a, self.a.user, self.goal.id, {"title": "Dig", **data})

    def ask(self, step, tenant=None):
        member = self.member_b if tenant is None else self.member_a
        projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(member.id)])

    def accept(self, step, tenant=None):
        tenant = tenant or self.b
        return projects.respond(tenant, tenant.user, self.goal.id, step.id, {"answer": "yes"})

    def test_multi_member_creation_preview_join_and_legacy(self):
        edge2 = _edge(self.a, self.c)
        res = self.client.post(
            "/api/v1/friends/missions/",
            {"title": "Party", "member_friendship_ids": [str(self.edge.id), str(edge2.id), str(edge2.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        goal = access.get_mission(res.data["mission_id"])
        self.assertIsNone(goal.friendship_id)
        self.assertEqual(SharedGoalMembership.objects.filter(shared_goal=goal).count(), 3)
        self.assertTrue(
            any(r["mission_id"] == str(goal.id) for r in services.list_missions(self.c, include_invited=True))
        )
        self.assertEqual(services.get_mission_detail(self.c, goal.id)["updates"], [])
        self.assertEqual(self.jwt(self.c).get(f"/api/v1/friends/missions/{goal.id}/plan/").status_code, 404)
        services.join_mission(self.c, self.c.user, goal.id)
        self.assertEqual(self.jwt(self.c).get(f"/api/v1/friends/missions/{goal.id}/plan/").status_code, 200)
        legacy = services.create_mission(self.a, self.a.user, self.edge.id, title="Old route")
        self.assertIsNone(legacy.friendship_id)

    def test_creator_can_rejoin_solo_project_and_historical_fk_still_works(self):
        solo = services.create_mission(self.a, self.a.user, member_friendship_ids=[], title="Solo")
        services.leave_mission(self.a, solo.id)
        self.assertEqual(services.join_mission(self.a, self.a.user, solo.id)["status"], "active")
        # Existing projects retain their FK; invitations still use its party check.
        from .models import SharedGoal

        SharedGoal.objects.filter(id=self.goal.id).update(friendship=self.edge)
        services.leave_mission(self.b, self.goal.id)
        self.assertEqual(services.join_mission(self.b, self.b.user, self.goal.id)["status"], "active")

    def test_blocked_creator_edge_hides_invitation_and_join(self):
        goal = services.create_mission(self.a, self.a.user, member_friendship_ids=[str(self.edge.id)], title="New")
        self.edge.status = "blocked"
        self.edge.save(update_fields=["status"])
        self.assertFalse(
            any(r["mission_id"] == str(goal.id) for r in services.list_missions(self.b, include_invited=True))
        )
        for suffix, method in [("", "get"), ("join/", "post")]:
            res = getattr(self.jwt(self.b), method)(f"/api/v1/friends/missions/{goal.id}/{suffix}")
            self.assertEqual(res.status_code, 403)

    def test_create_rejects_foreign_pending_and_malformed_friendship(self):
        foreign = _edge(self.b, self.c)
        for ids in [[str(foreign.id)], ["bad"], "not a list"]:
            res = self.client.post(
                "/api/v1/friends/missions/", {"title": "No", "member_friendship_ids": ids}, format="json"
            )
            self.assertIn(res.status_code, [400, 403])
        self.edge.status = "pending"
        self.edge.save(update_fields=["status"])
        self.assertEqual(
            self.client.post(
                "/api/v1/friends/missions/",
                {"title": "No", "member_friendship_ids": [str(self.edge.id)]},
                format="json",
            ).status_code,
            403,
        )

    def test_plan_contains_identity_and_no_private_task_fields(self):
        step = self.step()
        self.ask(step)
        self.accept(step)
        with self.assertNumQueries(8):
            plan = projects.get_plan(self.a, self.goal.id)
        self.assertEqual(plan["my_membership_id"], str(self.member_a.id))
        self.assertEqual(plan["my_role"], "owner")
        self.assertEqual(plan["steps"][0]["owners"][0]["handle"], "ben")
        self.assertIn("hue", plan["members"][0])
        self.assertIn("display_name", plan["members"][0])
        self.assertNotIn("task_id", plan["steps"][0]["assignments"][0])

    def test_steps_crud_version_conflict_and_no_completion_bypass(self):
        res = self.client.post(
            self.base + "steps/",
            {"title": "  Plant\u200b   seeds ", "description": "Hello\u202e friend"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        sid = res.data["step_id"]
        url = self.base + f"steps/{sid}/"
        row = SharedGoalStep.objects.get(id=sid)
        self.assertEqual((row.title, row.description), ("Plant seeds", "Hello friend"))
        self.assertEqual(self.client.patch(url, {"title": "X"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"version": 0, "title": "X"}, format="json").status_code, 200)
        res = self.client.patch(url, {"version": 0, "title": "stale"}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertIn("refresh and try again", str(res.data))
        for data in [
            {"version": 1, "status": "done"},
            {"version": True},
            {"version": 1, "due_date": "bad"},
            {"version": 1, "start_date": "2026-10-05", "due_date": "2026-10-01"},
        ]:
            self.assertEqual(self.client.patch(url, data, format="json").status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 204)

    def _edit_cases(self):
        # Creator, caller, assignments (member/status), expected edit permission.
        return [
            (self.b, self.b, [], True),
            (self.b, self.b, [(self.member_b, "asked")], True),
            (self.b, self.b, [(self.member_a, "countered")], True),
            (self.b, self.b, [(self.member_a, "declined")], True),
            (self.b, self.b, [(self.member_a, "asked")], False),
            (self.b, self.b, [(self.member_a, "accepted")], False),
            (self.a, self.b, [(self.member_b, "accepted")], True),
            (self.a, self.b, [(self.member_b, "asked")], False),
            (self.a, self.b, [(self.member_b, "countered")], False),
            (self.a, self.b, [(self.member_b, "declined")], False),
            (self.a, self.b, [], False),
            (self.b, self.a, [(self.member_b, "asked")], True),
            (self.b, self.a, [(self.member_b, "accepted")], True),
            (self.b, self.b, [(self.member_b, "accepted"), (self.member_a, "asked")], True),
        ]

    def _editable_step_fixture(self, creator, assignments):
        step = projects.create_step(creator, creator.user, self.goal.id, {"title": "Edit rights"})
        for member, status in assignments:
            SharedGoalStepAssignment.objects.create(step=step, membership=member, status=status, asked_by=self.a)
        return step

    def test_step_patch_and_delete_edit_rights_matrix(self):
        for creator, actor, assignments, allowed in self._edit_cases():
            for method in ["patch", "delete"]:
                with self.subTest(creator=creator.id, actor=actor.id, assignments=assignments, method=method):
                    step = self._editable_step_fixture(creator, assignments)
                    response = getattr(self.jwt(actor), method)(
                        self.base + f"steps/{step.id}/", {"title": "Changed", "version": 0}, format="json"
                    )
                    self.assertEqual(response.status_code, (200 if method == "patch" else 204) if allowed else 403)
                    if not allowed:
                        self.assertEqual(str(response.data["detail"]), "Ask the step's owner to change it.")
                        step.refresh_from_db()
                        self.assertEqual((step.title, step.version), ("Edit rights", 0))

    def test_dependency_delete_uses_blocked_step_edit_rights_matrix(self):
        for creator, actor, assignments, allowed in self._edit_cases():
            with self.subTest(creator=creator.id, actor=actor.id, assignments=assignments):
                blocked = self._editable_step_fixture(creator, assignments)
                # Caller owns the blocker AND created the edge: neither grants deletion.
                blocker = projects.create_step(actor, actor.user, self.goal.id, {"title": "Blocker"})
                edge = projects.dependency_write(
                    actor, self.goal.id, {"blocker_id": str(blocker.id), "blocked_id": str(blocked.id)}
                )
                response = self.jwt(actor).delete(self.base + f"dependencies/{edge.id}/")
                self.assertEqual(response.status_code, 204 if allowed else 403)
                if not allowed:
                    self.assertEqual(str(response.data["detail"]), "Ask the step's owner to change it.")
                    self.assertTrue(access.project_dependencies(self.goal).filter(id=edge.id).exists())

    def test_creator_cannot_edit_after_asking_another_member_even_if_they_left(self):
        # Cleo (a plain member) creates the step and it is asked of Aya; Aya — the only
        # owner — leaves, so ownership passes to Ben (joined first), not to Cleo.
        projects.add_members(self.a, self.goal.id, [str(_edge(self.a, self.c).id)])
        services.join_mission(self.c, self.c.user, self.goal.id)
        step = self._editable_step_fixture(self.c, [(self.member_a, "asked")])
        services.leave_mission(self.a, self.goal.id)
        self.assertEqual(
            self.jwt(self.c).patch(self.base + f"steps/{step.id}/", {"version": 0}, format="json").status_code, 403
        )
        # The member who inherited ownership can.
        self.assertEqual(
            self.jwt(self.b).patch(self.base + f"steps/{step.id}/", {"version": 0}, format="json").status_code, 200
        )

    def test_milestone_patch_delete_creator_or_project_owner(self):
        for creator, actor, allowed in [(self.b, self.b, True), (self.b, self.a, True), (self.a, self.b, False)]:
            for method in ["patch", "delete"]:
                with self.subTest(creator=creator.id, actor=actor.id, method=method):
                    milestone = projects.milestone_write(creator, self.goal.id, {"title": "Harvest"})
                    response = getattr(self.jwt(actor), method)(
                        self.base + f"milestones/{milestone.id}/", {"title": "Changed"}, format="json"
                    )
                    self.assertEqual(response.status_code, (200 if method == "patch" else 204) if allowed else 403)
                    if not allowed:
                        milestone.refresh_from_db()
                        self.assertEqual(milestone.title, "Harvest")
                        self.assertIn("milestone's creator", str(response.data["detail"]))

    def test_regular_member_can_create_steps_milestones_and_dependencies(self):
        client = self.jwt(self.b)
        milestone = client.post(self.base + "milestones/", {"title": "Harvest"}, format="json")
        self.assertEqual(milestone.status_code, 201)
        step = client.post(
            self.base + "steps/", {"title": "Plant", "milestone_id": milestone.data["milestone_id"]}, format="json"
        )
        self.assertEqual(step.status_code, 201)
        other = self.step()
        edge = client.post(
            self.base + "dependencies/",
            {"blocker_id": str(other.id), "blocked_id": step.data["step_id"]},
            format="json",
        )
        self.assertEqual(edge.status_code, 201)

    def test_accepted_owner_patch_mirrors_only_own_task_title_and_dates(self):
        step = self.step(start_date="2026-10-01", due_date="2026-10-05", description="Shared description")
        self.ask(step)
        b = self.accept(step)
        self.ask(step, self.a)
        a = self.accept(step, self.a)
        task_a, task_b = Task.objects.get(id=a.task_id), Task.objects.get(id=b.task_id)
        task_b.description = "Private description"
        task_b.save(update_fields=["description"])
        description_receipt = task_b.pii_receipts["description"]
        client = self.jwt(self.b)
        url = self.base + f"steps/{step.id}/"
        for version, fields in enumerate(
            [
                {"title": "  Plant\u200b  seeds ", "start_date": "2026-10-02", "due_date": "2026-10-06"},
                {"start_date": "2026-10-03"},
                {"due_date": None},
                {"title": "Water"},
            ]
        ):
            if version == 1:
                # A start-only edit still restores the shared due date in my Task.
                task_b.due_date = None
                task_b.save(update_fields=["due_date"])
            response = client.patch(url, {"version": version, **fields}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            step.refresh_from_db()
            task_b.refresh_from_db()
            task_a.refresh_from_db()
            self.assertEqual((task_b.title, task_b.due_date), (step.title, step.due_date))
            self.assertEqual(task_b.description, "Private description")
            self.assertEqual(task_b.pii_receipts["description"], description_receipt)
            self.assertEqual(task_b.pii_receipts["title"]["state"], "bypass")
            self.assertEqual((task_a.title, str(task_a.due_date)), ("Dig", "2026-10-05"))
        self.assertEqual(step.status, "open")

    def test_project_owner_patch_does_not_mirror_another_members_task(self):
        step = self.step(due_date="2026-10-05")
        self.ask(step)
        assignment = self.accept(step)
        response = self.client.patch(
            self.base + f"steps/{step.id}/", {"version": 0, "title": "Changed", "due_date": None}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(id=assignment.task_id)
        self.assertEqual((task.title, str(task.due_date)), ("Dig", "2026-10-05"))

    def test_mirror_never_follows_a_foreign_tenant_task_link(self):
        step = self.step()
        self.ask(step)
        assignment = self.accept(step)
        foreign = Task.objects.create(tenant=self.a, title="Private")
        SharedGoalStepAssignment.objects.filter(id=assignment.id).update(task=foreign)
        response = self.jwt(self.b).patch(
            self.base + f"steps/{step.id}/", {"version": 0, "title": "Changed", "due_date": "2026-10-05"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        foreign.refresh_from_db()
        self.assertEqual((foreign.title, foreign.due_date), ("Private", None))

    def test_mirror_title_uses_authoring_receipt_and_preserves_private_description(self):
        from apps.pii.authoring import AuthoredText

        step = self.step()
        self.ask(step)
        assignment = self.accept(step)
        receipt = {"state": "test-authored"}
        with patch("apps.pii.authoring.author_text", return_value=AuthoredText("Meet [PERSON_1]", receipt)) as author:
            response = self.jwt(self.b).patch(
                self.base + f"steps/{step.id}/", {"version": 0, "title": "Meet a person"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        author.assert_called_once()
        self.assertEqual(author.call_args.args, (self.b, "Meet a person"))
        task = Task.objects.get(id=assignment.task_id)
        self.assertEqual(task.title, "Meet [PERSON_1]")
        self.assertEqual(task.pii_receipts["title"], receipt)

    def test_patch_rechecks_rights_version_and_task_link_after_authoring(self):
        from apps.pii.authoring import AuthoredText

        for changed in ["rights", "version", "task_link"]:
            with self.subTest(changed=changed):
                step = self.step()
                self.ask(step)
                assignment = self.accept(step)
                original_task_id = assignment.task_id

                def author(*args, changed=changed, assignment=assignment, step=step, **kwargs):
                    if changed == "rights":
                        SharedGoalStepAssignment.objects.filter(id=assignment.id).update(status="declined")
                    elif changed == "version":
                        SharedGoalStep.objects.filter(id=step.id).update(version=1)
                    else:
                        SharedGoalStepAssignment.objects.filter(id=assignment.id).update(task=None)
                    return AuthoredText("Changed", {})

                with patch("apps.pii.authoring.author_text", side_effect=author):
                    response = self.jwt(self.b).patch(
                        self.base + f"steps/{step.id}/", {"version": 0, "title": "Changed"}, format="json"
                    )
                self.assertEqual(response.status_code, 403 if changed == "rights" else 409)
                step.refresh_from_db()
                self.assertEqual(step.title, "Dig")
                self.assertEqual(Task.objects.get(id=original_task_id).title, "Dig")

    def test_stale_or_invalid_patch_does_not_author_or_sync_private_task(self):
        step = self.step()
        self.ask(step)
        assignment = self.accept(step)
        for data, code in [({"version": 9, "title": "Changed"}, 409), ({"version": 0, "due_date": "bad"}, 400)]:
            with patch("apps.pii.authoring.author_text") as author:
                response = self.jwt(self.b).patch(self.base + f"steps/{step.id}/", data, format="json")
            self.assertEqual(response.status_code, code)
            author.assert_not_called()
        task = Task.objects.get(id=assignment.task_id)
        self.assertEqual((task.title, task.due_date), ("Dig", None))

    def test_plan_mute_preferences_are_visible_only_to_their_member(self):
        SharedGoalMembership.objects.filter(shared_goal=self.goal).update(muted=True)
        step = self.step()
        self.ask(step)
        self.accept(step)
        self.ask(step, self.a)
        self.accept(step, self.a)
        for tenant, member in [(self.a, self.member_a), (self.b, self.member_b)]:
            response = self.jwt(tenant).get(self.base + "plan/")
            self.assertEqual(response.status_code, 200)
            for row in response.data["members"] + response.data["steps"][0]["owners"]:
                if row["id"] == str(member.id):
                    self.assertIs(row["muted"], True)
                else:
                    self.assertNotIn("muted", row)

    def test_counter_offer_flow_requires_authorized_edit_then_reask_and_accept(self):
        step = projects.create_step(self.b, self.b.user, self.goal.id, {"title": "Plant"})
        projects.ask(self.b, self.b.user, self.goal.id, step.id, [str(self.member_a.id)])
        projects.respond(self.a, self.a.user, self.goal.id, step.id, {"answer": "dates", "due": "2026-10-05"})
        step.refresh_from_db()
        self.assertIsNone(step.due_date)
        self.assertEqual(Task.objects.count(), 0)
        response = self.jwt(self.b).patch(
            self.base + f"steps/{step.id}/", {"version": 0, "due_date": "2026-10-05"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        projects.ask(self.b, self.b.user, self.goal.id, step.id, [str(self.member_a.id)])
        assignment = projects.respond(self.a, self.a.user, self.goal.id, step.id, {"answer": "yes"})
        self.assertEqual(str(Task.objects.get(id=assignment.task_id).due_date), "2026-10-05")

    def test_asking_someone_does_not_grant_step_edit_rights(self):
        step = self.step()
        projects.ask(self.b, self.b.user, self.goal.id, step.id, [str(self.member_a.id)])
        projects.respond(self.a, self.a.user, self.goal.id, step.id, {"answer": "dates", "due": "2026-10-05"})
        self.assertEqual(
            self.jwt(self.b)
            .patch(self.base + f"steps/{step.id}/", {"version": 0, "due_date": "2026-10-05"}, format="json")
            .status_code,
            403,
        )

    def test_ask_requires_consent_and_accept_mints_once(self):
        step = self.step()
        self.ask(step)
        self.ask(step)
        self.assertEqual(Task.objects.count(), 0)
        self.assertEqual(SharedGoalStepAssignment.objects.count(), 1)
        assignment = self.accept(step)
        self.accept(step)
        self.assertEqual(Task.objects.count(), 1)
        task = Task.objects.get(id=assignment.task_id)
        self.assertEqual(task.tenant_id, self.b.id)
        self.assertEqual(
            task.related_ref, {"pillar": "neighborhood", "object_type": "SharedGoalStep", "object_id": str(step.id)}
        )
        self.assertEqual(task.pii_receipts["title"]["state"], "bypass")

    def test_respond_only_asked_member_and_complete_only_accepted_owner(self):
        step = self.step()
        self.ask(step)
        root = self.base + f"steps/{step.id}/"
        self.assertEqual(self.client.post(root + "respond/", {"answer": "yes"}, format="json").status_code, 403)
        for client in [self.client, self.jwt(self.b)]:
            for action in ["complete", "reopen"]:
                self.assertEqual(client.post(root + action + "/", {}, format="json").status_code, 403)
        self.accept(step)
        self.assertEqual(self.client.post(root + "complete/", {}, format="json").status_code, 403)
        self.assertEqual(self.jwt(self.b).post(root + "complete/", {}, format="json").status_code, 200)

    def test_invitee_can_be_asked_but_must_join_before_answering(self):
        SharedGoalMembership.objects.filter(id=self.member_b.id).update(status="invited")
        step = self.step()
        self.ask(step)
        url = self.base + f"steps/{step.id}/respond/"
        self.assertEqual(self.jwt(self.b).post(url, {"answer": "yes"}, format="json").status_code, 404)
        services.join_mission(self.b, self.b.user, self.goal.id)
        self.assertEqual(self.jwt(self.b).post(url, {"answer": "yes"}, format="json").status_code, 200)

    def test_counter_dates_smaller_decline_and_reask(self):
        step = self.step()
        self.ask(step)
        url = self.base + f"steps/{step.id}/respond/"
        client = self.jwt(self.b)
        for body in [
            {"answer": "dates", "start": "2026-10-03", "due": "2026-10-05"},
            {"answer": "smaller", "note": "  Half\u200b please "},
        ]:
            self.assertEqual(client.post(url, body, format="json").data["status"], "countered")
        assignment = SharedGoalStepAssignment.objects.get(step=step)
        self.assertEqual(assignment.note, "Half please")
        self.assertEqual(Task.objects.count(), 0)
        self.assertEqual(
            client.post(url, {"answer": "no", "note": "Private reason"}, format="json").data["status"], "declined"
        )
        self.assertEqual(projects.get_plan(self.a, self.goal.id)["steps"][0]["owners"], [])
        update = SharedGoalUpdate.objects.filter(kind="step_answered").latest("created_at")
        self.assertNotIn("note", update.payload)
        self.ask(step)
        self.accept(step)
        self.assertEqual(Task.objects.count(), 1)

    def test_counter_validation_and_caps(self):
        step = self.step()
        self.ask(step)
        url = self.base + f"steps/{step.id}/respond/"
        for body in [
            {"answer": "dates"},
            {"answer": "dates", "start": "2026-10-05", "due": "2026-10-01"},
            {"answer": "smaller", "note": "x" * 201},
            {"answer": "no", "note": "x" * 501},
            {"answer": "unknown"},
        ]:
            self.assertEqual(self.jwt(self.b).post(url, body, format="json").status_code, 400)

    def test_journal_done_is_idempotent_and_api_reopen_mirrors(self):
        step = self.step()
        self.ask(step)
        assignment = self.accept(step)
        task = Task.objects.get(id=assignment.task_id)
        task.complete()
        task.save()
        step.refresh_from_db()
        self.assertEqual(step.status, "done")
        self.assertEqual(step.completed_by_id, self.b.id)
        self.assertEqual(SharedGoalUpdate.objects.filter(kind="step_done").count(), 1)
        projects.complete(self.b, self.b.user, self.goal.id, step.id, reopen=True)
        task.refresh_from_db()
        self.assertEqual((task.status, task.completed_at), ("open", None))
        projects.complete(self.b, self.b.user, self.goal.id, step.id)
        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        self.assertEqual(SharedGoalUpdate.objects.filter(kind="step_done").count(), 2)

    def test_shared_reopen_is_not_undone_by_resaving_another_owners_done_task(self):
        step = self.step()
        self.ask(step)
        b = self.accept(step)
        self.ask(step, self.a)
        self.accept(step, self.a)
        task_b = Task.objects.get(id=b.task_id)
        task_b.complete()
        projects.complete(self.a, self.a.user, self.goal.id, step.id, reopen=True)
        task_b.description = "An unrelated edit"
        task_b.save()
        step.refresh_from_db()
        self.assertEqual(step.status, "open")

    def test_no_friends_assistant_opt_in_needed(self):
        self.a.friends_enabled = False
        self.a.save(update_fields=["friends_enabled"])
        self.assertEqual(self.client.get(self.base + "plan/").status_code, 200)

    def test_unsaved_done_attribute_does_not_complete_shared_step(self):
        step = self.step()
        self.ask(step)
        assignment = self.accept(step)
        task = Task.objects.get(id=assignment.task_id)
        task.status = "done"
        task.description = "Only this field is saved"
        task.save(update_fields=["description"])
        task.refresh_from_db()
        step.refresh_from_db()
        self.assertEqual((task.status, step.status), ("open", "open"))

    def test_forged_link_and_wrong_task_owner_cannot_complete(self):
        step = self.step()
        self.ask(step)
        assignment = self.accept(step)
        forged = Task.objects.create(
            tenant=self.a,
            title="Fake",
            related_ref={"pillar": "neighborhood", "object_type": "SharedGoalStep", "object_id": str(step.id)},
        )
        forged.complete()
        step.refresh_from_db()
        self.assertEqual(step.status, "open")
        SharedGoalStepAssignment.objects.filter(id=assignment.id).update(task=forged)
        forged.save()
        step.refresh_from_db()
        self.assertEqual(step.status, "open")

    def test_leave_retains_task_and_revokes_completion(self):
        step = self.step()
        self.ask(step)
        assignment = self.accept(step)
        services.leave_mission(self.b, self.goal.id)
        Task.objects.get(id=assignment.task_id).complete()
        step.refresh_from_db()
        self.assertEqual(step.status, "open")
        self.assertTrue(Task.objects.filter(id=assignment.task_id).exists())
        self.assertEqual(self.jwt(self.b).get(self.base + "plan/").status_code, 404)

    def test_receiver_failure_never_breaks_task_save(self):
        step = self.step()
        self.ask(step)
        assignment = self.accept(step)
        with patch("apps.friends.access.assignment_for_task", side_effect=RuntimeError("test")):
            Task.objects.get(id=assignment.task_id).complete()
        self.assertEqual(Task.objects.get(id=assignment.task_id).status, "done")

    def test_old_task_endpoint_also_creates_accepted_step(self):
        res = self.client.post(self.base + "tasks/", {"title": "Old client"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(set(res.data), {"task_id", "title"})
        assignment = SharedGoalStepAssignment.objects.get(task_id=res.data["task_id"])
        self.assertEqual((assignment.status, assignment.membership_id), ("accepted", self.member_a.id))

    def test_legacy_proposal_approval_also_populates_plan(self):
        action, _ = services.propose_mission_task(self.a, self.goal.id, title="Proposed")
        result = services.approve_goal_action(self.a, action.id)
        self.assertTrue(SharedGoalStepAssignment.objects.filter(task_id=result["task_id"], status="accepted").exists())

    def test_legacy_proposal_cannot_be_approved_after_leaving(self):
        action, _ = services.propose_mission_task(self.b, self.goal.id, title="Proposed")
        services.leave_mission(self.b, self.goal.id)
        response = self.jwt(self.b).post(f"/api/v1/friends/mission-actions/{action.id}/approve/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Task.objects.count(), 0)

    def test_milestone_crud_completion_and_reset(self):
        res = self.client.post(
            self.base + "milestones/", {"title": " Harvest ", "target_date": "2026-10-05"}, format="json"
        )
        self.assertEqual(res.status_code, 201)
        mid = res.data["milestone_id"]
        step = self.step(milestone_id=mid)
        self.ask(step)
        self.accept(step)
        projects.complete(self.b, self.b.user, self.goal.id, step.id)
        milestone = access.project_milestones(self.goal).get(id=mid)
        self.assertIsNotNone(milestone.reached_at)
        self.assertEqual(SharedGoalUpdate.objects.filter(kind="milestone_reached").count(), 1)
        projects.complete(self.b, self.b.user, self.goal.id, step.id, reopen=True)
        milestone.refresh_from_db()
        self.assertIsNone(milestone.reached_at)
        url = self.base + f"milestones/{mid}/"
        self.assertEqual(self.client.patch(url, {"title": "New"}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 204)
        step.refresh_from_db()
        self.assertIsNone(step.milestone_id)

    def test_dependency_cycle_self_foreign_rejection_and_delete(self):
        a, b, c = [self.step(title=n) for n in "abc"]
        url = self.base + "dependencies/"
        for x, y in [(a, b), (b, c)]:
            self.assertEqual(
                self.client.post(url, {"blocker_id": str(x.id), "blocked_id": str(y.id)}, format="json").status_code,
                201,
            )
        for x, y in [(c, a), (a, a)]:
            self.assertEqual(
                self.client.post(url, {"blocker_id": str(x.id), "blocked_id": str(y.id)}, format="json").status_code,
                400,
            )
        foreign = services.create_mission(self.a, self.a.user, member_friendship_ids=[], title="Other")
        f = projects.create_step(self.a, self.a.user, foreign.id, {"title": "Foreign"})
        self.assertEqual(
            self.client.post(url, {"blocker_id": str(a.id), "blocked_id": str(f.id)}, format="json").status_code, 404
        )
        edge = access.project_dependencies(self.goal).first()
        self.assertEqual(self.client.delete(self.base + f"dependencies/{edge.id}/").status_code, 204)

    def test_foreign_milestone_membership_and_step_ids_are_scoped(self):
        other = services.create_mission(self.a, self.a.user, member_friendship_ids=[], title="Other")
        milestone = projects.milestone_write(self.a, other.id, {"title": "Other milestone"})
        foreign_step = projects.create_step(self.a, self.a.user, other.id, {"title": "Other step"})
        member = SharedGoalMembership.objects.get(shared_goal=other, tenant=self.a)
        step = self.step()
        body = {"version": 0, "milestone_id": str(milestone.id)}
        self.assertEqual(self.client.patch(self.base + f"steps/{step.id}/", body, format="json").status_code, 404)
        body = {"membership_ids": [str(member.id)]}
        self.assertEqual(self.client.post(self.base + f"steps/{step.id}/ask/", body, format="json").status_code, 404)
        self.assertEqual(self.client.delete(self.base + f"steps/{foreign_step.id}/").status_code, 404)
        self.assertEqual(self.client.delete(self.base + f"milestones/{milestone.id}/").status_code, 404)

    def test_input_caps_and_hygiene_on_legacy_goal_fields(self):
        response = self.client.post(
            "/api/v1/friends/missions/",
            {
                "title": " Goal\u200b ",
                "description": " A\u202e   plan ",
                "member_friendship_ids": [],
                "target": {"metric": "check\ufeff in"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        goal = access.get_mission(response.data["mission_id"])
        self.assertEqual((goal.title, goal.description, goal.target["metric"]), ("Goal", "A plan", "check in"))
        for path, body in [
            ("steps/", {"title": "x" * 121}),
            ("steps/", {"title": "ok", "description": "x" * 501}),
            ("milestones/", {"title": "x" * 121}),
            ("updates/", {"kind": "note", "text": "x" * 501}),
            ("tasks/", {"title": "x" * 121}),
        ]:
            self.assertEqual(self.client.post(self.base + path, body, format="json").status_code, 400)

    def test_unblocked_event_only_after_all_blockers(self):
        a, b, c = [self.step(title=n) for n in "abc"]
        for blocker in [a, b]:
            projects.dependency_write(self.a, self.goal.id, {"blocker_id": str(blocker.id), "blocked_id": str(c.id)})
            self.ask(blocker)
            self.accept(blocker)
        projects.complete(self.b, self.b.user, self.goal.id, a.id)
        self.assertEqual(SharedGoalUpdate.objects.filter(kind="step_unblocked").count(), 0)
        projects.complete(self.b, self.b.user, self.goal.id, b.id)
        self.assertEqual(SharedGoalUpdate.objects.filter(kind="step_unblocked").count(), 1)

    def write_routes(self):
        sid, mid, did = [uuid.uuid4() for _ in range(3)]
        return [
            ("post", self.base + "steps/"),
            ("patch", self.base + f"steps/{sid}/"),
            ("delete", self.base + f"steps/{sid}/"),
            *[("post", self.base + f"steps/{sid}/{action}/") for action in ["ask", "respond", "complete", "reopen"]],
            ("post", self.base + "milestones/"),
            ("patch", self.base + f"milestones/{mid}/"),
            ("delete", self.base + f"milestones/{mid}/"),
            ("post", self.base + "dependencies/"),
            ("delete", self.base + f"dependencies/{did}/"),
        ]

    def test_runtime_auth_rejected_on_every_write(self):
        self.a.internal_api_key = "p1a-runtime-test-key"
        self.a.save(update_fields=["internal_api_key"])
        client = APIClient()
        client.credentials(HTTP_X_NBHD_INTERNAL_KEY=self.a.internal_api_key, HTTP_X_NBHD_TENANT_ID=str(self.a.id))
        # First prove these are valid runtime credentials, then attempt app writes.
        runtime = client.get(f"/api/v1/integrations/runtime/{self.a.id}/missions/")
        self.assertEqual(runtime.status_code, 200, runtime.data)
        routes = self.write_routes() + [
            ("post", "/api/v1/friends/missions/"),
            ("patch", self.base),
            *[("post", self.base + s + "/") for s in ["join", "decline", "leave", "updates", "tasks"]],
        ]
        for method, url in routes:
            with self.subTest(method=method, url=url):
                self.assertIn(getattr(client, method)(url, {}, format="json").status_code, [401, 403])
        client.credentials(HTTP_AUTHORIZATION="Bearer p1a-runtime-test-key")
        for method, url in self.write_routes():
            self.assertIn(getattr(client, method)(url, {}, format="json").status_code, [401, 403])

    def test_flag_off_404_on_all_new_routes_and_payload(self):
        with override_settings(PROJECTS_V2_TENANT_IDS=""):
            self.assertFalse(TenantSerializer(self.a).data["projects_v2_enabled"])
            self.assertEqual(self.client.get(self.base + "plan/").status_code, 404)
            for method, url in self.write_routes():
                self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, 404)
            self.assertEqual(self.client.get(self.base).status_code, 200)
        with override_settings(PROJECTS_V2_TENANT_IDS=f"{self.b.id}, {self.a.id}"):
            self.assertTrue(TenantSerializer(self.a).data["projects_v2_enabled"])
        with override_settings(PROJECTS_V2_TENANT_IDS=str(self.c.id)):
            self.assertFalse(TenantSerializer(self.a).data["projects_v2_enabled"])

    def test_stranger_cannot_read_or_mutate_any_route(self):
        client = self.jwt(self.c)
        self.assertEqual(client.get(self.base + "plan/").status_code, 404)
        for method, url in self.write_routes():
            self.assertEqual(getattr(client, method)(url, {}, format="json").status_code, 404)

    def test_limits(self):
        for i in range(8):
            projects.milestone_write(self.a, self.goal.id, {"title": str(i)})
        self.assertEqual(
            self.client.post(self.base + "milestones/", {"title": "overflow"}, format="json").status_code, 400
        )
        SharedGoalStep.objects.bulk_create(
            [SharedGoalStep(shared_goal=self.goal, created_by=self.a, title=str(i)) for i in range(60)]
        )
        self.assertEqual(self.client.post(self.base + "steps/", {"title": "overflow"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.base + "tasks/", {"title": "overflow"}, format="json").status_code, 400)
        self.assertEqual(Task.objects.count(), 0)
        from .models import SharedGoalStepDependency

        rows = list(SharedGoalStep.objects.all())
        pairs = [(a, b) for i, a in enumerate(rows) for b in rows[i + 1 :]]
        SharedGoalStepDependency.objects.bulk_create(
            [SharedGoalStepDependency(blocker=a, blocked=b) for a, b in pairs[:120]]
        )
        a, b = pairs[120]
        self.assertEqual(
            self.client.post(
                self.base + "dependencies/", {"blocker_id": str(a.id), "blocked_id": str(b.id)}, format="json"
            ).status_code,
            400,
        )

    def test_backfill_title_rule_task_link_and_idempotence(self):
        task = Task.objects.create(tenant=self.a, title="legacy")
        rows = []
        for tenant, title, payload in [
            (self.a, " WALK ", {"title": " WALK ", "task_id": str(task.id)}),
            (self.b, "WALK", {"title": "WALK", "task_id": str(task.id)}),
            (self.a, "Fallback", {"task_id": "bad"}),
        ]:
            rows.append(
                SharedGoalUpdate.objects.create(
                    shared_goal=self.goal, tenant=tenant, kind="task_added", text=title, payload=payload
                )
            )
        SharedGoalUpdate.objects.create(
            shared_goal=self.goal, tenant=self.a, kind="task_completed", payload={"title": "walk"}
        )
        migration = importlib.import_module("apps.friends.migrations.0014_projects_v2_backfill")
        with connection.schema_editor() as editor:
            migration.backfill(apps, editor)
            migration.backfill(apps, editor)
        self.assertEqual(SharedGoalStep.objects.count(), 3)
        self.assertEqual(SharedGoalStep.objects.get(id=rows[0].id).status, "done")
        self.assertEqual(SharedGoalStep.objects.get(id=rows[1].id).status, "open")
        self.assertEqual(SharedGoalStep.objects.get(id=rows[2].id).title, "Fallback")
        self.assertEqual(SharedGoalStepAssignment.objects.get(step_id=rows[0].id).task_id, task.id)
        self.assertIsNone(SharedGoalStepAssignment.objects.get(step_id=rows[1].id).task_id)


class ProjectRlsTests(TestCase):
    def setUp(self):
        self.a, self.b, self.c = [_tenant("prls_" + n) for n in "abc"]
        self.goal = services.create_mission(
            self.a, self.a.user, member_friendship_ids=[str(_edge(self.a, self.b).id)], title="RLS"
        )
        self.milestone = projects.milestone_write(self.a, self.goal.id, {"title": "First"})
        self.step = projects.create_step(
            self.a, self.a.user, self.goal.id, {"title": "First step", "milestone_id": str(self.milestone.id)}
        )
        self.other = projects.create_step(self.a, self.a.user, self.goal.id, {"title": "Second step"})
        self.member = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.a)
        projects.ask(self.a, self.a.user, self.goal.id, self.step.id, [str(self.member.id)])
        projects.dependency_write(
            self.a, self.goal.id, {"blocker_id": str(self.step.id), "blocked_id": str(self.other.id)}
        )
        self.tables = {
            "shared_goal_steps": 2,
            "shared_goal_milestones": 1,
            "shared_goal_step_assignments": 1,
            "shared_goal_step_dependencies": 1,
        }

    def visible(self, tenant=None, service=False):
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cur.execute("ALTER TABLE shared_goal_memberships DISABLE ROW LEVEL SECURITY")
            cur.execute("GRANT USAGE ON SCHEMA public TO app_user")
            cur.execute("GRANT SELECT ON shared_goal_memberships, " + ", ".join(self.tables) + " TO app_user")
            cur.execute(
                "SELECT set_config('app.tenant_id', %s, false), set_config('app.service_role', %s, false)",
                [str(tenant.id) if tenant else "", "true" if service else ""],
            )
            cur.execute("SET ROLE app_user")
            try:
                counts = {}
                for table in self.tables:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    counts[table] = cur.fetchone()[0]
                return counts
            finally:
                cur.execute("RESET ROLE")
                cur.execute("SELECT set_config('app.tenant_id', '', false), set_config('app.service_role', '', false)")

    def test_no_guc_stranger_and_invited_fail_closed(self):
        for tenant in [None, self.b, self.c]:
            self.assertEqual(self.visible(tenant), dict.fromkeys(self.tables, 0))

    def test_active_member_and_service_role_can_read(self):
        self.assertEqual(self.visible(self.a), self.tables)
        self.assertEqual(self.visible(service=True), self.tables)
        services.join_mission(self.b, self.b.user, self.goal.id)
        self.assertEqual(self.visible(self.b), self.tables)
        services.leave_mission(self.b, self.goal.id)
        self.assertEqual(self.visible(self.b), dict.fromkeys(self.tables, 0))

    def test_other_project_not_visible_even_for_a_member_elsewhere(self):
        other = services.create_mission(self.c, self.c.user, member_friendship_ids=[], title="Private project")
        projects.create_step(self.c, self.c.user, other.id, {"title": "Other"})
        self.assertEqual(self.visible(self.a), self.tables)
        self.assertEqual(self.visible(self.c), {**dict.fromkeys(self.tables, 0), "shared_goal_steps": 1})

    def test_all_new_tables_are_forced_and_named_role_only(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
                [list(self.tables)],
            )
            rows = cur.fetchall()
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(rls and force for _, rls, force in rows))
            cur.execute("SELECT roles FROM pg_policies WHERE tablename = ANY(%s)", [list(self.tables)])
            policies = cur.fetchall()
            self.assertEqual(len(policies), 16)
            self.assertTrue(all(roles == ["app_user"] for (roles,) in policies))

    def test_service_context_restores_nested_context(self):
        with access.backstop_service_context():
            with access.backstop_service_context():
                pass
            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('app.service_role', true)")
                self.assertEqual(cur.fetchone()[0], "true")
