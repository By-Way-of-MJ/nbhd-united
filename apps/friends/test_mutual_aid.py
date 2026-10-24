"""Consent and attribution contracts for the native mutual-aid experience."""

from unittest.mock import patch

from django.test import TestCase
from rest_framework.exceptions import NotFound

from . import access, circles, feed, services
from .models import FriendThread, SharedGoalMembership
from .test_pr6 import _client, _edge, _profile, _tenant


class MutualAidContractTests(TestCase):
    def setUp(self):
        self.a, self.b, self.c = [_tenant("aid_" + name) for name in ("a", "b", "c")]
        for tenant, handle in [(self.a, "aya"), (self.b, "ben"), (self.c, "cleo")]:
            _profile(tenant, handle)
        self.edge = _edge(self.a, self.b)
        self.mission = services.create_mission(
            self.a, self.a.user, self.edge.id, title="Help each other get started", description="One small step"
        )

    def test_invitation_discoverable_without_joining_or_revealing_activity(self):
        services.add_mission_update(self.a, self.a.user, self.mission.id, "note", "Member conversation")
        rows = services.list_missions(self.b, include_invited=True)
        self.assertEqual(rows[0]["my_status"], "invited")
        self.assertEqual(services.list_missions(self.b), [])
        self.assertEqual(list(access.missions_for(self.b)), [])
        preview = services.get_mission_detail(self.b, self.mission.id)
        self.assertEqual(preview["description"], "One small step")
        self.assertEqual(preview["members"], [])
        self.assertEqual(preview["updates"], [])
        with self.assertRaises(NotFound):
            services.add_mission_update(self.b, self.b.user, self.mission.id, "note", "Not yet")

    def test_accept_reveals_attributed_history_and_voluntary_commitment(self):
        services.add_mission_update(self.a, self.a.user, self.mission.id, "note", "Bring what you can")
        services.join_mission(self.b, self.b.user, self.mission.id, "Listen for ten minutes")
        detail = services.get_mission_detail(self.b, self.mission.id)
        self.assertEqual(detail["my_status"], "active")
        self.assertEqual(detail["my_commitment"], "Listen for ten minutes")
        note = next(u for u in detail["updates"] if u["text"] == "Bring what you can")
        self.assertEqual(note["author_name"], "Aya")

    def test_decline_is_idempotent_and_removes_invitation(self):
        client = _client(self.b.user)
        path = f"/api/v1/friends/missions/{self.mission.id}/decline/"
        self.assertEqual(client.post(path).status_code, 200)
        self.assertEqual(client.post(path).status_code, 200)
        self.assertEqual(services.list_missions(self.b, include_invited=True), [])
        with self.assertRaises(NotFound):
            services.join_mission(self.b, self.b.user, self.mission.id)
        with self.assertRaises(NotFound):
            services.get_mission_detail(self.b, self.mission.id)

    def test_stranger_cannot_read_or_decline_and_active_member_cannot_decline(self):
        for tenant in [self.c, self.a]:
            response = _client(tenant.user).post(f"/api/v1/friends/missions/{self.mission.id}/decline/")
            self.assertEqual(response.status_code, 404)
        self.assertEqual(services.list_missions(self.c), [])
        self.assertEqual(_client(self.c.user).get(f"/api/v1/friends/missions/{self.mission.id}/").status_code, 404)
        self.assertEqual(SharedGoalMembership.objects.get(shared_goal=self.mission, tenant=self.a).status, "active")

    def test_history_is_bounded_and_newest_first(self):
        for index in range(55):
            services.add_mission_update(self.a, self.a.user, self.mission.id, "note", str(index))
        history = services.get_mission_detail(self.a, self.mission.id)["updates"]
        self.assertEqual(len(history), 50)
        self.assertEqual(history[0]["text"], "54")

    @patch("apps.friends.services._notify_friend_message")
    def test_circle_feed_has_human_attribution_and_revokes_departed_reader(self, _notify):
        circle = circles.create_circle(self.a, self.a.user, name="Small steps")
        circles.join_circle(self.b, self.b.user, circle.invite_code)
        thread = FriendThread.objects.get(circle=circle)
        services.send_friend_message(self.b, self.b.user, thread.id, "aid-message-1", "I can listen")
        messages, _ = feed.build_thread_page(self.a, thread, cursor=None, limit=50)
        self.assertEqual(messages[0]["author"]["handle"], "ben")
        self.assertEqual(messages[0]["author"]["display_name"], "Ben")
        self.assertFalse(messages[0]["mine"])
        circles.leave_circle(self.b, circle.id)
        self.assertEqual(_client(self.b.user).get(f"/api/v1/friends/threads/{thread.id}/messages/").status_code, 404)

    def test_circle_assistant_choice_is_explicit_and_does_not_change_others(self):
        from .models import FriendThreadMembership

        response = _client(self.a.user).post(
            "/api/v1/friends/circles/", {"name": "Care", "agent_absorb_enabled": False}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        circle = circles.Circle.objects.get(id=response.data["circle_id"])
        thread = FriendThread.objects.get(circle=circle)
        self.assertFalse(FriendThreadMembership.objects.get(thread=thread, tenant=self.a).agent_absorb_enabled)
        response = _client(self.b.user).post(
            "/api/v1/friends/circles/join/",
            {"invite_code": circle.invite_code, "agent_absorb_enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(FriendThreadMembership.objects.get(thread=thread, tenant=self.b).agent_absorb_enabled)
        self.assertFalse(FriendThreadMembership.objects.get(thread=thread, tenant=self.a).agent_absorb_enabled)

    def test_network_contract_requires_enabled_authenticated_account(self):
        self.assertEqual(_client(self.a.user).get("/api/v1/friends/network/").data["version"], 1)
        self.c.neighborhood_enabled = False
        self.c.save(update_fields=["neighborhood_enabled"])
        self.assertEqual(_client(self.c.user).get("/api/v1/friends/network/").status_code, 403)

    def test_revoked_friendship_hides_and_prevents_accepting_invitation(self):
        self.edge.status = "blocked"
        self.edge.save(update_fields=["status"])
        self.assertEqual(services.list_missions(self.b, include_invited=True), [])
        response = _client(self.b.user).post(f"/api/v1/friends/missions/{self.mission.id}/join/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SharedGoalMembership.objects.get(shared_goal=self.mission, tenant=self.b).status, "invited")


class MissionLearningUpdatesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = _tenant("learning_a")
        _profile(cls.a, "aya")
        cls.mission = services.create_mission(cls.a, cls.a.user, member_friendship_ids=[], title="Build a bench")
        cls.member = access.mission_memberships().get(shared_goal=cls.mission, tenant=cls.a)
        cls.step = access.project_steps(cls.mission).create(
            shared_goal=cls.mission, title="Buy timber", created_by=cls.a
        )

    def history(self):
        return services.get_mission_detail(self.a, self.mission.id)["updates"]

    def step_note(self, text="", photos=0):
        note = access.create_note(self.mission, self.step, self.member, text, [(b"jpeg", 10, 10)] * photos)
        update = services._append_update(
            self.mission,
            self.a,
            self.a.user,
            "step_note",
            text=text[:120],
            payload={"note_id": str(note.id), "step_id": str(self.step.id)},
        )
        return note, update

    def test_only_nonblank_human_updates_are_included(self):
        for kind in [
            "joined",
            "step_added",
            "step_done",
            "step_unblocked",
            "member_left",
            "task_added",
            "task_completed",
            "task_linked",
            "add_task",
            "milestone_reached",
        ]:
            services._append_update(self.mission, self.a, self.a.user, kind, text="System activity")
        for kind in ["note", "progress", "milestone"]:
            services.add_mission_update(self.a, self.a.user, self.mission.id, kind, f"Written {kind}")
            for blank in ["", " \t\n\u2003"]:
                access.mission_updates().create(shared_goal=self.mission, tenant=self.a, kind=kind, text=blank)
        rows = self.history()
        self.assertEqual([r["text"] for r in rows], ["Written milestone", "Written progress", "Written note"])
        self.assertEqual(rows[-1]["author_name"], "Aya")
        self.assertEqual(set(rows[-1]), {"id", "kind", "text", "created_at", "author_name"})

    def test_step_note_with_words_keeps_the_existing_preview(self):
        text = "We found a good supplier. " * 10
        note, update = self.step_note(text, photos=1)
        self.assertEqual(update.text, text[:120].strip())
        self.assertEqual(self.history()[0]["text"], update.text)
        self.assertEqual(self.history()[0]["id"], str(update.id))

    def test_photo_only_step_notes_have_singular_and_plural_text(self):
        for count, expected in [(1, "added 1 photo to “Buy timber”"), (2, "added 2 photos to “Buy timber”")]:
            with self.subTest(count=count):
                self.step_note(" \t\n", photos=count)
                self.assertEqual(self.history()[0]["text"], expected)

    def test_missing_step_title_falls_back_to_photo_count(self):
        import uuid

        _, update = self.step_note(photos=2)
        update.payload["step_id"] = str(uuid.uuid4())
        update.save(update_fields=["payload"])
        self.assertEqual(self.history()[0]["text"], "added 2 photos")

    def test_deleted_missing_and_empty_step_notes_are_excluded(self):
        from . import project_notes

        deleted, update = self.step_note("Remove this", photos=2)
        project_notes.delete(self.a, self.mission.id, deleted.id)
        # Even a stale, nonblank audit preview cannot revive a deleted note.
        update.text = "Stale text"
        update.save(update_fields=["text"])
        missing, _ = self.step_note("Gone")
        access.project_notes(self.mission).filter(id=missing.id).delete()
        self.step_note(" \t\n")
        self.assertEqual(self.history(), [])

    def test_more_than_fifty_system_events_do_not_displace_written_notes(self):
        services.add_mission_update(self.a, self.a.user, self.mission.id, "note", "Keep this learning")
        for index in range(60):
            services._append_update(self.mission, self.a, self.a.user, "joined", text=f"System event {index}")
        self.assertEqual([r["text"] for r in self.history()], ["Keep this learning"])

    def test_limit_is_applied_after_blank_and_missing_notes_are_filtered(self):
        import uuid

        for index in range(55):
            services.add_mission_update(self.a, self.a.user, self.mission.id, "note", str(index))
        for _ in range(55):
            access.mission_updates().create(shared_goal=self.mission, tenant=self.a, kind="note", text=" \t\n")
            services._append_update(
                self.mission, self.a, self.a.user, "step_note", text="Missing", payload={"note_id": str(uuid.uuid4())}
            )
        rows = self.history()
        self.assertEqual([r["text"] for r in rows], [str(i) for i in range(54, 4, -1)])

    def test_foreign_project_notes_steps_and_authors_cannot_influence_the_feed(self):
        other = _tenant("learning_other")
        goal = services.create_mission(other, other.user, member_friendship_ids=[], title="Private project")
        member = access.mission_memberships().get(shared_goal=goal, tenant=other)
        step = access.project_steps(goal).create(shared_goal=goal, title="Private step", created_by=other)
        foreign_note = access.create_note(goal, step, member, "Private words", [(b"jpeg", 10, 10)] * 3)
        # The goal, step and author must ALL belong to this project, even for
        # inconsistent historical rows and payload references.
        wrong_step = access.create_note(self.mission, step, self.member, "Foreign step", [])
        wrong_author = access.create_note(self.mission, self.step, member, "Foreign author", [])
        for note in [foreign_note, wrong_step, wrong_author]:
            services._append_update(
                self.mission,
                self.a,
                self.a.user,
                "step_note",
                text="Untrusted preview",
                payload={"note_id": str(note.id), "step_id": str(step.id)},
            )
        _, own_update = self.step_note(photos=1)
        own_update.payload["step_id"] = str(step.id)
        own_update.save(update_fields=["payload"])
        services.add_mission_update(other, other.user, goal.id, "note", "Private update")
        self.assertEqual([r["text"] for r in self.history()], ["added 1 photo"])
        with self.assertRaises(NotFound):
            services.get_mission_detail(other, self.mission.id)

    def test_malformed_note_references_are_skipped(self):
        for payload in [{}, {"note_id": "bad"}, {"note_id": ["bad"]}, ["bad"], None]:
            access.mission_updates().create(
                shared_goal=self.mission, tenant=self.a, kind="step_note", text="Bad reference", payload=payload or {}
            )
        self.assertEqual(self.history(), [])

    def test_note_metadata_queries_stay_constant_as_the_page_grows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.step_note(photos=2)
        with CaptureQueriesContext(connection) as small:
            self.history()
        for _ in range(19):
            self.step_note(photos=2)
        with self.assertNumQueries(len(small)) as queries:
            rows = self.history()
        self.assertEqual(len(rows), 20)
        self.assertNotIn('."image"', " ".join(q["sql"] for q in queries.captured_queries))
