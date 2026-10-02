"""Projects: people come and go. Stepping back and leaving free steps out loud,
"still yours?", passing on ownership, "not me — maybe them", and showing the work
(done note + the optional second look)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.journal.models import Task

from . import plan_projection, services
from . import project_services as projects
from .models import SharedGoalMembership, SharedGoalStep, SharedGoalStepAssignment, SharedGoalUpdate
from .project_notifications import run_still_yours_nudges
from .test_pr6 import _edge, _profile, _tenant
from .test_projects_v2_p1b import _PushCapture


class _Base(TestCase):
    prefix = "pc"

    def setUp(self):
        self.a, self.b, self.c = [_tenant(f"{self.prefix}_{n}") for n in "abc"]
        for tenant, name in [(self.a, "aya"), (self.b, "ben"), (self.c, "cleo")]:
            _profile(tenant, name)
        self.ab, self.ac = _edge(self.a, self.b), _edge(self.a, self.c)
        self.goal = services.create_mission(
            self.a, self.a.user, member_friendship_ids=[str(self.ab.id), str(self.ac.id)], title="Garden"
        )
        services.join_mission(self.b, self.b.user, self.goal.id)
        services.join_mission(self.c, self.c.user, self.goal.id)

    def member(self, tenant):
        return SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=tenant)

    def client_for(self, tenant):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(tenant.user).access_token}")
        return client

    def step(self, title="Buy timber", **fields):
        return projects.create_step(self.a, self.a.user, self.goal.id, {"title": title, **fields})

    def give(self, step, tenant):
        """``tenant`` is asked and says yes."""
        projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.member(tenant).id)])
        projects.respond(tenant, tenant.user, self.goal.id, step.id, {"answer": "yes"})

    def plan_step(self, viewer, step):
        plan = projects.get_plan(viewer, self.goal.id)
        return next(s for s in plan["steps"] if s["id"] == str(step.id))

    def fresh(self, step):
        return SharedGoalStep.objects.get(id=step.id)

    def url(self, tail):
        return f"/api/v1/friends/missions/{self.goal.id}/{tail}"


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class SteppingBackTests(_Base):
    prefix = "sb"

    def test_stepping_back_frees_the_step_with_a_hand_off_line_and_tells_the_others(self):
        step = self.step()
        self.give(step, self.b)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.b).post(
                self.url("step-back/"),
                {"steps": [{"step_id": str(step.id), "note": "Timber is ordered, pick up Thursday."}]},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"released": 1})
        seen = self.plan_step(self.a, step)
        self.assertEqual(seen["owners"], [])
        self.assertEqual(seen["attention"], "open_again")
        self.assertEqual(seen["released"][0]["membership_id"], str(self.member(self.b).id))
        self.assertEqual(seen["released"][0]["note"], "Timber is ordered, pick up Thursday.")
        # Ben stays in the project; the others hear it once, without a reason.
        self.assertEqual(self.member(self.b).status, "active")
        self.assertEqual(cap.to(self.b), [])
        [call] = cap.to(self.a)
        self.assertEqual(call["extra"]["kind"], "step_released")
        # Builds that predate this kind still open the step: the routing type is one they know.
        self.assertEqual((call["extra"]["type"], call["extra"]["step_id"]), ("step_answer", str(step.id)))
        self.assertIn("stepped back from “Buy timber”", call["body"])
        self.assertNotIn("Thursday", call["body"])
        self.assertEqual(len(cap.to(self.c)), 1)

    def test_all_lets_go_of_every_open_step_but_not_done_ones_and_keeps_the_private_task(self):
        done, open_a, open_b = self.step("Measure"), self.step("Buy"), self.step("Build")
        for step in (done, open_a, open_b):
            self.give(step, self.b)
        projects.complete(self.b, self.b.user, self.goal.id, done.id)
        task_id = SharedGoalStepAssignment.objects.get(step=open_a, membership=self.member(self.b)).task_id
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            released = projects.step_back(self.b, self.b.user, self.goal.id, {"all": True})
        self.assertEqual({s.id for s in released}, {open_a.id, open_b.id})
        self.assertIn("stepped back from 2 steps", cap.to(self.a)[0]["body"])
        self.assertEqual(self.fresh(done).status, "done")
        self.assertEqual(len(self.plan_step(self.a, done)["owners"]), 1)
        self.assertTrue(Task.objects.filter(id=task_id, tenant=self.b).exists())
        self.assertEqual(SharedGoalUpdate.objects.filter(shared_goal=self.goal, kind="step_released").count(), 2)

    def test_someone_else_can_take_a_freed_step_and_the_one_who_left_it_can_be_asked_again(self):
        step = self.step()
        self.give(step, self.b)
        projects.step_back(self.b, self.b.user, self.goal.id, {"all": True})
        with self.assertRaises(PermissionDenied):
            projects.complete(self.b, self.b.user, self.goal.id, step.id)
        self.give(step, self.c)
        projects.complete(self.c, self.c.user, self.goal.id, step.id)
        self.assertEqual(self.fresh(step).status, "done")
        other = self.step("Water")
        self.give(other, self.b)
        projects.step_back(self.b, self.b.user, self.goal.id, {"all": True})
        self.give(other, self.b)
        self.assertEqual(len(self.plan_step(self.a, other)["owners"]), 1)
        self.assertEqual(self.plan_step(self.a, other)["released"], [])

    def test_an_ask_i_never_answered_is_closed_quietly_not_announced_as_stepping_back(self):
        step = self.step()
        projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.member(self.b).id)])
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            released = projects.step_back(self.b, self.b.user, self.goal.id, {"all": True})
        self.assertEqual(released, [])
        # Whoever asked hears a plain "can't take it" — never "stepped back" / "open again".
        self.assertEqual(cap.to(self.c), [])
        [call] = cap.to(self.a)
        self.assertEqual(call["extra"]["type"], "step_answer")
        self.assertIn("can’t take “Buy timber” this time", call["body"])
        seen = self.plan_step(self.a, step)
        self.assertEqual((seen["released"], seen["attention"]), ([], None))
        self.assertEqual(
            SharedGoalStepAssignment.objects.get(step=step, membership=self.member(self.b)).status, "declined"
        )

    def test_only_people_in_the_project_can_use_the_new_actions(self):
        step = self.step()
        self.give(step, self.b)
        outsider = _tenant("sb_out")
        _profile(outsider, "olga")
        services.leave_mission(self.c, self.goal.id)
        for caller in (outsider, self.c):
            client = self.client_for(caller)
            for tail, body in [
                ("step-back/", {"all": True}),
                ("owners/", {"membership_id": str(self.member(self.b).id), "role": "owner"}),
                (f"steps/{step.id}/second-look/", {"on": True}),
                (f"steps/{step.id}/confirm/", {}),
                (f"steps/{step.id}/question/", {}),
                (f"steps/{step.id}/keep/", {}),
            ]:
                with self.subTest(caller=caller.id, tail=tail):
                    self.assertEqual(client.post(self.url(tail), body, format="json").status_code, 404)

    def test_step_back_needs_a_choice(self):
        with self.assertRaises(ValidationError):
            projects.step_back(self.b, self.b.user, self.goal.id, {})


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class LeavingTests(_Base):
    prefix = "lv"

    def test_leaving_frees_open_steps_and_tells_the_group(self):
        step = self.step()
        self.give(step, self.b)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.b).post(self.url("leave/"), {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["released"], 1)
        seen = self.plan_step(self.a, step)
        self.assertEqual(seen["attention"], "open_again")
        self.assertEqual(seen["released"][0]["membership_id"], str(self.member(self.b).id))
        [call] = cap.to(self.a)
        self.assertEqual(call["extra"]["kind"], "member_left")
        self.assertIn("left “Garden” — 1 step is open again.", call["body"])
        self.assertEqual(cap.to(self.b), [])
        self.assertTrue(SharedGoalUpdate.objects.filter(shared_goal=self.goal, kind="member_left").exists())

    def test_the_new_owner_is_told_when_the_last_owner_leaves(self):
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            services.leave_mission(self.a, self.goal.id)
        # Whoever joined first inherits; the test doesn't care which of the two it is.
        heir, other = (self.b, self.c) if self.member(self.b).role == "owner" else (self.c, self.b)
        self.assertEqual((self.member(heir).role, self.member(other).role), ("owner", "member"))
        [to_heir] = cap.to(heir)
        self.assertEqual(to_heir["extra"]["kind"], "project_owner")
        self.assertIn("You’re looking after it now.", to_heir["body"])
        [to_other] = cap.to(other)
        self.assertEqual(to_other["extra"]["kind"], "member_left")
        self.assertNotIn("looking after", to_other["body"])

    def test_leaving_a_mission_outside_the_rollout_pushes_nobody(self):
        with (
            override_settings(PROJECTS_V2_TENANT_IDS=""),
            _PushCapture(self) as cap,
            self.captureOnCommitCallbacks(execute=True),
        ):
            services.leave_mission(self.b, self.goal.id)
        self.assertEqual(self.member(self.b).status, "left")
        self.assertEqual(cap.calls, [])

    def test_an_owner_shares_then_passes_on_looking_after_the_project(self):
        target = str(self.member(self.b).id)
        # A plain member can't hand out ownership.
        response = self.client_for(self.c).post(
            self.url("owners/"), {"membership_id": target, "role": "owner"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        # The only owner can't step down before someone else is one.
        mine = str(self.member(self.a).id)
        with self.assertRaises(ValidationError):
            projects.set_owner_role(self.a, self.a.user, self.goal.id, {"membership_id": mine, "role": "member"})
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.a).post(
                self.url("owners/"), {"membership_id": target, "role": "owner"}, format="json"
            )
        self.assertEqual(response.json(), {"membership_id": target, "role": "owner"})
        self.assertEqual(cap.to(self.b)[0]["extra"]["kind"], "project_owner")
        # Nobody demotes someone else; stepping down yourself is fine now.
        with self.assertRaises(PermissionDenied):
            projects.set_owner_role(self.a, self.a.user, self.goal.id, {"membership_id": target, "role": "member"})
        projects.set_owner_role(self.a, self.a.user, self.goal.id, {"membership_id": mine, "role": "member"})
        self.assertEqual(self.member(self.a).role, "member")
        self.assertEqual(self.member(self.b).role, "owner")


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class NotMeMaybeThemTests(_Base):
    prefix = "nm"

    def test_declining_can_point_at_someone_without_asking_them(self):
        step = self.step()
        projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.member(self.b).id)])
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.b).post(
                self.url(f"steps/{step.id}/respond/"),
                {"answer": "other", "suggest_membership_id": str(self.member(self.c).id)},
                format="json",
            )
        self.assertEqual(response.json()["status"], "declined")
        assignment = SharedGoalStepAssignment.objects.get(step=step, membership=self.member(self.b))
        self.assertEqual(assignment.suggested_membership_id, self.member(self.c).id)
        # Cleo is not asked by this — only Aya (who asked) hears about it.
        self.assertFalse(SharedGoalStepAssignment.objects.filter(step=step, membership=self.member(self.c)).exists())
        self.assertEqual(cap.to(self.c), [])
        self.assertIn("maybe someone else can", cap.to(self.a)[0]["body"])
        seen = next(a for a in self.plan_step(self.a, step)["assignments"] if a["status"] == "declined")
        self.assertEqual(seen["suggested_membership_id"], str(self.member(self.c).id))

    def test_you_cannot_suggest_yourself_or_a_stranger(self):
        step = self.step()
        projects.ask(self.a, self.a.user, self.goal.id, step.id, [str(self.member(self.b).id)])
        elsewhere = services.create_mission(
            self.a, self.a.user, member_friendship_ids=[str(self.ac.id)], title="Another project"
        )
        stranger = SharedGoalMembership.objects.get(shared_goal=elsewhere, tenant=self.c)
        response = self.client_for(self.b).post(
            self.url(f"steps/{step.id}/respond/"),
            {"answer": "other", "suggest_membership_id": str(stranger.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        response = self.client_for(self.b).post(
            self.url(f"steps/{step.id}/respond/"),
            {"answer": "other", "suggest_membership_id": str(self.member(self.b).id)},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            SharedGoalStepAssignment.objects.get(step=step, membership=self.member(self.b)).status, "asked"
        )


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ShowingTheWorkTests(_Base):
    prefix = "sw"

    def test_a_done_note_and_link_ride_with_the_tick_and_clear_on_reopen(self):
        step = self.step()
        self.give(step, self.b)
        response = self.client_for(self.b).post(
            self.url(f"steps/{step.id}/complete/"),
            {"note": "Receipt is in the shed.", "link": "https://example.org/receipt"},
            format="json",
        )
        self.assertEqual(response.json()["status"], "done")
        seen = self.plan_step(self.a, step)
        self.assertEqual(
            (seen["done_note"], seen["done_link"]), ("Receipt is in the shed.", "https://example.org/receipt")
        )
        projects.complete(self.b, self.b.user, self.goal.id, step.id, reopen=True)
        seen = self.plan_step(self.a, step)
        self.assertEqual((seen["status"], seen["done_note"], seen["done_link"]), ("open", "", ""))

    def test_a_refusal_reaches_the_app_as_a_sentence_it_will_show(self):
        step = self.step()
        self.give(step, self.b)
        response = self.client_for(self.c).post(self.url(f"steps/{step.id}/question/"), {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"non_field_errors": ["You can ask about a step once it's ticked off."]})

    def test_a_link_must_be_a_web_link(self):
        step = self.step()
        self.give(step, self.b)
        with self.assertRaises(ValidationError):
            projects.complete(self.b, self.b.user, self.goal.id, step.id, data={"link": "javascript:alert(1)"})
        self.assertEqual(self.fresh(step).status, "open")

    def test_any_member_asks_for_a_second_look_and_only_the_asker_or_an_owner_switches_it_off(self):
        step = self.step()
        self.give(step, self.b)
        response = self.client_for(self.c).post(self.url(f"steps/{step.id}/second-look/"), {"on": True}, format="json")
        self.assertEqual(response.status_code, 200)
        seen = self.plan_step(self.b, step)
        self.assertTrue(seen["needs_review"])
        self.assertEqual(seen["review_set_by_membership_id"], str(self.member(self.c).id))
        # The doer can't wave it away alone.
        with self.assertRaises(PermissionDenied):
            projects.set_second_look(self.b, self.goal.id, step.id, False)
        projects.set_second_look(self.c, self.goal.id, step.id, False)
        self.assertFalse(self.fresh(step).needs_review)
        projects.set_second_look(self.b, self.goal.id, step.id, True)
        projects.set_second_look(self.a, self.goal.id, step.id, False)  # project owner
        self.assertFalse(self.fresh(step).needs_review)

    def test_a_step_that_needs_a_look_waits_until_someone_else_confirms(self):
        first, second = self.step("Send the money"), self.step("Order seeds")
        projects.dependency_write(self.a, self.goal.id, {"blocker_id": str(first.id), "blocked_id": str(second.id)})
        self.give(first, self.b)
        projects.set_second_look(self.c, self.goal.id, first.id, True)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.complete(self.b, self.b.user, self.goal.id, first.id, data={"note": "Sent."})
        self.assertEqual(self.fresh(first).status, "in_review")
        seen = self.plan_step(self.a, first)
        self.assertEqual(seen["attention"], "needs_look")
        # Not done yet: the step waiting on it is still blocked, and nothing says "done".
        self.assertEqual(self.plan_step(self.a, second)["blocked_by_open"], [str(first.id)])
        self.assertEqual(projects.get_plan(self.a, self.goal.id)["done_count"], 0)
        self.assertFalse(SharedGoalUpdate.objects.filter(shared_goal=self.goal, kind="step_done").exists())
        self.assertEqual(cap.to(self.b), [])
        self.assertEqual(cap.to(self.a)[0]["extra"]["kind"], "step_needs_look")
        # The doer can't confirm their own work; it can't be switched off mid-look.
        with self.assertRaises(PermissionDenied):
            projects.confirm(self.b, self.b.user, self.goal.id, first.id)
        with self.assertRaises(ValidationError):
            projects.set_second_look(self.c, self.goal.id, first.id, False)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.a).post(self.url(f"steps/{first.id}/confirm/"), {}, format="json")
        self.assertEqual(response.json()["status"], "done")
        seen = self.plan_step(self.b, first)
        self.assertEqual(seen["reviewed_by_membership_id"], str(self.member(self.a).id))
        self.assertIsNone(seen["attention"])
        self.assertEqual(self.plan_step(self.a, second)["blocked_by_open"], [])
        self.assertEqual(cap.to(self.b)[0]["extra"]["kind"], "step_confirmed")
        self.assertTrue(SharedGoalUpdate.objects.filter(shared_goal=self.goal, kind="step_done").exists())

    def test_ticking_my_private_task_also_waits_for_the_look(self):
        step = self.step()
        self.give(step, self.b)
        projects.set_second_look(self.a, self.goal.id, step.id, True)
        task = Task.objects.get(
            id=SharedGoalStepAssignment.objects.get(step=step, membership=self.member(self.b)).task_id
        )
        task.complete()
        self.assertEqual(self.fresh(step).status, "in_review")

    def test_the_owner_can_take_it_back_while_it_waits(self):
        step = self.step()
        self.give(step, self.b)
        projects.set_second_look(self.a, self.goal.id, step.id, True)
        projects.complete(self.b, self.b.user, self.goal.id, step.id)
        projects.complete(self.b, self.b.user, self.goal.id, step.id, reopen=True)
        self.assertEqual(self.fresh(step).status, "open")
        self.assertTrue(self.fresh(step).needs_review)

    def test_the_doer_cannot_wave_the_look_away_even_as_project_owner(self):
        step = self.step()
        self.give(step, self.a)  # Aya is the project owner AND holds the step
        projects.set_second_look(self.c, self.goal.id, step.id, True)
        with self.assertRaises(PermissionDenied):
            projects.set_second_look(self.a, self.goal.id, step.id, False)
        self.assertTrue(self.fresh(step).needs_review)

    def test_a_co_owner_cannot_confirm_and_the_milestone_waits_and_done_is_credited_to_the_doer(self):
        milestone = projects.milestone_write(self.a, self.goal.id, {"title": "Paid"})
        step = self.step(milestone_id=str(milestone.id))
        self.give(step, self.b)
        self.give(step, self.c)
        projects.set_second_look(self.a, self.goal.id, step.id, True)
        projects.complete(self.b, self.b.user, self.goal.id, step.id)
        with self.assertRaises(PermissionDenied):
            projects.confirm(self.c, self.c.user, self.goal.id, step.id)  # holds the step too
        plan = projects.get_plan(self.a, self.goal.id)
        self.assertIsNone(plan["milestones"][0]["reached_at"])
        nxt = self.step("Order seeds")
        projects.dependency_write(self.a, self.goal.id, {"blocker_id": str(step.id), "blocked_id": str(nxt.id)})
        self.give(nxt, self.b)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.confirm(self.a, self.a.user, self.goal.id, step.id)
        self.assertIsNotNone(projects.get_plan(self.a, self.goal.id)["milestones"][0]["reached_at"])
        done = SharedGoalUpdate.objects.get(shared_goal=self.goal, kind="step_done")
        self.assertEqual(done.tenant_id, self.b.id)
        # The doer hears everything that follows from it; the one who tapped confirm doesn't.
        kinds = sorted(c["extra"].get("kind", c["extra"]["type"]) for c in cap.to(self.b))
        self.assertEqual(kinds, ["milestone_reached", "step_confirmed", "step_unblocked"])
        self.assertEqual(cap.to(self.a), [])

    def test_a_question_about_a_step_nobody_holds_reaches_the_project_owner(self):
        step = self.step()
        self.give(step, self.b)
        projects.complete(self.b, self.b.user, self.goal.id, step.id)
        services.leave_mission(self.b, self.goal.id)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.question(self.c, self.c.user, self.goal.id, step.id, {})
        self.assertEqual([c["extra"]["kind"] for c in cap.to(self.a)], ["step_question"])

    def test_a_waiting_step_cannot_be_patched_back_to_in_progress(self):
        step = self.step()
        self.give(step, self.b)
        projects.set_second_look(self.a, self.goal.id, step.id, True)
        projects.complete(self.b, self.b.user, self.goal.id, step.id)
        version = self.fresh(step).version
        with self.assertRaises(ValidationError):
            projects.patch_step(self.b, self.goal.id, step.id, {"version": version, "status": "in_progress"})
        self.assertEqual(self.fresh(step).status, "in_review")

    def test_a_step_never_waits_for_a_look_nobody_can_give(self):
        # Everyone left in the project holds the step: ticking goes straight to done.
        services.leave_mission(self.c, self.goal.id)
        step = self.step()
        self.give(step, self.b)
        projects.set_second_look(self.b, self.goal.id, step.id, True)
        projects.ask(self.b, self.b.user, self.goal.id, step.id, [str(self.member(self.a).id)])
        projects.respond(self.a, self.a.user, self.goal.id, step.id, {"answer": "yes"})
        projects.complete(self.b, self.b.user, self.goal.id, step.id)
        self.assertEqual(self.fresh(step).status, "done")

    def test_a_project_owner_can_reopen_a_step_stuck_waiting_but_not_finished_work(self):
        waiting, finished = self.step("Send the money"), self.step("Order seeds")
        for step in (waiting, finished):
            self.give(step, self.b)
        projects.set_second_look(self.c, self.goal.id, waiting.id, True)
        projects.complete(self.b, self.b.user, self.goal.id, waiting.id)
        projects.complete(self.b, self.b.user, self.goal.id, finished.id)
        # While the doer is still here, reopening is theirs — not the project owner's.
        with self.assertRaises(PermissionDenied):
            projects.complete(self.a, self.a.user, self.goal.id, waiting.id, reopen=True)
        services.leave_mission(self.b, self.goal.id)  # the doer is gone; nobody holds the step
        with self.assertRaises(PermissionDenied):
            projects.complete(self.c, self.c.user, self.goal.id, waiting.id, reopen=True)  # a plain member
        with self.assertRaises(PermissionDenied):
            projects.complete(self.a, self.a.user, self.goal.id, finished.id, reopen=True)  # done work stays done
        projects.complete(self.a, self.a.user, self.goal.id, waiting.id, reopen=True)
        fresh = self.fresh(waiting)
        self.assertEqual((fresh.status, fresh.done_note, fresh.completed_by_id), ("open", "", None))
        # It reads as open again ("Ben had this"), not as an ownerless mystery.
        seen = self.plan_step(self.a, waiting)
        self.assertEqual(seen["attention"], "open_again")
        self.assertEqual(seen["released"][0]["membership_id"], str(self.member(self.b).id))

    def test_someone_outside_the_rollout_cannot_be_the_one_to_look(self):
        step = self.step()
        self.give(step, self.a)
        projects.set_second_look(self.a, self.goal.id, step.id, True)
        with override_settings(PROJECTS_V2_TENANT_IDS=str(self.a.id)):
            projects.complete(self.a, self.a.user, self.goal.id, step.id)
        self.assertEqual(self.fresh(step).status, "done")

    def test_a_second_look_needs_a_second_person(self):
        services.leave_mission(self.b, self.goal.id)
        services.leave_mission(self.c, self.goal.id)
        step = self.step()
        with self.assertRaises(ValidationError):
            projects.set_second_look(self.a, self.goal.id, step.id, True)

    def test_asking_about_a_done_step_nudges_the_owner_once_a_day_and_never_reopens(self):
        step = self.step()
        self.give(step, self.b)
        with self.assertRaises(ValidationError):
            projects.question(self.c, self.c.user, self.goal.id, step.id, {})
        projects.complete(self.b, self.b.user, self.goal.id, step.id)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.c).post(
                self.url(f"steps/{step.id}/question/"), {"note": "Did the refund arrive?"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fresh(step).status, "done")
        [call] = cap.to(self.b)
        self.assertEqual(call["extra"]["kind"], "step_question")
        self.assertNotIn("refund", call["body"])
        with self.assertRaises(ValidationError):
            projects.question(self.c, self.c.user, self.goal.id, step.id, {})


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class MutingTests(_Base):
    prefix = "mu"

    def test_i_can_mute_a_project_and_then_get_no_pushes_from_it(self):
        step = self.step()
        self.give(step, self.b)
        response = self.client_for(self.a).patch(self.url("membership/"), {"muted": True}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["muted"], True)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.step_back(self.b, self.b.user, self.goal.id, {"all": True})
        self.assertEqual(cap.to(self.a), [])
        self.assertEqual(len(cap.to(self.c)), 1)
        # Only I see my own switch.
        mine_id = str(self.member(self.a).id)
        mine = next(m for m in projects.get_plan(self.a, self.goal.id)["members"] if m["id"] == mine_id)
        theirs = next(m for m in projects.get_plan(self.b, self.goal.id)["members"] if m["id"] == mine_id)
        self.assertEqual(mine["muted"], True)
        self.assertNotIn("muted", theirs)
        # Unmuting brings them back.
        self.client_for(self.a).patch(self.url("membership/"), {"muted": False}, format="json")
        self.give(step, self.c)
        with _PushCapture(self) as cap, self.captureOnCommitCallbacks(execute=True):
            projects.step_back(self.c, self.c.user, self.goal.id, {"all": True})
        self.assertEqual(len(cap.to(self.a)), 1)


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class QuietStepTests(_Base):
    prefix = "qs"

    def setUp(self):
        super().setUp()
        self.b.user.timezone = "Asia/Tokyo"
        self.b.user.save(update_fields=["timezone"])
        self.nine = datetime(2026, 10, 18, 9, 5, tzinfo=ZoneInfo("Asia/Tokyo"))  # due + 3
        self.step_ = self.step(due_date="2026-10-15")
        self.give(self.step_, self.b)

    def test_one_still_yours_three_days_past_due_and_keep_quiets_the_flag(self):
        with _PushCapture(self) as cap:
            early = run_still_yours_nudges(now=self.nine - timedelta(days=1))
            first = run_still_yours_nudges(now=self.nine)
            again = run_still_yours_nudges(now=self.nine + timedelta(days=1))
        self.assertEqual(early, {"claimed": 0, "sent": 0})
        self.assertEqual(first, {"claimed": 1, "sent": 1})
        self.assertEqual(again["claimed"], 0)
        [call] = cap.to(self.b)
        self.assertEqual(call["extra"]["kind"], "step_still_yours")
        self.assertEqual(cap.to(self.a), [])
        response = self.client_for(self.b).post(self.url(f"steps/{self.step_.id}/keep/"), {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(
            SharedGoalStepAssignment.objects.get(step=self.step_, membership=self.member(self.b)).kept_at
        )
        # Only the owner can say "still mine".
        with self.assertRaises(PermissionDenied):
            projects.keep_step(self.c, self.goal.id, self.step_.id)

    def test_no_burst_no_ancient_steps_and_not_right_after_taking_one(self):
        # Three more overdue steps for Ben: one push this morning, the rest on later mornings.
        for title, due in [("Old A", "2026-10-13"), ("Old B", "2026-10-14"), ("Ancient", "2026-09-01")]:
            self.give(self.step(title, due_date=due), self.b)
        SharedGoalStepAssignment.objects.filter(membership=self.member(self.b)).update(
            responded_at=self.nine - timedelta(days=10)
        )
        with _PushCapture(self) as cap:
            first = run_still_yours_nudges(now=self.nine)
            second = run_still_yours_nudges(now=self.nine + timedelta(days=1))
            third = run_still_yours_nudges(now=self.nine + timedelta(days=2))
            fourth = run_still_yours_nudges(now=self.nine + timedelta(days=3))
        self.assertEqual([r["sent"] for r in (first, second, third, fourth)], [1, 1, 1, 0])
        self.assertFalse(any("Ancient" in c["body"] for c in cap.calls))
        # Someone who took an already-late step yesterday isn't asked this morning.
        late = self.step("Just taken", due_date="2026-10-10")
        self.give(late, self.c)
        took = datetime(2026, 10, 17, 9, 5, tzinfo=ZoneInfo("UTC"))
        SharedGoalStepAssignment.objects.filter(step=late).update(responded_at=took)
        with _PushCapture(self) as cap:
            run_still_yours_nudges(now=datetime(2026, 10, 18, 9, 5, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(cap.to(self.c), [])

    def test_keep_clears_the_overdue_flag_in_the_real_plan(self):
        # Real rows, real clock: a step five days past due is flagged until its owner says "still mine".
        late = self.step("Late one", due_date=(timezone.localdate() - timedelta(days=5)).isoformat())
        self.give(late, self.b)
        self.assertEqual(self.plan_step(self.a, late)["attention"], "overdue")
        projects.keep_step(self.b, self.goal.id, late.id)
        self.assertIsNone(self.plan_step(self.a, late)["attention"])
        # And the morning sweep leaves a just-kept step alone.
        SharedGoalStepAssignment.objects.filter(step=late).update(responded_at=timezone.now() - timedelta(days=9))
        nine_today = timezone.now().astimezone(ZoneInfo("Asia/Tokyo")).replace(hour=9, minute=5)
        with _PushCapture(self) as cap:
            run_still_yours_nudges(now=nine_today)
        self.assertFalse(any("Late one" in c["body"] for c in cap.calls))

    def test_attention_is_a_schedule_fact(self):
        def attention(step, owners, assignments, today):
            return plan_projection._attention(step, owners, assignments, today)

        today = date(2026, 10, 18)
        owner = {"id": "m1"}
        accepted = {"status": "accepted", "membership_id": "m1", "kept_at": None}
        due = {"status": "open", "due_date": date(2026, 10, 15)}
        self.assertEqual(attention(due, [owner], [accepted], today), "overdue")
        self.assertIsNone(attention(due, [owner], [accepted], date(2026, 10, 17)))
        kept = {**accepted, "kept_at": datetime(2026, 10, 16, 3, 0, tzinfo=ZoneInfo("UTC"))}
        self.assertIsNone(attention(due, [owner], [kept], today))
        self.assertEqual(attention(due, [owner], [kept], date(2026, 10, 24)), "overdue")
        self.assertEqual(attention(due, [], [{"status": "released", "membership_id": "m1"}], today), "open_again")
        self.assertEqual(attention(due, [], [], today), "unowned_due")
        self.assertIsNone(attention({"status": "open", "due_date": date(2026, 11, 30)}, [], [], today))
        self.assertIsNone(attention({"status": "open", "due_date": None}, [], [], today))
        self.assertEqual(attention({"status": "in_review", "due_date": None}, [owner], [accepted], today), "needs_look")
        self.assertIsNone(attention({"status": "done", "due_date": date(2026, 10, 1)}, [owner], [accepted], today))
