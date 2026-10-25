"""Finish lifecycle, reporter-only note moderation and append order contracts."""

from unittest.mock import patch
from uuid import uuid4

from django.contrib import admin
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.journal.models import Task

from . import access, project_notes, services
from . import project_assistant as assistant
from . import project_services as projects
from .models import ContentReport, PendingProjectAction, SharedGoalMembership
from .test_pr6 import _edge, _profile, _tenant

FINISHED = {"detail": "This project is finished — reopen it to change it."}


@override_settings(PROJECTS_V2_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class MissionLifecycleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.people = {
            name: _tenant("be2_" + name) for name in ["owner", "coowner", "member", "invitee", "removed", "stranger"]
        }
        for name, tenant in self.people.items():
            _profile(tenant, name)
            setattr(self, name, tenant)
        self.edges = [_edge(self.owner, self.people[name]) for name in ["coowner", "member", "invitee", "removed"]]
        self.goal = services.create_mission(
            self.owner, self.owner.user, member_friendship_ids=[str(e.id) for e in self.edges], title="Garden"
        )
        for name in ["coowner", "member", "removed"]:
            services.join_mission(self.people[name], self.people[name].user, self.goal.id)
        SharedGoalMembership.objects.filter(shared_goal=self.goal, tenant=self.coowner).update(role="owner")
        SharedGoalMembership.objects.filter(shared_goal=self.goal, tenant=self.removed).update(status="removed")
        self.members = {m.tenant_id: m for m in access.mission_memberships().filter(shared_goal=self.goal)}
        self.base = f"/api/v1/friends/missions/{self.goal.id}/"
        self.step = projects.create_step(self.owner, self.owner.user, self.goal.id, {"title": "Dig"})
        self.other_step = projects.create_step(self.member, self.member.user, self.goal.id, {"title": "Plant"})
        self.milestone = projects.milestone_write(self.owner, self.goal.id, {"title": "Spring"})
        self.edge = projects.dependency_write(
            self.owner, self.goal.id, {"blocker_id": str(self.step.id), "blocked_id": str(self.other_step.id)}
        )
        self.note = project_notes.create(
            self.member, self.member.user, self.goal.id, self.step.id, "A fact", [(b"jpeg", 2, 3)]
        )
        self.own_note = project_notes.create(self.owner, self.owner.user, self.goal.id, self.step.id, "My fact", [])
        self.proposal = assistant.propose(
            self.owner,
            self.goal.id,
            {"summary": "Suggested next steps", "changes": [{"kind": "add_step", "title": "Water"}]},
        )
        self.legacy_proposal, _ = services.propose_mission_task(self.owner, self.goal.id, title="Weed")

    def jwt(self, tenant):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(tenant.user).access_token}")
        return client

    def version(self):
        self.goal.refresh_from_db()
        return self.goal.version

    def lifecycle(self, *, actor=None, reopen=False):
        return self.jwt(actor or self.owner).post(
            self.base + ("reopen/" if reopen else "finish/"), {"version": self.version()}, format="json"
        )

    def finish(self):
        response = self.lifecycle()
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def report(self, actor, note=None):
        return self.jwt(actor).post(
            "/api/v1/friends/report/",
            {
                "target_kind": "project_note",
                "target_id": (note or self.note)["id"],
                "reason": "Concern",
                "detail": "Please review",
            },
            format="json",
        )

    def notes(self, actor):
        return self.jwt(actor).get(self.base + f"steps/{self.step.id}/notes/").json()["notes"]

    def test_finish_and_reopen_permission_matrix_and_flags(self):
        for name, actor in self.people.items():
            code = 200 if name in {"owner", "coowner"} else 403 if name == "member" else 404
            with self.subTest(actor=name):
                plan = self.jwt(actor).get(self.base + "plan/")
                if code != 404:
                    self.assertEqual(plan.status_code, 200)
                    self.assertEqual(plan.data["can_finish"], code == 200)
                    self.assertFalse(plan.data["can_reopen"])
                result = self.lifecycle(actor=actor)
                self.assertEqual(result.status_code, code, result.data)
                if code != 200:
                    self.finish()
                plan = self.jwt(actor).get(self.base + "plan/")
                if code != 404:
                    self.assertFalse(plan.data["can_finish"])
                    self.assertEqual(plan.data["can_reopen"], code == 200)
                result = self.lifecycle(actor=actor, reopen=True)
                self.assertEqual(result.status_code, code, result.data)
                if code != 200:
                    self.assertEqual(self.lifecycle(reopen=True).status_code, 200)

    def test_lifecycle_version_extra_keys_timestamp_memberships_and_system_events(self):
        members = list(access.mission_memberships().filter(shared_goal=self.goal).values("id", "role", "status"))
        client = self.jwt(self.owner)
        for suffix in ["finish/", "reopen/"]:
            if suffix == "reopen/":
                self.finish()
            for body in [{}, {"version": True}, {"version": "0"}, {"version": 0, "extra": 1}]:
                response = client.post(self.base + suffix, body, format="json")
                self.assertEqual(response.status_code, 400, response.data)
            response = client.post(self.base + suffix, {"version": 99}, format="json")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["version"], self.version())
        self.assertIsNotNone(self.goal.achieved_at)
        stamp = self.goal.achieved_at.isoformat()
        for actor in [self.owner, self.coowner, self.member]:
            plan = self.jwt(actor).get(self.base + "plan/").json()
            self.assertEqual((plan["status"], plan["finished_at"]), ("finished", stamp))
            self.assertFalse(plan["can_edit_project"])
            self.assertFalse(plan["can_invite"])
            self.assertFalse(any(s["can_edit"] or s["can_delete"] for s in plan["steps"]))
            self.assertFalse(any(m["can_edit"] for m in plan["milestones"]))
            self.assertFalse(any(m["can_remove"] or m["can_cancel_invite"] for m in plan["members"]))
            self.assertNotIn("created_by_id", str(plan))
            row = next(
                r
                for r in self.jwt(actor).get("/api/v1/friends/missions/").json()
                if r["mission_id"] == str(self.goal.id)
            )
            self.assertEqual((row["status"], row["finished_at"]), ("finished", stamp.replace("+00:00", "Z")))
            self.assertEqual(self.jwt(actor).get(self.base).status_code, 200)
        result = self.lifecycle(actor=self.coowner, reopen=True)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertIsNone(result.data["finished_at"])
        self.assertEqual(result.data["status"], "active")
        self.assertEqual(self.version(), 2)
        self.assertEqual(
            members, list(access.mission_memberships().filter(shared_goal=self.goal).values("id", "role", "status"))
        )
        for kind, actor in [("project_finished", self.owner), ("project_reopened", self.coowner)]:
            update = access.mission_updates().get(shared_goal=self.goal, kind=kind)
            self.assertEqual((update.tenant_id, update.user_id), (actor.id, actor.user.id))
        self.assertFalse(
            {"project_finished", "project_reopened"}
            & {r["kind"] for r in services.get_mission_detail(self.owner, self.goal.id)["updates"]}
        )
        self.assertEqual(
            client.post(self.base + "steps/", {"title": "After reopening"}, format="json").status_code, 201
        )

    def test_finished_exceptions_note_authorship_report_leave_and_mute(self):
        self.finish()
        owner = self.jwt(self.owner)
        member = self.jwt(self.member)
        note_url = self.base + f"notes/{self.note['id']}/"
        self.assertEqual(member.patch(note_url, {"text": "Edited after finish"}, format="json").status_code, 200)
        self.assertEqual(owner.delete(note_url).status_code, 409)
        self.assertEqual(owner.patch(note_url, {"text": "Not mine"}, format="json").status_code, 409)
        self.assertEqual(self.report(self.owner).status_code, 200)
        self.assertEqual(member.delete(note_url).status_code, 204)
        self.assertEqual(owner.patch(self.base + "membership/", {"muted": True}, format="json").status_code, 200)
        self.assertEqual(member.post(self.base + "leave/", {}, format="json").status_code, 200)
        self.assertEqual(self.lifecycle(reopen=True).status_code, 200)
        self.assertEqual(owner.delete(self.base + f"notes/{self.own_note['id']}/").status_code, 204)

    def test_runtime_finished_context_and_proposal_refusal_without_claiming(self):
        self.finish()
        context = assistant.runtime_context(self.owner)
        [project] = context["projects"]
        self.assertEqual(project["status"], "finished")
        self.assertIsNotNone(project["finished_at"])
        self.assertIn("Do not propose changes", context["finished_rule"])
        with self.assertRaises(APIException) as raised:
            assistant.propose(
                self.owner,
                self.goal.id,
                {"summary": "Suggested next steps", "changes": [{"kind": "add_step", "title": "No"}]},
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, FINISHED["detail"])
        self.assertEqual(PendingProjectAction.objects.get(id=self.proposal["proposal_id"]).status, "pending")

    def test_finished_decline_and_legacy_proposal_writes_are_blocked(self):
        self.finish()
        response = self.jwt(self.invitee).post(self.base + "decline/", {}, format="json")
        self.assertEqual((response.status_code, response.json()), (409, FINISHED))
        self.legacy_proposal.refresh_from_db()
        for action in ["approve", "reject"]:
            response = self.jwt(self.owner).post(
                f"/api/v1/friends/mission-actions/{self.legacy_proposal.id}/{action}/", {}, format="json"
            )
            self.assertEqual((response.status_code, response.json()), (409, FINISHED))
            self.legacy_proposal.refresh_from_db()
            self.assertEqual(self.legacy_proposal.status, "pending")
        with self.assertRaises(APIException) as refused:
            services.propose_mission_task(self.owner, self.goal.id, title="No more")
        self.assertEqual(refused.exception.status_code, 409)

    def test_finish_between_note_preparation_and_commit_refuses_the_insert(self):
        prepare = project_notes.prepare

        def finish_while_preparing(*args):
            result = prepare(*args)
            self.finish()
            return result

        before = access.project_notes(self.goal).count()
        with patch.object(project_notes, "prepare", side_effect=finish_while_preparing):
            response = self.jwt(self.owner).post(
                self.base + f"steps/{self.step.id}/notes/", {"text": "Too late"}, format="json"
            )
        self.assertEqual((response.status_code, response.json()), (409, FINISHED))
        self.assertEqual(access.project_notes(self.goal).count(), before)

    def test_finish_between_step_acceptance_preparation_and_commit_refuses_private_task(self):
        projects.ask(self.owner, self.owner.user, self.goal.id, self.step.id, [str(self.members[self.member.id].id)])
        prepare = services._prepare_member_task

        def finish_while_preparing(*args):
            result = prepare(*args)
            self.finish()
            return result

        with patch.object(services, "_prepare_member_task", side_effect=finish_while_preparing):
            response = self.jwt(self.member).post(
                self.base + f"steps/{self.step.id}/respond/", {"answer": "yes"}, format="json"
            )
        self.assertEqual((response.status_code, response.json()), (409, FINISHED))
        self.assertEqual(access.project_assignments(self.goal).get(step=self.step).status, "asked")
        self.assertFalse(Task.objects.filter(tenant=self.member).exists())

    def test_report_permission_matrix(self):
        for name, actor in self.people.items():
            with self.subTest(actor=name):
                expected = 403 if name == "member" else 200 if name in {"owner", "coowner"} else 404
                response = self.report(actor)
                self.assertEqual(response.status_code, expected, response.data)
        for target in [str(uuid4()), "invalid"]:
            response = self.jwt(self.owner).post(
                "/api/v1/friends/report/", {"target_kind": "project_note", "target_id": target}, format="json"
            )
            self.assertEqual(response.status_code, 404 if target != "invalid" else 400)

    def test_report_member_can_hide_note_and_photos_for_self_only_idempotently_and_queue(self):
        # The reporter is a regular member; another member authored this note.
        self.assertTrue(next(r for r in self.notes(self.member) if r["id"] == self.own_note["id"])["can_report"])
        self.assertFalse(next(r for r in self.notes(self.owner) if r["id"] == self.own_note["id"])["can_report"])
        first = self.report(self.member, self.own_note)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.json(), self.report(self.member, self.own_note).json())
        self.assertTrue(first.json()["hidden"])
        self.assertNotIn(self.own_note["id"], [r["id"] for r in self.notes(self.member)])
        for actor in [self.owner, self.coowner]:
            self.assertIn(self.own_note["id"], [r["id"] for r in self.notes(actor)])
        report = ContentReport.objects.get(id=first.json()["report_id"])
        self.assertEqual(
            (report.target_kind, str(report.project_note_id), report.status),
            ("project_note", self.own_note["id"], "hidden"),
        )
        self.assertEqual(report.reason, "Concern — Please review")
        queue = admin.site._registry[ContentReport]
        self.assertTrue(
            queue.get_queryset(RequestFactory().get("/admin/friends/contentreport/")).filter(id=report.id).exists()
        )
        self.assertIn("project_note", queue.readonly_fields)
        from apps.steward.facts import _content_reports

        queued = next(row for row in _content_reports(timezone.now()) if row["id"] == f"content-report:{report.id}")
        self.assertEqual(queued["target_kind"], "project_note")
        self.assertIn(str(report.id), queued["link"])
        # Report a photo-bearing note and verify the direct bytes route too.
        response = self.report(self.coowner)
        self.assertEqual(response.status_code, 200, response.data)
        url = self.note["photos"][0]["url"]
        self.assertEqual(self.jwt(self.coowner).get(url).status_code, 404)
        self.assertEqual(self.jwt(self.owner).get(url).status_code, 200)
        self.assertEqual(self.jwt(self.member).get(url).status_code, 200)
        self.assertNotIn(self.note["id"], [r["id"] for r in self.notes(self.coowner)])
        self.assertNotIn("A fact", str(assistant.runtime_context(self.coowner)))
        self.assertNotIn("A fact", str(services.get_mission_detail(self.coowner, self.goal.id)["updates"]))
        self.assertEqual(self.jwt(self.owner).delete(self.base + f"notes/{self.note['id']}/").status_code, 204)
        for actor in [self.owner, self.coowner, self.member]:
            self.assertNotIn(self.note["id"], [r["id"] for r in self.notes(actor)])
            self.assertEqual(self.jwt(actor).get(url).status_code, 404)
        self.assertTrue(ContentReport.objects.filter(id=response.json()["report_id"]).exists())

    def test_append_manual_owned_step_milestones_and_proposals_by_group(self):
        projects.patch_step(self.owner, self.goal.id, self.step.id, {"version": 0, "order": 12})
        added = projects.create_step(self.member, self.member.user, self.goal.id, {"title": "Append"})
        self.assertEqual(added.order, 13)
        owned, _task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title="Owned")
        self.assertEqual(owned.order, 14)
        explicit = projects.create_step(self.owner, self.owner.user, self.goal.id, {"title": "Template", "order": -4})
        self.assertEqual(explicit.order, -4)
        group = {"milestone_id": str(self.milestone.id)}
        for fields, expected in [({"order": 30}, 30), ({}, 31)]:
            step = projects.create_step(
                self.owner, self.owner.user, self.goal.id, {"title": "Grouped", **group, **fields}
            )
            self.assertEqual(step.order, expected)
        projects.milestone_write(self.owner, self.goal.id, {"title": "Template", "order": 17})
        milestone = projects.milestone_write(self.member, self.goal.id, {"title": "Append"})
        self.assertEqual(milestone.order, 18)
        proposal = assistant.propose(
            self.owner,
            self.goal.id,
            {
                "summary": "Suggested next steps",
                "changes": [
                    {"kind": "add_step", "title": "Proposal one"},
                    {"kind": "add_step", "title": "Proposal grouped", **group},
                    {"kind": "add_step", "title": "Proposal two"},
                ],
            },
        )
        result = assistant.approve(self.owner, self.owner.user, proposal["proposal_id"])
        self.assertEqual(result["status"], "approved", result)
        rows = dict(access.project_steps(self.goal).filter(title__startswith="Proposal").values_list("title", "order"))
        self.assertEqual(rows, {"Proposal one": 15, "Proposal grouped": 32, "Proposal two": 16})

    def test_batch_explicit_order_keeps_append_at_current_max(self):
        projects.patch_step(self.owner, self.goal.id, self.step.id, {"version": 0, "order": 20})
        items = [({"title": "Explicit", "order": 2}, False, []), ({"title": "Implicit"}, False, [])]
        rows = projects.create_steps(self.owner, self.owner.user, self.goal.id, items, [None, None])
        self.assertEqual([r.order for r in rows], [2, 21])

    def test_remove_activity_is_attributed_to_departing_person_with_owner_in_payload(self):
        projects.ask(self.owner, self.owner.user, self.goal.id, self.step.id, [str(self.members[self.member.id].id)])
        projects.respond(self.member, self.member.user, self.goal.id, self.step.id, {"answer": "yes"})
        projects.change_member(
            self.coowner, self.coowner.user, self.goal.id, self.members[self.member.id].id, {"version": 0}
        )
        for kind in ["step_released", "member_left"]:
            row = access.mission_updates().get(shared_goal=self.goal, kind=kind)
            self.assertEqual((row.tenant_id, row.user_id), (self.member.id, self.member.user.id))
            self.assertEqual(row.payload["membership_id"], str(self.members[self.member.id].id))
            self.assertEqual(row.payload["removed_by_membership_id"], str(self.members[self.coowner.id].id))

    def test_task_completion_does_not_mutate_finished_project(self):
        step, task = projects.add_owned_step(self.owner, self.owner.user, self.goal.id, title="Private")
        self.finish()
        before = access.mission_updates().filter(shared_goal=self.goal).count()
        task.complete()
        step.refresh_from_db()
        self.assertEqual(step.status, "open")
        self.assertEqual(Task.objects.get(id=task.id).status, "done")
        self.assertEqual(access.mission_updates().filter(shared_goal=self.goal).count(), before)


