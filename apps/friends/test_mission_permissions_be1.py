"""R1–R4 API permission matrices and invitation/removal regressions."""

from datetime import date

from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.journal.models import Task

from . import access, services
from . import project_services as projects
from .models import SharedGoal, SharedGoalMembership, SharedGoalStepAssignment, SharedGoalUpdate
from .test_pr6 import _edge, _profile, _tenant
from .test_projects_v2_p1b import _PushCapture


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class MissionPermissionMatrixTests(TestCase):
    def setUp(self):
        names = ["owner", "coowner", "member", "creator", "invitee", "removed", "stranger", "candidate"]
        self.people = {name: _tenant("be1_" + name) for name in names}
        for name, tenant in self.people.items():
            _profile(tenant, name)
            setattr(self, name, tenant)
        edges = [_edge(self.owner, self.people[name]) for name in names[1:6]]
        self.goal = services.create_mission(
            self.owner, self.owner.user, member_friendship_ids=[str(edge.id) for edge in edges], title="Garden"
        )
        for name in ["coowner", "member", "creator", "removed"]:
            services.join_mission(self.people[name], self.people[name].user, self.goal.id)
        SharedGoalMembership.objects.filter(shared_goal=self.goal, tenant=self.coowner).update(role="owner")
        SharedGoalMembership.objects.filter(shared_goal=self.goal, tenant=self.removed).update(status="removed")
        self.members = {row.tenant_id: row for row in SharedGoalMembership.objects.filter(shared_goal=self.goal)}
        self.base = f"/api/v1/friends/missions/{self.goal.id}/"
        self.creator_step = projects.create_step(self.creator, self.creator.user, self.goal.id, {"title": "Prepare"})

    def jwt(self, tenant):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(tenant.user).access_token}")
        return client

    def actors(self):
        for name in ["owner", "coowner", "member", "creator_before", "creator_after", "invitee", "removed", "stranger"]:
            if name == "creator_after":
                projects.ask(
                    self.owner,
                    self.owner.user,
                    self.goal.id,
                    self.creator_step.id,
                    [str(self.members[self.member.id].id)],
                )
            key = "creator" if name.startswith("creator_") else name
            yield (
                name,
                self.people[key],
                200 if key in {"owner", "coowner"} else 403 if key in {"member", "creator"} else 404,
            )

    def version(self):
        self.goal.refresh_from_db()
        return self.goal.version

    def member_url(self, tenant, action):
        return self.base + f"members/{self.members[tenant.id].id}/{action}/"

    def plan(self, tenant):
        response = self.jwt(tenant).get(self.base + "plan/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def plan_step(self, tenant, step):
        return next(row for row in self.plan(tenant)["steps"] if row["id"] == str(step.id))

    def test_project_edit_writes_and_flags_matrix(self):
        for label, actor, code in self.actors():
            with self.subTest(actor=label):
                plan = self.jwt(actor).get(self.base + "plan/")
                self.assertEqual(plan.status_code, 404 if code == 404 else 200)
                if code != 404:
                    self.assertEqual(plan.data["can_edit_project"], code == 200)
                    self.assertEqual(plan.data["can_invite"], code == 200)
                for fields in [
                    {"title": "New name"},
                    {"description": "New purpose"},
                    {"target_date": "2026-11-01"},
                    {"target_date": None},
                ]:
                    version = self.version()
                    response = self.jwt(actor).patch(self.base, {"version": version, **fields}, format="json")
                    self.assertEqual(response.status_code, code, response.data)
                    self.assertEqual(self.version(), version + (code == 200))

    def test_unknown_patch_keys_rejected_at_api_and_service_without_version_bump(self):
        for key in ["status", "target", "pillar", "created_by_id", "typo"]:
            for fields in [{key: "bad"}, {"title": "Changed", key: "bad"}]:
                with self.subTest(fields=fields):
                    response = self.jwt(self.owner).patch(self.base, {"version": 0, **fields}, format="json")
                    self.assertEqual(response.status_code, 400, response.data)
                    with self.assertRaises(ValidationError):
                        services.update_mission(self.owner, self.goal.id, expected_version=0, fields=fields)
                    self.assertEqual(self.version(), 0)
        for data in [[], {}, {"version": 0}, {"version": True, "title": "Bad"}]:
            self.assertEqual(self.jwt(self.owner).patch(self.base, data, format="json").status_code, 400)

    def test_description_round_trip_cleaning_limits_and_version_conflict(self):
        response = self.jwt(self.owner).patch(
            self.base,
            {
                "version": 0,
                "title": "  New\u200b  name ",
                "description": " Hello\u202e\n neighbors ",
                "target_date": "2026-11-01",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["description"], "Hello neighbors")
        self.assertEqual(response.data["title"], "New name")
        self.assertEqual(self.plan(self.member)["description"], "Hello neighbors")
        self.assertEqual(self.jwt(self.member).get(self.base).data["description"], "Hello neighbors")
        self.goal.refresh_from_db()
        self.assertEqual(self.goal.target_date, date(2026, 11, 1))
        self.assertEqual(
            self.jwt(self.owner).patch(self.base, {"version": 0, "description": "stale"}, format="json").status_code,
            409,
        )
        for fields in [
            {"title": "x" * 121},
            {"title": 3},
            {"title": "\u200b"},
            {"description": "x" * 501},
            {"description": {}},
            {"target_date": "bad"},
        ]:
            self.assertEqual(
                self.jwt(self.owner).patch(self.base, {"version": 1, **fields}, format="json").status_code, 400
            )
            self.assertEqual(self.version(), 1)
        response = self.jwt(self.coowner).patch(
            self.base, {"version": 1, "description": "", "target_date": None}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.plan(self.owner)["description"], "")
        self.assertIsNone(self.plan(self.owner)["target_date"])

    def test_invite_write_matrix_records_actual_inviter(self):
        edges = {actor.id: _edge(actor, self.candidate) for actor in self.people.values() if actor != self.candidate}
        for label, actor, code in self.actors():
            with self.subTest(actor=label):
                response = self.jwt(actor).post(
                    self.base + "members/", {"member_friendship_ids": [str(edges[actor.id].id)]}, format="json"
                )
                self.assertEqual(response.status_code, 201 if code == 200 else code, response.data)
                if code == 200:
                    row = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.candidate)
                    self.assertEqual(row.invited_by_id, actor.id)
                    row.status = "declined"
                    row.save(update_fields=["status"])
        self.assertEqual(self.members[self.invitee.id].invited_by_id, self.owner.id)

    def test_remove_cancel_writes_and_per_member_flags_matrix(self):
        for label, actor, code in self.actors():
            with self.subTest(actor=label):
                if code != 404:
                    rows = {row["id"]: row for row in self.plan(actor)["members"]}
                    for target in self.members.values():
                        self.assertEqual(
                            rows[str(target.id)]["can_remove"],
                            code == 200 and target.status == "active" and target.role != "owner",
                        )
                        self.assertEqual(
                            rows[str(target.id)]["can_cancel_invite"], code == 200 and target.status == "invited"
                        )
                for target, action, new_status in [
                    (self.member, "remove", "removed"),
                    (self.invitee, "cancel-invite", "declined"),
                ]:
                    version = self.version()
                    response = self.jwt(actor).post(
                        self.member_url(target, action), {"version": version}, format="json"
                    )
                    self.assertEqual(response.status_code, code, response.data)
                    row = SharedGoalMembership.objects.get(id=self.members[target.id].id)
                    if code == 200:
                        self.assertEqual(response.data["status"], new_status)
                        self.assertEqual(response.data["version"], version + 1)
                        self.assertIsNotNone(row.left_at)
                        row.status, row.left_at = self.members[target.id].status, None
                        row.save(update_fields=["status", "left_at"])
                    else:
                        self.assertEqual(row.status, self.members[target.id].status)

    def test_member_actions_refuse_owners_wrong_states_foreign_targets_and_stale_version(self):
        client = self.jwt(self.owner)
        for tenant, action in [
            (self.owner, "remove"),
            (self.coowner, "remove"),
            (self.invitee, "remove"),
            (self.removed, "remove"),
            (self.member, "cancel-invite"),
        ]:
            self.assertEqual(
                client.post(self.member_url(tenant, action), {"version": 0}, format="json").status_code, 403
            )
        for data in [{}, {"version": True}, {"version": "0"}, {"version": 0, "status": "left"}]:
            self.assertEqual(client.post(self.member_url(self.member, "remove"), data, format="json").status_code, 400)
        foreign = services.create_mission(
            self.stranger, self.stranger.user, member_friendship_ids=[], title="Elsewhere"
        )
        foreign_member = SharedGoalMembership.objects.get(shared_goal=foreign)
        self.assertEqual(
            client.post(self.base + f"members/{foreign_member.id}/remove/", {"version": 0}, format="json").status_code,
            404,
        )
        for action, target in [("remove", self.member), ("cancel-invite", self.invitee)]:
            response = client.post(self.member_url(target, action), {"version": 99}, format="json")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["version"], 0)
        self.assertEqual(self.version(), 0)

    def test_remove_releases_open_work_preserves_tasks_and_matches_leave_activity_without_push(self):
        steps = [
            projects.create_step(self.owner, self.owner.user, self.goal.id, {"title": title})
            for title in ["Held", "Asked", "Counter", "Done", "Review"]
        ]
        assignments = []
        for step in steps:
            projects.ask(self.owner, self.owner.user, self.goal.id, step.id, [str(self.members[self.member.id].id)])
            if step.title in {"Held", "Done", "Review"}:
                row = projects.respond(self.member, self.member.user, self.goal.id, step.id, {"answer": "yes"})
            else:
                row = SharedGoalStepAssignment.objects.get(step=step)
            assignments.append(row)
        steps[0].status = "in_progress"
        steps[0].save(update_fields=["status"])
        assignments[2].status = "countered"
        assignments[2].save(update_fields=["status"])
        for step, status in zip(steps[3:], ["done", "in_review"], strict=True):
            step.status = status
            step.save(update_fields=["status"])
        task_ids = list(Task.objects.filter(tenant=self.member).values_list("id", flat=True))
        self.assertEqual(len(task_ids), 3)
        before = {str(row.id): (row.title, row.status, row.related_ref) for row in Task.objects.filter(id__in=task_ids)}
        update_count = SharedGoalUpdate.objects.filter(shared_goal=self.goal).count()
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            response = self.jwt(self.coowner).post(
                self.member_url(self.member, "remove"), {"version": 0}, format="json"
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["released"], 1)
        self.assertEqual(cap.to(self.member), [])
        for row, expected in zip(
            assignments, ["released", "declined", "declined", "accepted", "accepted"], strict=True
        ):
            row.refresh_from_db()
            self.assertEqual(row.status, expected)
        self.assertEqual(
            {str(row.id): (row.title, row.status, row.related_ref) for row in Task.objects.filter(id__in=task_ids)},
            before,
        )
        self.assertEqual(SharedGoalUpdate.objects.filter(shared_goal=self.goal).count(), update_count + 2)
        self.assertEqual(
            set(
                SharedGoalUpdate.objects.filter(shared_goal=self.goal)
                .order_by("-created_at")
                .values_list("kind", flat=True)[:2]
            ),
            {"step_released", "member_left"},
        )
        for suffix, method in [("plan/", "get"), ("", "get"), ("join/", "post")]:
            self.assertEqual(getattr(self.jwt(self.member), method)(self.base + suffix).status_code, 404)
        self.assertFalse(
            any(
                row["mission_id"] == str(self.goal.id)
                for row in services.list_missions(self.member, include_invited=True)
            )
        )

    def test_cancel_hides_invitation_and_closes_pending_step_asks(self):
        step = projects.create_step(self.creator, self.creator.user, self.goal.id, {"title": "Seed"})
        projects.ask(self.owner, self.owner.user, self.goal.id, step.id, [str(self.members[self.invitee.id].id)])
        self.assertFalse(self.plan_step(self.creator, step)["can_edit"])
        count = SharedGoalUpdate.objects.filter(shared_goal=self.goal).count()
        response = self.jwt(self.owner).post(
            self.member_url(self.invitee, "cancel-invite"), {"version": 0}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(SharedGoalUpdate.objects.filter(shared_goal=self.goal).count(), count)
        self.assertFalse(
            any(
                row["mission_id"] == str(self.goal.id)
                for row in services.list_missions(self.invitee, include_invited=True)
            )
        )
        self.assertEqual(self.jwt(self.invitee).get(self.base).status_code, 404)
        self.assertEqual(self.jwt(self.invitee).post(self.base + "join/").status_code, 404)
        self.assertTrue(self.plan_step(self.creator, step)["can_edit"])

    def test_step_flags_and_patch_delete_use_same_editor_matrix(self):
        for label, actor, code in self.actors():
            for asked in [False, True]:
                for method in ["patch", "delete"]:
                    with self.subTest(actor=label, asked=asked, method=method):
                        step = projects.create_step(self.creator, self.creator.user, self.goal.id, {"title": "Seed"})
                        if asked:
                            projects.ask(
                                self.owner,
                                self.owner.user,
                                self.goal.id,
                                step.id,
                                [str(self.members[self.member.id].id)],
                            )
                        allowed = code == 200 or (actor == self.creator and not asked)
                        if code != 404:
                            row = next(row for row in self.plan(actor)["steps"] if row["id"] == str(step.id))
                            self.assertEqual((row["can_edit"], row["can_delete"]), (allowed, allowed))
                        response = getattr(self.jwt(actor), method)(
                            self.base + f"steps/{step.id}/", {"version": 0, "title": "Changed"}, format="json"
                        )
                        self.assertEqual(
                            response.status_code,
                            (200 if method == "patch" else 204) if allowed else 404 if code == 404 else 403,
                            response.data,
                        )
                        access.project_steps(self.goal).filter(id=step.id).delete()

    def test_accepted_holder_retains_edit_flags_even_when_another_person_is_asked(self):
        step = projects.create_step(self.creator, self.creator.user, self.goal.id, {"title": "Seed"})
        projects.ask(
            self.owner,
            self.owner.user,
            self.goal.id,
            step.id,
            [str(self.members[self.member.id].id), str(self.members[self.coowner.id].id)],
        )
        projects.respond(self.member, self.member.user, self.goal.id, step.id, {"answer": "yes"})
        row = self.plan_step(self.member, step)
        self.assertTrue(row["can_edit"])
        self.assertTrue(row["can_delete"])
        response = self.jwt(self.member).patch(
            self.base + f"steps/{step.id}/", {"version": 0, "due_date": "2026-11-01"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.jwt(self.member).delete(self.base + f"steps/{step.id}/").status_code, 204)

    def test_milestone_flags_and_patch_delete_matrix(self):
        for label, actor, code in self.actors():
            for method in ["patch", "delete"]:
                with self.subTest(actor=label, method=method):
                    milestone = projects.milestone_write(self.creator, self.goal.id, {"title": "Harvest"})
                    allowed = code == 200 or actor == self.creator
                    if code != 404:
                        row = next(row for row in self.plan(actor)["milestones"] if row["id"] == str(milestone.id))
                        self.assertEqual(row["can_edit"], allowed)
                    response = getattr(self.jwt(actor), method)(
                        self.base + f"milestones/{milestone.id}/", {"title": "Changed"}, format="json"
                    )
                    self.assertEqual(
                        response.status_code,
                        (200 if method == "patch" else 204) if allowed else 404 if code == 404 else 403,
                        response.data,
                    )
                    access.project_milestones(self.goal).filter(id=milestone.id).delete()

    def test_plan_capabilities_do_not_expose_tenant_creator_or_inviter_ids(self):
        projects.create_step(self.creator, self.creator.user, self.goal.id, {"title": "Seed"})
        projects.milestone_write(self.creator, self.goal.id, {"title": "Harvest"})
        plan = self.plan(self.owner)

        def check(value):
            if isinstance(value, dict):
                self.assertFalse(
                    {"tenant_id", "created_by_id", "created_by", "invited_by", "invited_by_id", "task_id"} & set(value)
                )
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(plan)
        for tenant in self.people.values():
            self.assertNotIn(str(tenant.id), str(plan))

    def test_successor_owner_invites_own_neighbor_and_acceptance_uses_inviter(self):
        goal = services.create_mission(
            self.owner,
            self.owner.user,
            member_friendship_ids=[str(_edge(self.owner, self.candidate).id)],
            title="Successor",
        )
        services.join_mission(self.candidate, self.candidate.user, goal.id)
        services.leave_mission(self.owner, goal.id)
        edge = _edge(self.candidate, self.stranger)
        projects.add_members(self.candidate, goal.id, [str(edge.id)])
        row = SharedGoalMembership.objects.get(shared_goal=goal, tenant=self.stranger)
        self.assertEqual(row.invited_by_id, self.candidate.id)
        self.assertTrue(projects.get_plan(self.candidate, goal.id)["can_invite"])
        self.assertTrue(any(item.id == goal.id for item in access.missions_for(self.stranger, include_invited=True)))
        self.assertEqual(services.get_mission_detail(self.stranger, goal.id)["my_status"], "invited")
        services.join_mission(self.stranger, self.stranger.user, goal.id)
        self.assertEqual(SharedGoalMembership.objects.get(id=row.id).status, "active")

    def test_inviter_friendship_wins_over_creator_and_historical_friendship_fk(self):
        creator_edge = _edge(self.owner, self.candidate)
        inviter_edge = _edge(self.coowner, self.candidate)
        # Even historical projects with friendship set use invited_by when present.
        SharedGoal.objects.filter(id=self.goal.id).update(friendship=creator_edge)
        projects.add_members(self.coowner, self.goal.id, [str(inviter_edge.id)])
        inviter_edge.status = "blocked"
        inviter_edge.save(update_fields=["status"])
        self.assertFalse(
            any(item.id == self.goal.id for item in access.missions_for(self.candidate, include_invited=True))
        )
        for suffix, method in [("", "get"), ("join/", "post")]:
            self.assertEqual(getattr(self.jwt(self.candidate), method)(self.base + suffix).status_code, 403)
        inviter_edge.status = "accepted"
        inviter_edge.save(update_fields=["status"])
        creator_edge.status = "blocked"
        creator_edge.save(update_fields=["status"])
        self.assertTrue(
            any(item.id == self.goal.id for item in access.missions_for(self.candidate, include_invited=True))
        )
        self.assertEqual(self.jwt(self.candidate).get(self.base).status_code, 200)
        self.assertEqual(self.jwt(self.candidate).post(self.base + "join/").status_code, 200)

    def test_old_null_inviter_rows_fall_back_without_backfill_and_reinvite_sets_inviter(self):
        row = self.members[self.invitee.id]
        row.invited_by = None
        row.save(update_fields=["invited_by"])
        self.assertTrue(
            any(item.id == self.goal.id for item in access.missions_for(self.invitee, include_invited=True))
        )
        self.assertEqual(self.jwt(self.invitee).get(self.base).status_code, 200)
        # The accepted creator edge is required for a legacy invitation.
        from .models import Friendship

        Friendship.objects.filter(requester=self.owner, addressee=self.invitee).update(status="blocked")
        self.assertFalse(
            any(item.id == self.goal.id for item in access.missions_for(self.invitee, include_invited=True))
        )
        self.assertEqual(self.jwt(self.invitee).post(self.base + "join/").status_code, 403)
        response = self.jwt(self.owner).post(
            self.member_url(self.invitee, "cancel-invite"), {"version": 0}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        new_edge = _edge(self.coowner, self.invitee)
        projects.add_members(self.coowner, self.goal.id, [str(new_edge.id)])
        row.refresh_from_db()
        self.assertEqual(row.invited_by_id, self.coowner.id)
        self.assertEqual(self.jwt(self.invitee).post(self.base + "join/").status_code, 200)

    def test_removed_member_returns_only_after_an_owner_reinvites_them(self):
        response = self.jwt(self.owner).post(self.member_url(self.member, "remove"), {"version": 0}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.jwt(self.member).post(self.base + "join/").status_code, 404)
        edge = _edge(self.coowner, self.member)
        response = self.jwt(self.coowner).post(
            self.base + "members/", {"member_friendship_ids": [str(edge.id)]}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        row = SharedGoalMembership.objects.get(id=self.members[self.member.id].id)
        self.assertEqual((row.status, row.invited_by_id, row.left_at), ("invited", self.coowner.id, None))
        self.assertEqual(self.jwt(self.member).post(self.base + "join/").status_code, 200)
