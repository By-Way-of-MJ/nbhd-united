"""Private step preparation: holder authority, lifecycle, API and query bounds."""

import ast
import json
from importlib import import_module
from pathlib import Path
from uuid import uuid4

from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from . import access, project_assistant, project_prep, services
from . import project_services as projects
from .models import SharedGoalMembership, SharedGoalStep, SharedGoalStepAssignment, StepPrep
from .test_access_chokepoint import ACCESS_MODULE, _is_excluded, _manager_accesses
from .test_pr6 import _edge, _profile, _tenant


@override_settings(PROJECTS_V2_TENANT_IDS="*", STEP_PREP_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class StepPrepTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner, self.holder, self.invitee, self.stranger = [_tenant("prep_" + name) for name in "abcd"]
        for tenant, name in [(self.owner, "aya"), (self.holder, "ben"), (self.invitee, "cleo")]:
            _profile(tenant, name)
        self.goal = services.create_mission(
            self.owner,
            self.owner.user,
            member_friendship_ids=[str(_edge(self.owner, t).id) for t in [self.holder, self.invitee]],
            title="Garden",
        )
        services.join_mission(self.holder, self.holder.user, self.goal.id)
        self.member = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.holder)
        self.owner_member = SharedGoalMembership.objects.get(shared_goal=self.goal, tenant=self.owner)
        self.step = projects.create_step(
            self.owner,
            self.owner.user,
            self.goal.id,
            {"title": "Dig", "description": "Near the fence", "start_date": "2026-11-03", "due_date": "2026-11-08"},
        )
        self.assignment = SharedGoalStepAssignment.objects.create(
            membership=self.member, step=self.step, status="accepted", asked_by=self.owner
        )
        self.base = f"/api/v1/friends/missions/{self.goal.id}/"
        self.url = self.base + f"steps/{self.step.id}/prep/"
        self.list_url = "/api/v1/friends/step-preps/"

    def jwt(self, tenant=None):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user((tenant or self.holder).user).access_token}"
        )
        return client

    def start(self, tenant=None, url=None):
        return self.jwt(tenant).post(url or self.url, {}, format="json")

    def row(self):
        return StepPrep.objects.get(membership=self.member, step=self.step)

    def waiting(self):
        response = self.start()
        self.assertEqual(response.status_code, 200, response.data)
        StepPrep.objects.filter(id=response.data["id"]).update(
            status="needs_choice", question="Which supplier?", choices=["Local", "Online"], doc_slug="private-prep-doc"
        )
        return self.row()

    def test_start_by_holder_and_payload(self):
        response = self.start()
        self.assertEqual(response.status_code, 200, response.data)
        prep = self.row()
        self.assertEqual(prep.tenant_id, self.holder.id)
        self.assertEqual(prep.status, "working")
        self.assertEqual(
            response.json(),
            {
                "id": str(prep.id),
                "mission_id": str(self.goal.id),
                "step_id": str(self.step.id),
                "project_title": "Garden",
                "status": "working",
                "brief": {
                    "title": "Dig",
                    "description": "Near the fence",
                    "start_date": "2026-11-03",
                    "due_date": "2026-11-08",
                },
                "question": "",
                "choices": [],
                "doc_slug": "",
                "updated_at": prep.updated_at.isoformat(),
            },
        )
        self.assertEqual(prep.answer, "")
        self.assertIsNone(prep.last_run_at)
        self.assertIsNone(prep.last_seen_note_at)
        self.assertIsNotNone(prep.created_at)

    @override_settings(STEP_PREP_TENANT_IDS="")
    def test_flag_off_returns_404_on_all_four_endpoints(self):
        for suffix in ["", "stop/", "choose/"]:
            response = self.jwt().post(self.url + suffix, {"choice": "Local"}, format="json")
            self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(self.jwt().get(self.list_url).status_code, 404)
        self.assertFalse(StepPrep.objects.exists())

    def test_flag_is_per_tenant_and_fails_closed(self):
        for flag in [str(self.owner.id), "invalid", "prefix*", " "]:
            with self.subTest(flag=flag), override_settings(STEP_PREP_TENANT_IDS=flag):
                self.assertEqual(self.start().status_code, 404)
        with override_settings(STEP_PREP_TENANT_IDS=f"invalid, {str(self.holder.id).upper()} "):
            self.assertEqual(self.start().status_code, 200)
            for suffix in ["", "stop/", "choose/"]:
                self.assertEqual(self.jwt(self.owner).post(self.url + suffix, {}, format="json").status_code, 404)
            self.assertEqual(self.jwt(self.owner).get(self.list_url).status_code, 404)

    def test_app_authentication_required_for_all_routes(self):
        client = APIClient()
        for suffix in ["", "stop/", "choose/"]:
            self.assertEqual(client.post(self.url + suffix, {}, format="json").status_code, 401)
        self.assertEqual(client.get(self.list_url).status_code, 401)

    def test_start_refused_for_nonmember_invited_and_declined_members(self):
        for tenant in [self.stranger, self.invitee]:
            self.assertEqual(self.start(tenant).status_code, 404)
        SharedGoalMembership.objects.filter(shared_goal=self.goal, tenant=self.invitee).update(status="declined")
        self.assertEqual(self.start(self.invitee).status_code, 404)
        self.assertFalse(StepPrep.objects.exists())

    def test_start_refused_without_accepted_assignment_even_for_project_owner(self):
        self.assertEqual(self.start(self.owner).status_code, 403)
        for status in ["asked", "declined", "countered", "released"]:
            with self.subTest(status=status):
                SharedGoalStepAssignment.objects.filter(id=self.assignment.id).update(status=status)
                self.assertEqual(self.start().status_code, 403)
        self.assertFalse(StepPrep.objects.exists())

    def test_start_refused_on_closed_steps(self):
        for status in ["done", "skipped", "in_review"]:
            with self.subTest(status=status):
                SharedGoalStep.objects.filter(id=self.step.id).update(status=status)
                self.assertEqual(self.start().status_code, 400)
        self.assertFalse(StepPrep.objects.exists())

    def test_start_on_in_progress_step_and_null_dates(self):
        SharedGoalStep.objects.filter(id=self.step.id).update(status="in_progress", start_date=None, due_date=None)
        response = self.start()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["brief"]["start_date"])
        self.assertIsNone(response.data["brief"]["due_date"])

    def test_start_refused_on_finished_project(self):
        projects.finish_project(self.owner, self.owner.user, self.goal.id, {"version": self.goal.version})
        response = self.start()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data, {"detail": "This project is finished — reopen it to change it."})
        self.assertFalse(StepPrep.objects.exists())

    def test_step_and_mission_ids_must_match(self):
        other = services.create_mission(self.holder, self.holder.user, member_friendship_ids=[], title="Other")
        wrong = f"/api/v1/friends/missions/{other.id}/steps/{self.step.id}/prep/"
        self.assertEqual(self.start(url=wrong).status_code, 404)
        self.start()
        for suffix in ["stop/", "choose/"]:
            self.assertEqual(self.jwt().post(wrong + suffix, {"choice": "Local"}, format="json").status_code, 404)
        missing = self.base + f"steps/{uuid4()}/prep/"
        self.assertEqual(self.start(url=missing).status_code, 404)

    def test_brief_is_snapshot_and_double_start_preserves_every_field(self):
        prep = self.waiting()
        now = timezone.now()
        StepPrep.objects.filter(id=prep.id).update(answer="An earlier answer", last_run_at=now, last_seen_note_at=now)
        before = StepPrep.objects.values().get(id=prep.id)
        projects.patch_step(
            self.holder,
            self.goal.id,
            self.step.id,
            {
                "version": self.step.version,
                "title": "Dig deeper",
                "description": "By the gate",
                "due_date": "2026-11-09",
            },
        )
        response = self.start()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], str(prep.id))
        self.assertEqual(StepPrep.objects.count(), 1)
        self.assertEqual(StepPrep.objects.values().get(id=prep.id), before)
        self.assertEqual(response.data["brief"], prep.brief)

    def test_all_live_statuses_are_unchanged_by_start(self):
        prep = self.waiting()
        for status in ["working", "ready", "stuck", "needs_choice"]:
            with self.subTest(status=status):
                StepPrep.objects.filter(id=prep.id).update(status=status)
                before = StepPrep.objects.values().get(id=prep.id)
                self.assertEqual(self.start().status_code, 200)
                self.assertEqual(StepPrep.objects.values().get(id=prep.id), before)

    def test_stop_then_restart_reuses_row_and_resets_work(self):
        prep = self.waiting()
        now = timezone.now()
        StepPrep.objects.filter(id=prep.id).update(answer="An answer", last_run_at=now, last_seen_note_at=now)
        response = self.jwt().post(self.url + "stop/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.row().status, "stopped")
        SharedGoalStep.objects.filter(id=self.step.id).update(title="New title", description="New description")
        response = self.start()
        self.assertEqual(response.status_code, 200, response.data)
        restarted = self.row()
        self.assertEqual((restarted.id, restarted.created_at), (prep.id, prep.created_at))
        self.assertEqual(restarted.status, "working")
        self.assertEqual(restarted.brief["title"], "New title")
        self.assertEqual(restarted.brief["description"], "New description")
        self.assertEqual(
            (restarted.question, restarted.choices, restarted.answer, restarted.doc_slug), ("", [], "", "")
        )
        self.assertIsNone(restarted.last_run_at)
        self.assertIsNone(restarted.last_seen_note_at)
        self.assertGreater(restarted.updated_at, prep.updated_at)

    def test_stop_missing_and_repeated_stop(self):
        self.assertEqual(self.jwt().post(self.url + "stop/", {}, format="json").status_code, 404)
        self.start()
        for _ in range(2):
            response = self.jwt().post(self.url + "stop/", {}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["status"], "stopped")

    def test_another_member_cannot_stop_choose_or_see_my_row(self):
        prep = self.waiting()
        for tenant in [self.owner, self.invitee, self.stranger]:
            self.assertEqual(self.jwt(tenant).get(self.list_url).json(), [])
            for suffix in ["stop/", "choose/"]:
                response = self.jwt(tenant).post(self.url + suffix, {"choice": "Local"}, format="json")
                self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(self.row().status, prep.status)
        self.assertEqual(self.jwt().get(self.list_url).json()[0]["id"], str(prep.id))

    def test_two_holders_have_independent_rows(self):
        SharedGoalStepAssignment.objects.create(
            membership=self.owner_member, step=self.step, status="accepted", asked_by=self.owner
        )
        first = self.waiting()
        second = self.start(self.owner)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertNotEqual(second.data["id"], str(first.id))
        self.assertEqual([r["id"] for r in self.jwt(self.owner).get(self.list_url).json()], [second.data["id"]])
        self.jwt(self.owner).post(self.url + "stop/", {}, format="json")
        self.assertEqual(self.row().status, "needs_choice")

    def test_choose_exact_offered_string_clears_question_and_resumes(self):
        prep = self.waiting()
        response = self.jwt().post(self.url + "choose/", {"choice": "Local"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        chosen = self.row()
        self.assertEqual((chosen.status, chosen.answer, chosen.question, chosen.choices), ("working", "Local", "", []))
        self.assertEqual((chosen.brief, chosen.doc_slug), (prep.brief, prep.doc_slug))
        self.assertNotIn("answer", response.data)
        self.assertGreater(chosen.updated_at, prep.updated_at)

    def test_choose_refuses_invalid_choice_and_wrong_status_without_mutating(self):
        self.assertEqual(self.jwt().post(self.url + "choose/", {"choice": "Local"}, format="json").status_code, 404)
        prep = self.waiting()
        for body in [
            {},
            {"choice": "local"},
            {"choice": " Local "},
            {"choice": "Other"},
            {"choice": []},
            {"choice": None},
        ]:
            with self.subTest(body=body):
                response = self.jwt().post(self.url + "choose/", body, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(self.row().updated_at, prep.updated_at)
        for status in ["working", "ready", "stuck", "stopped"]:
            with self.subTest(status=status):
                StepPrep.objects.filter(id=prep.id).update(status=status)
                response = self.jwt().post(self.url + "choose/", {"choice": "Local"}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(self.row().status, status)

    def test_choose_refuses_non_object_bodies_and_oversized_offered_answer(self):
        prep = self.waiting()
        for body in [[], "Local", 42, True, None]:
            response = self.jwt().generic(
                "POST", self.url + "choose/", json.dumps(body), content_type="application/json"
            )
            self.assertEqual(response.status_code, 400, response.data)
        StepPrep.objects.filter(id=prep.id).update(choices=["x" * 121])
        self.assertEqual(self.jwt().post(self.url + "choose/", {"choice": "x" * 121}, format="json").status_code, 400)

    def test_list_hides_stopped_and_is_newest_first_in_constant_queries(self):
        first = project_prep.start(self.holder, self.holder.user, self.goal.id, self.step.id)
        preps = [first]
        for index in range(3):
            step = projects.create_step(self.owner, self.owner.user, self.goal.id, {"title": f"Step {index}"})
            SharedGoalStepAssignment.objects.create(
                membership=self.member, step=step, status="accepted", asked_by=self.owner
            )
            preps.append(project_prep.start(self.holder, self.holder.user, self.goal.id, step.id))
        StepPrep.objects.filter(id=preps[1].id).update(status="stopped")
        SharedGoalStepAssignment.objects.filter(step_id=preps[2].step_id).update(status="released")
        # Lock/select, page cleanup and bulk stop stay constant with row count.
        with self.assertNumQueries(6):
            rows = project_prep.list_mine(self.holder)
        self.assertEqual([r["id"] for r in rows], [str(preps[3].id), str(first.id)])
        self.assertTrue(all(r["project_title"] == "Garden" for r in rows))
        self.assertEqual(StepPrep.objects.get(id=preps[2].id).status, "stopped")
        self.assertEqual(self.jwt().get(self.list_url).json(), rows)

    def test_list_stops_when_i_let_the_step_go_and_does_not_revive_it(self):
        self.start()
        projects.step_back(self.holder, self.holder.user, self.goal.id, {"all": True})
        self.assertEqual(self.jwt().get(self.list_url).json(), [])
        self.assertEqual(self.row().status, "stopped")
        SharedGoalStepAssignment.objects.filter(id=self.assignment.id).update(status="accepted")
        self.assertEqual(self.jwt().get(self.list_url).json(), [])
        self.assertEqual(self.start().status_code, 200)
        self.assertEqual(len(self.jwt().get(self.list_url).json()), 1)

    def test_list_stops_closed_steps_and_missing_assignments(self):
        for state in ["done", "skipped", "in_review", "missing_assignment"]:
            with self.subTest(state=state):
                SharedGoalStep.objects.filter(id=self.step.id).update(status="open")
                SharedGoalStepAssignment.objects.get_or_create(
                    membership=self.member, step=self.step, defaults={"status": "accepted", "asked_by": self.owner}
                )
                self.assertEqual(self.start().status_code, 200)
                if state == "missing_assignment":
                    SharedGoalStepAssignment.objects.filter(membership=self.member, step=self.step).delete()
                else:
                    SharedGoalStep.objects.filter(id=self.step.id).update(status=state)
                self.assertEqual(self.jwt().get(self.list_url).json(), [])
                self.assertEqual(self.row().status, "stopped")

    def test_list_stops_finished_project(self):
        self.start()
        projects.finish_project(self.owner, self.owner.user, self.goal.id, {"version": self.goal.version})
        self.assertEqual(self.jwt().get(self.list_url).json(), [])
        self.assertEqual(self.row().status, "stopped")

    def test_list_stops_inactive_membership(self):
        self.start()
        SharedGoalMembership.objects.filter(id=self.member.id).update(status="left")
        self.assertEqual(self.jwt().get(self.list_url).json(), [])
        self.assertEqual(self.row().status, "stopped")

    def test_plan_and_assistant_context_never_include_private_prep(self):
        before = {t.id: projects.get_plan(t, self.goal.id) for t in [self.holder, self.owner]}
        prep = self.waiting()
        for tenant in [self.holder, self.owner]:
            with self.subTest(tenant=tenant.id):
                self.assertEqual(projects.get_plan(tenant, self.goal.id), before[tenant.id])
                for data in [
                    self.jwt(tenant).get(self.base + "plan/").json(),
                    project_assistant.runtime_context(tenant),
                ]:
                    rendered = json.dumps(data, default=str)
                    for secret in [str(prep.id), prep.question, prep.doc_slug, "step_preps", '"brief"']:
                        self.assertNotIn(secret, rendered)

    def test_database_uniqueness_and_access_rejects_mismatched_ownership(self):
        prep = project_prep.start(self.holder, self.holder.user, self.goal.id, self.step.id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StepPrep.objects.create(tenant=self.holder, membership=self.member, step=self.step)
        self.assertFalse(access.my_step_preps(self.owner).exists())
        StepPrep.objects.filter(id=prep.id).update(tenant=self.owner)
        self.assertFalse(access.my_step_preps(self.owner).exists())
        self.assertFalse(access.my_step_preps(self.holder).exists())

    def test_migration_uses_pending_project_action_rls_lockdown(self):
        migration = import_module("apps.friends.migrations.0023_step_prep")
        proposal_lockdown = import_module("apps.tenants.migrations.0174_relock_after_projects_v2_assistant")
        self.assertEqual(migration.Migration.operations[-1].sql, proposal_lockdown.RELOCK_SQL)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN (%s, %s) ORDER BY relname",
                ["friends_step_preps", "friends_pending_project_actions"],
            )
            self.assertEqual(cursor.fetchall(), [(True, False), (True, False)])
            cursor.execute(
                "SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename IN (%s, %s)",
                ["friends_step_preps", "friends_pending_project_actions"],
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_step_prep_manager_and_instance_writes_stay_in_access(self):
        # Extend the existing architectural guard for this new private model,
        # within this task's allowed test file. Reads and writes have one broker.
        offenders = []
        for path in Path(__file__).parent.rglob("*.py"):
            if path == ACCESS_MODULE or _is_excluded(path.relative_to(Path(__file__).parent)):
                continue
            if "StepPrep" in _manager_accesses(path.read_text()):
                offenders.append(str(path))
        self.assertEqual(offenders, [])
        tree = ast.parse(Path(project_prep.__file__).read_text())
        self.assertFalse(
            any(isinstance(n, ast.Attribute) and n.attr in {"save", "delete", "update"} for n in ast.walk(tree))
        )