# One independently named regression for each public write entry point. Valid
# payloads ensure a finished project refuses the action before item permissions.
WRITE_CASES = {
    "step_create": ("post", lambda s: s.base + "steps/", lambda s: {"title": "No"}),
    "step_patch": ("patch", lambda s: s.base + f"steps/{s.step.id}/", lambda s: {"version": 0, "title": "No"}),
    "step_delete": ("delete", lambda s: s.base + f"steps/{s.step.id}/", lambda s: {}),
    "milestone_create": ("post", lambda s: s.base + "milestones/", lambda s: {"title": "No"}),
    "milestone_patch": ("patch", lambda s: s.base + f"milestones/{s.milestone.id}/", lambda s: {"title": "No"}),
    "milestone_delete": ("delete", lambda s: s.base + f"milestones/{s.milestone.id}/", lambda s: {}),
    "dependency_create": (
        "post",
        lambda s: s.base + "dependencies/",
        lambda s: {"blocker_id": str(s.step.id), "blocked_id": str(s.other_step.id)},
    ),
    "dependency_delete": ("delete", lambda s: s.base + f"dependencies/{s.edge.id}/", lambda s: {}),
    "member_add": ("post", lambda s: s.base + "members/", lambda s: {"member_friendship_ids": [str(s.edges[0].id)]}),
    "member_remove": (
        "post",
        lambda s: s.base + f"members/{s.members[s.member.id].id}/remove/",
        lambda s: {"version": 1},
    ),
    "invite_cancel": (
        "post",
        lambda s: s.base + f"members/{s.members[s.invitee.id].id}/cancel-invite/",
        lambda s: {"version": 1},
    ),
    "owner_role": (
        "post",
        lambda s: s.base + "owners/",
        lambda s: {"membership_id": str(s.members[s.member.id].id), "role": "owner"},
    ),
    "step_back": ("post", lambda s: s.base + "step-back/", lambda s: {}),
    "note_create": ("post", lambda s: s.base + f"steps/{s.step.id}/notes/", lambda s: {"text": "No"}),
    "rename": ("patch", lambda s: s.base, lambda s: {"version": 1, "title": "No"}),
    "project_delete": ("post", lambda s: s.base + "delete/", lambda s: {}),
    "project_finish": ("post", lambda s: s.base + "finish/", lambda s: {"version": 1}),
    "goal_link": ("patch", lambda s: s.base + "membership/", lambda s: {"linked_goal_id": None, "muted": True}),
    "legacy_update": ("post", lambda s: s.base + "updates/", lambda s: {"kind": "note", "text": "No"}),
    "legacy_owned_step": ("post", lambda s: s.base + "tasks/", lambda s: {"title": "No"}),
    "join": ("post", lambda s: s.base + "join/", lambda s: {}),
    "proposal_approve": (
        "post",
        lambda s: f"/api/v1/friends/project-proposals/{s.proposal['proposal_id']}/approve/",
        lambda s: {},
    ),
    "proposal_reject": (
        "post",
        lambda s: f"/api/v1/friends/project-proposals/{s.proposal['proposal_id']}/reject/",
        lambda s: {},
    ),
}
for action, body in [
    ("ask", {"membership_ids": []}),
    ("respond", {"answer": "yes"}),
    ("complete", {}),
    ("reopen", {}),
    ("second-look", {"on": True}),
    ("confirm", {}),
    ("question", {"note": "Why?"}),
    ("keep", {}),
]:
    WRITE_CASES["step_" + action.replace("-", "_")] = (
        "post",
        lambda s, action=action: s.base + f"steps/{s.step.id}/{action}/",
        lambda s, body=body: {
            **body,
            **({"membership_ids": [str(s.members[s.member.id].id)]} if "membership_ids" in body else {}),
        },
    )


def _write_case(method, url, data):
    def test(self):
        self.finish()
        before = access.mission_updates().filter(shared_goal=self.goal).count()
        with patch("apps.friends.project_notes.prepare") as prepare:
            response = getattr(self.jwt(self.owner), method)(url(self), data(self), format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.json(), FINISHED)
        prepare.assert_not_called()
        self.assertEqual(self.version(), 1)
        self.assertEqual(access.mission_updates().filter(shared_goal=self.goal).count(), before)
        self.assertEqual(PendingProjectAction.objects.get(id=self.proposal["proposal_id"]).status, "pending")
        self.assertFalse(self.members[self.owner.id].muted)

    return test


for name, case in WRITE_CASES.items():
    setattr(MissionLifecycleTests, "test_finished_blocks_" + name, _write_case(*case))
