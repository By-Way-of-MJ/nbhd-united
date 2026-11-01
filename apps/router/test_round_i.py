"""Regression checks for the reviewed lane and publication races."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.router import test_keep_chatting as keep
from apps.router.job_changes import save_side_jobs
from apps.router.models import AppChatMessage, PendingMessage
from apps.router.side_lane import claim_chat_batch, store_handoff_reply


@override_settings(KEEP_CHATTING_TENANT_IDS="*", NBHD_INTERNAL_API_KEY="test-key")
class RoundITests(TestCase):
    setUp = keep.KeepChattingTests.setUp
    row = keep.KeepChattingTests.row
    main = keep.KeepChattingTests.main
    drain = keep.KeepChattingTests.drain
    app = keep.KeepChattingTests.app

    def test_unclaimed_side_returns_to_main_after_job_lands(self):
        main = self.main()
        status = self.row("status?")
        batch, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
        from apps.router.side_lane import frame_chat_content

        frame_chat_content(self.tenant, batch, "status?", f"thread:{self.thread.id}")
        queued = self.row("actually make it two weeks")
        claim_chat_batch(self.tenant, str(self.thread.id), 120)
        queued.refresh_from_db()
        self.assertEqual(queued.lane, "side")
        self.assertIsNone(queued.lease_token)
        keep.KeepChattingTests.finish(self, main)
        claim_chat_batch(self.tenant, str(self.thread.id), 120)
        queued.refresh_from_db()
        self.assertEqual(queued.lane, "main")
        status.refresh_from_db()
        self.assertEqual(status.lease_token, batch[0].lease_token)
        from apps.router.pending_queue import _store_ios_turn_reply

        _store_ios_turn_reply(self.tenant, batch, "Still working.")
        PendingMessage.objects.filter(pk=status.pk).delete()
        self.drain()
        content = self.post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("actually make it two weeks", content)
        self.assertNotIn("NBHD side conversation", content)

    def test_classification_publication_wait_costs_no_attempt_and_reuses_proxy_id(self):
        row = self.row("plan the move")
        self.thread.side_jobs = {
            str(row.id): {
                "main_ids": [row.payload["client_msg_id"]],
                "side_ids": [],
                "kind": "none",
                "finished": False,
                "helpers": [],
                "checking_until": timezone.now().timestamp() + 20,
            }
        }
        self.thread.save(update_fields=["side_jobs"])
        self.drain()
        row.refresh_from_db()
        self.assertEqual(row.delivery_attempts, 0)
        self.assertIsNone(row.delivery_in_flight_until)
        first_id = self.post.call_args.kwargs["headers"]["X-NBHD-Chat-Request-Id"]
        self.assertEqual(self.app(row).status, "pending")
        self.thread.refresh_from_db()
        self.thread.side_jobs[str(row.id)].pop("checking_until")
        save_side_jobs(self.thread)
        self.drain()
        self.assertEqual(first_id, self.post.call_args.kwargs["headers"]["X-NBHD-Chat-Request-Id"])
        self.assertEqual(self.app(row).status, "ready")
        self.assertEqual(AppChatMessage.objects.count(), 1)

    def test_dead_main_lease_uses_exact_snapshot_and_new_ownership(self):
        row = self.main()
        old_token = row.lease_token
        self.health.return_value = False
        batch, info = claim_chat_batch(self.tenant, str(self.thread.id), 120)
        self.assertEqual([r.id for r in batch], [row.id])
        self.assertEqual(info["broken_lease_count"], 1)
        self.assertNotEqual(batch[0].lease_token, old_token)

    def test_replacement_main_lease_survives_health_probe(self):
        row = self.main()
        replacement = timezone.now() + timedelta(seconds=400)

        def probe(tenant):
            PendingMessage.objects.filter(pk=row.pk).update(delivery_in_flight_until=replacement)
            return False

        self.health.side_effect = probe
        batch, info = claim_chat_batch(self.tenant, str(self.thread.id), 120)
        self.assertEqual(batch, [])
        self.assertEqual(info.get("broken_lease_count", 0), 0)
        row.refresh_from_db()
        self.assertEqual(row.delivery_in_flight_until, replacement)

    def test_no_action_marker_is_stripped_on_ordinary_main_and_is_not_failure(self):
        row = self.row()
        self.post.return_value = keep._ok_chat_response("[[side-recap-no-action]]")
        with patch("apps.router.pending_queue._dispatch_push") as push:
            self.drain()
        self.assertEqual(self.app(row).status, "ready")
        self.assertEqual(self.app(row).reply_text, "")
        push.assert_not_called()
        row = self.row()
        self.post.return_value = keep._ok_chat_response("Noted. [[side-recap-no-action]]")
        self.drain()
        self.assertEqual(self.app(row).reply_text, "Noted.")

    def test_history_keeps_all_open_and_newest_twenty_closed(self):
        jobs = {}
        for i in range(25):
            jobs[str(uuid.uuid4())] = {
                "finished": True,
                "helpers": [],
                "consumed": True,
                "created": i,
                "closed": i,
                "kind": "none",
            }
        newest = list(jobs)[-20:]
        open_id = str(uuid.uuid4())
        jobs[open_id] = {"finished": False, "helpers": [], "created": -1}
        context_id = str(uuid.uuid4())
        jobs[context_id] = {"finished": True, "helpers": [], "kind": "none", "created": -2}
        self.thread.side_jobs = jobs
        save_side_jobs(self.thread)
        self.thread.refresh_from_db()
        self.assertEqual(set(self.thread.side_jobs), set(newest + [open_id, context_id]))
        checking_id = str(uuid.uuid4())
        self.thread.side_jobs[checking_id] = {
            "finished": True,
            "helpers": [],
            "consumed": True,
            "created": -3,
            "closed": -3,
            "checking_until": timezone.now().timestamp() + 20,
        }
        save_side_jobs(self.thread)
        self.assertIn(checking_id, self.thread.side_jobs)

    def test_replayed_handoff_does_not_push_twice(self):
        row = self.row()
        row.payload = {"side_handoff": True}
        row.lease_token = uuid.uuid4()
        row.save()
        with self.captureOnCommitCallbacks(execute=True), patch("apps.router.pending_queue._dispatch_push") as push:
            store_handoff_reply(self.tenant, [row], "Done.")
            store_handoff_reply(self.tenant, [row], "Done.")
        self.assertEqual(push.call_count, 1)
        self.assertEqual(AppChatMessage.objects.filter(client_msg_id__startswith="side-resume-").count(), 1)
