"""P1b: project pushes (privacy, mute, gating), due-tomorrow nudges, decision moments,
the private Horizons goal link, and tenant-id-free plan payloads."""

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.journal.models import Goal, Task

from . import project_services as projects
from . import services
from .models import SharedGoalMembership, SharedGoalStep, SharedGoalStepAssignment
from .project_notifications import run_due_nudges
from .test_pr6 import _edge, _profile, _tenant


class _PushCapture:
    """Patch APNs on + capture every device push (after commit, synchronously)."""

    def __init__(self, case):
        self.case = case
        self.calls = []

    def __enter__(self):
        self._p1 = patch("apps.common.apns.apns_configured", return_value=True)
        self._p2 = patch("apps.router.push_views._push_to_user_devices", side_effect=self._record)
        self._p1.start()
        self._p2.start()
        return self

    def _record(self, user, **kwargs):
        self.calls.append({"user": user, **kwargs})
        return {}

    def __exit__(self, *exc):
        self._p2.stop()
        self._p1.stop()

    def to(self, tenant):
        return [c for c in self.calls if c["user"].id == tenant.user.id]


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ProjectPushTests(TestCase):
    def setUp(self):
        self.a, self.b, self.c = [_tenant("p1b_" + n) for n in "abc"]
        for tenant, name in [(self.a, "aya"), (self.b, "ben"), (self.c, "cleo")]:
            _profile(tenant, name)
        self.ab = _edge(self.a, self.b)
        self.ac = _edge(self.a, self.c)
        self.goal = services.create_mission(
            self.a, self.a.user, member_friendship_ids=[str(self.ab.id), str(self.ac.id)], title="Garden"
        )
        services.join_mission(self.b, self.b.user, self.goal.id)
        services.join_mission(self.c, self.c.user, self.goal.id)
        self.mb = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.b)
        self.mc = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.c)

    def step(self, title="Buy timber", **extra):
        return projects.create_step(self.a, self.a.user, self.goal.id, {"title": title, **extra})

    def test_invite_push_goes_to_invitees_with_names_only(self):
        edge = _edge(self.b, self.c)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            services.create_mission(self.b, self.b.user, member_friendship_ids=[str(edge.id)], title="Trip")
        [call] = cap.to(self.c)
        self.assertEqual(call["extra"]["type"], "project_invite")
        self.assertIn("invited you to “Trip”", call["body"])
        self.assertEqual(cap.to(self.b), [])

    def test_ask_pushes_only_the_asked_member(self):
        step = self.step()
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.mb.id)])
        [call] = cap.to(self.b)
        self.assertEqual(call["extra"], {"type": "step_ask", "mission_id": str(self.goal.id), "step_id": str(step.id)})
        self.assertIn("asked you to take “Buy timber”", call["body"])
        self.assertEqual(cap.to(self.a) + cap.to(self.c), [])

    def test_re_ask_of_an_outstanding_ask_does_not_push_again(self):
        step = self.step()
        projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.mb.id)])
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.mb.id)])
        self.assertEqual(cap.calls, [])

    def test_answer_push_never_carries_the_note(self):
        step = self.step()
        projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.mb.id)])
        secret = "I have a hospital appointment that week"
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.respond(self.b, self.b.user, self.goal.id, step.id, {"answer": "smaller", "note": secret})
        [call] = cap.to(self.a)
        self.assertEqual(call["extra"]["type"], "step_answer")
        self.assertIn("offered to take part of “Buy timber”", call["body"])
        self.assertNotIn("hospital", call["body"])

    def test_unblocked_push_goes_to_the_blocked_steps_owners(self):
        blocker = self.step("Buy timber")
        blocked = self.step("Build frames")
        projects.dependency_write(self.a, self.goal.id, {"blocker_id": str(blocker.id), "blocked_id": str(blocked.id)})
        for step, member, tenant in [(blocker, self.mb, self.b), (blocked, self.mc, self.c)]:
            projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(member.id)])
            projects.respond(tenant, tenant.user, self.goal.id, step.id, {"answer": "yes"})
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.complete(self.b, self.b.user, self.goal.id, blocker.id)
        unblocked = [c for c in cap.to(self.c) if c["extra"]["type"] == "step_unblocked"]
        self.assertEqual(len(unblocked), 1)
        self.assertIn("“Build frames” can start now", unblocked[0]["body"])
        self.assertEqual([c for c in cap.to(self.b) if c["extra"]["type"] == "step_unblocked"], [])

    def test_milestone_push_skips_the_actor_and_muted_members(self):
        m = projects.milestone_write(self.a, self.goal.id, {"title": "Beds built"})
        step = self.step(milestone_id=str(m.id))
        projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.mb.id)])
        projects.respond(self.b, self.b.user, self.goal.id, step.id, {"answer": "yes"})
        SharedGoalMembership.objects.filter(id=self.mc.id).update(muted=True)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.complete(self.b, self.b.user, self.goal.id, step.id)
        reached = [c for c in cap.calls if c["extra"]["type"] == "milestone_reached"]
        self.assertEqual([c["user"].id for c in reached], [self.a.user.id])
        self.assertIn("Beds built", reached[0]["body"])

    @override_settings(PROJECTS_V2_TENANT_IDS="")
    def test_cannot_ask_someone_whose_app_cannot_answer(self):
        with override_settings(PROJECTS_V2_TENANT_IDS=f"{self.a.id}"):
            step = self.step()
            res = self._client(self.a).post(
                f"/api/v1/friends/missions/{self.goal.id}/steps/{step.id}/ask/",
                {"membership_ids": [str(self.mb.id)]},
                format="json",
            )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(SharedGoalStepAssignment.objects.filter(step=step).count(), 0)

    def _client(self, tenant):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(tenant.user).access_token}")
        return client


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class DueNudgeTests(TestCase):
    def setUp(self):
        self.a, self.b = _tenant("due_a"), _tenant("due_b")
        _profile(self.a, "aya")
        _profile(self.b, "ben")
        self.b.user.timezone = "Asia/Tokyo"
        self.b.user.save(update_fields=["timezone"])
        edge = _edge(self.a, self.b)
        self.goal = services.create_mission(self.a, self.a.user, member_friendship_ids=[str(edge.id)], title="Garden")
        services.join_mission(self.b, self.b.user, self.goal.id)
        self.mb = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.b)
        self.tokyo_nine = datetime(2026, 10, 14, 9, 7, tzinfo=ZoneInfo("Asia/Tokyo"))
        self.step = projects.create_step(
            self.a, self.a.user, self.goal.id, {"title": "Buy timber", "due_date": "2026-10-15"}
        )
        projects.ask(self.a, self.a.user, self.goal.id, self.step.id, [str(self.mb.id)])
        projects.respond(self.b, self.b.user, self.goal.id, self.step.id, {"answer": "yes"})

    def test_one_nudge_at_nine_local_the_day_before(self):
        with _PushCapture(self) as cap:
            first = run_due_nudges(now=self.tokyo_nine)
            second = run_due_nudges(now=self.tokyo_nine + timedelta(minutes=30))
        self.assertEqual(first, {"claimed": 1, "sent": 1})
        self.assertEqual(second["claimed"], 0)
        [call] = cap.to(self.b)
        self.assertEqual(call["extra"]["type"], "step_due")
        self.assertIn("“Buy timber” is due tomorrow", call["body"])

    def test_no_nudge_at_other_hours_days_or_for_done_steps(self):
        with _PushCapture(self) as cap:
            run_due_nudges(now=self.tokyo_nine + timedelta(hours=1))
            run_due_nudges(now=self.tokyo_nine - timedelta(days=1))
            projects.complete(self.b, self.b.user, self.goal.id, self.step.id)
            run_due_nudges(now=self.tokyo_nine)
        self.assertEqual(cap.calls, [])

    def test_moving_the_due_date_earns_one_fresh_nudge(self):
        with _PushCapture(self) as cap:
            run_due_nudges(now=self.tokyo_nine)
            version = SharedGoalStep.objects.get(id=self.step.id).version
            step = projects.patch_step(
                self.b, self.goal.id, self.step.id, {"version": version, "due_date": "2026-10-16"}
            )
            self.assertEqual(step.due_date.isoformat(), "2026-10-16")
            run_due_nudges(now=self.tokyo_nine + timedelta(days=1))
        self.assertEqual(len(cap.to(self.b)), 2)


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class MomentsGoalLinkAndPrivacyTests(TestCase):
    def setUp(self):
        self.a, self.b = _tenant("gl_a"), _tenant("gl_b")
        _profile(self.a, "aya")
        _profile(self.b, "ben")
        edge = _edge(self.a, self.b)
        self.goal = services.create_mission(self.a, self.a.user, member_friendship_ids=[str(edge.id)], title="Garden")
        services.join_mission(self.b, self.b.user, self.goal.id)
        self.mb = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.b)
        self.step = projects.create_step(self.a, self.a.user, self.goal.id, {"title": "Buy timber"})
        projects.ask(self.a, self.a.user, self.goal.id, self.step.id, [str(self.mb.id)])

    def test_asks_are_decision_moments_for_the_asked_member_only(self):
        mine = [m for m in services.neighborhood_home(self.b)["moments"] if m["kind"] == "project_ask"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["step_title"], "Buy timber")
        self.assertEqual(mine[0]["mission_id"], str(self.goal.id))
        self.assertTrue(mine[0]["asked_by_name"])
        self.assertFalse([m for m in services.neighborhood_home(self.a)["moments"] if m["kind"] == "project_ask"])
        projects.respond(self.b, self.b.user, self.goal.id, self.step.id, {"answer": "no"})
        self.assertFalse([m for m in services.neighborhood_home(self.b)["moments"] if m["kind"] == "project_ask"])

    def test_goal_link_moves_my_steps_under_my_goal_privately(self):
        projects.respond(self.b, self.b.user, self.goal.id, self.step.id, {"answer": "yes"})
        task_id = SharedGoalStepAssignment.objects.get(step=self.step, membership=self.mb).task_id
        horizon = Goal.objects.create(tenant=self.b, title="Get outside more")
        projects.set_linked_goal(self.b, self.goal.id, str(horizon.id))
        self.assertEqual(Task.objects.get(id=task_id).parent_goal_id, horizon.id)

        # A step I take later is minted straight under the goal.
        later = projects.create_step(self.a, self.a.user, self.goal.id, {"title": "Water beds"})
        projects.ask(self.a, self.a.user, self.goal.id, later.id, [str(self.mb.id)])
        projects.respond(self.b, self.b.user, self.goal.id, later.id, {"answer": "yes"})
        later_task = SharedGoalStepAssignment.objects.get(step=later, membership=self.mb).task_id
        self.assertEqual(Task.objects.get(id=later_task).parent_goal_id, horizon.id)

        # Only I see the link, with my goal's title.
        mine = projects.get_plan(self.b, self.goal.id)
        me = next(m for m in mine["members"] if m["id"] == str(self.mb.id))
        self.assertEqual(me["linked_goal_id"], str(horizon.id))
        self.assertEqual(me["linked_goal_title"], "Get outside more")
        theirs = projects.get_plan(self.a, self.goal.id)
        them = next(m for m in theirs["members"] if m["id"] == str(self.mb.id))
        self.assertNotIn("linked_goal_id", them)
        self.assertNotIn("linked_goal_title", them)

        # Unlinking moves back only what the link placed.
        projects.set_linked_goal(self.b, self.goal.id, None)
        self.assertIsNone(Task.objects.get(id=task_id).parent_goal_id)

    def test_cannot_link_someone_elses_goal(self):
        theirs = Goal.objects.create(tenant=self.a, title="Aya's goal")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.b.user).access_token}")
        res = client.patch(
            f"/api/v1/friends/missions/{self.goal.id}/membership/", {"linked_goal_id": str(theirs.id)}, format="json"
        )
        self.assertEqual(res.status_code, 404)
        # Only linked_goal_id and muted (a real boolean) can be changed here.
        res = client.patch(f"/api/v1/friends/missions/{self.goal.id}/membership/", {"muted": "yes"}, format="json")
        self.assertEqual(res.status_code, 400)
        res = client.patch(f"/api/v1/friends/missions/{self.goal.id}/membership/", {"role": "owner"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_plan_payload_never_carries_tenant_ids(self):
        projects.respond(self.b, self.b.user, self.goal.id, self.step.id, {"answer": "yes"})
        projects.complete(self.b, self.b.user, self.goal.id, self.step.id)
        plan = projects.get_plan(self.a, self.goal.id)
        step = next(s for s in plan["steps"] if s["id"] == str(self.step.id))
        self.assertNotIn("completed_by_id", step)
        self.assertEqual(step["completed_by_membership_id"], str(self.mb.id))
        flat = str(plan)
        for tenant in (self.a, self.b):
            self.assertNotIn(str(tenant.id), flat)
