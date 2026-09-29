"""Projects v2 round 2: add people after the start, leave (owner hand-off), delete
for everyone, and the Horizons linked-project card."""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.journal.models import Goal

from . import access, services
from . import project_services as projects
from .models import PendingProjectAction, SharedGoal, SharedGoalMembership
from .test_projects_v2_p1b import _PushCapture
from .test_pr6 import _edge, _profile, _tenant


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ProjectRound2Tests(TestCase):
    def setUp(self):
        self.a, self.b, self.c, self.d = [_tenant("r2_" + n) for n in "abcd"]
        for tenant, name in [(self.a, "aya"), (self.b, "ben"), (self.c, "cleo"), (self.d, "dan")]:
            _profile(tenant, name)
        self.ab = _edge(self.a, self.b)
        self.ac = _edge(self.a, self.c)
        self.goal = services.create_mission(self.a, self.a.user, member_friendship_ids=[str(self.ab.id)], title="Garden")
        services.join_mission(self.b, self.b.user, self.goal.id)

    def client_for(self, tenant):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(tenant.user).access_token}")
        return client

    def member(self, tenant):
        return SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=tenant)

    # ── Add people later ─────────────────────────────────────────────────────

    def test_creator_adds_a_neighbor_who_can_then_join(self):
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.a).post(
                f"/api/v1/friends/missions/{self.goal.id}/members/",
                {"member_friendship_ids": [str(self.ac.id)]},
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"invited": 1})
        self.assertEqual(self.member(self.c).status, "invited")
        # Only the new invitee is pinged — not Ben, who is already in.
        self.assertEqual(len(cap.to(self.c)), 1)
        self.assertEqual(cap.to(self.b), [])
        services.join_mission(self.c, self.c.user, self.goal.id)
        self.assertEqual(self.member(self.c).status, "active")

    def test_adding_someone_already_in_is_a_no_op_and_a_left_member_is_re_invited(self):
        self.assertEqual(projects.add_members(self.a, self.goal.id, [str(self.ab.id)]), [])
        services.leave_mission(self.b, self.goal.id)
        self.assertEqual(len(projects.add_members(self.a, self.goal.id, [str(self.ab.id)])), 1)
        self.assertEqual(self.member(self.b).status, "invited")
        self.assertIsNone(self.member(self.b).left_at)

    def test_only_the_creator_can_add_and_only_their_own_neighbors(self):
        bd = _edge(self.b, self.d)
        response = self.client_for(self.b).post(
            f"/api/v1/friends/missions/{self.goal.id}/members/", {"member_friendship_ids": [str(bd.id)]}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        # A stranger's friendship id (Ben–Dan) is not the creator's edge.
        response = self.client_for(self.a).post(
            f"/api/v1/friends/missions/{self.goal.id}/members/", {"member_friendship_ids": [str(bd.id)]}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SharedGoalMembership.objects.filter(shared_goal=self.goal, tenant=self.d).exists())

    def test_plan_says_who_can_invite(self):
        self.assertTrue(projects.get_plan(self.a, self.goal.id)["can_invite"])
        self.assertFalse(projects.get_plan(self.b, self.goal.id)["can_invite"])

    # ── Leave ───────────────────────────────────────────────────────────────

    def test_last_owner_leaving_hands_the_project_on(self):
        services.leave_mission(self.a, self.goal.id)
        self.assertEqual(self.member(self.a).status, "left")
        self.assertEqual(self.member(self.b).role, "owner")
        # The new owner can still work in it; the one who left can't see it.
        projects.create_step(self.b, self.b.user, self.goal.id, {"title": "Water"})
        self.assertEqual([m.id for m in access.missions_for(self.a)], [])

    # ── Delete ──────────────────────────────────────────────────────────────

    def test_owner_deletes_for_everyone(self):
        PendingProjectAction.objects.create(
            tenant=self.b, shared_goal=self.goal, payload={"summary": "x", "changes": []}, expires_at=self.goal.created_at
        )
        response = self.client_for(self.a).post(f"/api/v1/friends/missions/{self.goal.id}/delete/")
        self.assertEqual(response.status_code, 200)
        self.goal.refresh_from_db()
        self.assertEqual(self.goal.status, SharedGoal.Status.ABANDONED)
        self.assertEqual({m.status for m in SharedGoalMembership.objects.filter(shared_goal=self.goal)}, {"left"})
        self.assertEqual(PendingProjectAction.objects.get(shared_goal=self.goal).status, "expired")
        for tenant in (self.a, self.b):
            self.assertEqual(list(access.missions_for(tenant, include_invited=True)), [])
            self.assertEqual(self.client_for(tenant).get(f"/api/v1/friends/missions/{self.goal.id}/plan/").status_code, 404)

    def test_a_member_cannot_delete(self):
        response = self.client_for(self.b).post(f"/api/v1/friends/missions/{self.goal.id}/delete/")
        self.assertEqual(response.status_code, 403)
        self.goal.refresh_from_db()
        self.assertEqual(self.goal.status, SharedGoal.Status.ACTIVE)

    # ── Horizons card ───────────────────────────────────────────────────────

    def test_linked_projects_show_progress_and_my_next_step(self):
        mine = Goal.objects.create(tenant=self.b, title="Get outside more")
        projects.set_linked_goal(self.b, self.goal.id, str(mine.id))
        later = projects.create_step(self.a, self.a.user, self.goal.id, {"title": "Plant", "due_date": "2026-11-20"})
        sooner = projects.create_step(self.a, self.a.user, self.goal.id, {"title": "Dig", "due_date": "2026-11-02"})
        projects.create_step(self.a, self.a.user, self.goal.id, {"title": "Aya's step"})
        mb = self.member(self.b)
        for step in (later, sooner):
            projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(mb.id)])
            projects.respond(self.b, self.b.user, self.goal.id, step.id, {"answer": "yes"})

        rows = self.client_for(self.b).get("/api/v1/friends/projects/linked/").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["linked_goal_id"], str(mine.id))
        self.assertEqual((rows[0]["done_count"], rows[0]["total"]), (0, 3))
        self.assertEqual(rows[0]["next_step"]["title"], "Dig")
        # Aya never linked a goal: nothing for her, and Ben's goal never leaks to her.
        self.assertEqual(self.client_for(self.a).get("/api/v1/friends/projects/linked/").json(), [])

    def test_linked_projects_skip_a_deleted_project(self):
        mine = Goal.objects.create(tenant=self.b, title="Get outside more")
        projects.set_linked_goal(self.b, self.goal.id, str(mine.id))
        projects.delete_project(self.a, self.goal.id)
        self.assertEqual(projects.linked_projects(self.b), [])
