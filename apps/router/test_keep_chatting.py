"""Two app lanes with real queue/app rows; all transports are offline mocks."""

import json
import uuid
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.pii.redactor import RedactionOutcome
from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import drain_pending_messages_for_tenant_task
from apps.router.side_lane import (
    SIDE_LANE_MIN_MAIN_AGE_SECONDS,
    LeaseLost,
    claim_chat_batch,
    lock_owned_batch,
    progress_rows,
)
from apps.router.test_pending_queue import _make_tenant, _make_user, _ok_chat_response


@override_settings(KEEP_CHATTING_TENANT_IDS="*", NBHD_INTERNAL_API_KEY="test-key")
class KeepChattingTests(TestCase):
    def setUp(self):
        from apps.router.job_changes import JobChange

        self.change_check = self.enterContext(
            patch(
                "apps.router.job_changes.classify_job_change",
                side_effect=lambda tenant, request, messages: JobChange(
                    changes_job=any(word in " ".join(messages) for word in ("cancel", "change", "redo", "actually")),
                    kind="change"
                    if any(word in " ".join(messages) for word in ("cancel", "change", "redo", "actually"))
                    else "none",
                ),
            )
        )
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        self.enterContext(patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="offline"))
        self.publish = self.enterContext(patch("apps.cron.publish.publish_task"))
        self.health = self.enterContext(patch("apps.router.pending_queue._is_tenant_container_live", return_value=True))
        self.enterContext(patch("apps.router.proactive_context.surface_proactive_context", return_value=""))
        self.enterContext(patch("apps.router.thread_recap.build_thread_recap_block", return_value=""))
        self.enterContext(patch("apps.router.pending_queue._schedule_ios_digest_refresh"))
        self.usage = self.enterContext(patch("apps.router.pending_queue._record_usage_safe"))
        self.enterContext(patch("apps.router.pending_queue._dispatch_push"))
        self.redact = self.enterContext(
            patch(
                "apps.pii.redactor.redact_user_message_checked",
                side_effect=lambda text, *a, **kw: RedactionOutcome(text, True, "ok"),
            )
        )
        self.post = self.enterContext(
            patch("apps.router.pending_queue.httpx.post", return_value=_ok_chat_response("side answer"))
        )

    def row(self, text="hello", **flags):
        cid = uuid.uuid4().hex
        AppChatMessage.objects.create(
            tenant=self.tenant, user=self.user, thread=self.thread, client_msg_id=cid, user_text=text
        )
        return PendingMessage.objects.create(
            tenant=self.tenant,
            channel="ios",
            channel_user_id=str(self.thread.id),
            user_text=text,
            payload={
                "message_text": text,
                "user_param": f"thread:{self.thread.id}",
                "client_msg_id": cid,
                "user_timezone": "UTC",
                **flags,
            },
        )

    def main(self, age=12):
        row = self.row("compare those three vacuums")
        started = timezone.now() - timedelta(seconds=age)
        PendingMessage.objects.filter(pk=row.pk).update(
            created_at=started,
            turn_started_at=started,
            delivery_in_flight_until=timezone.now() + timedelta(seconds=200),
            lease_token=uuid.uuid4(),
        )
        row.refresh_from_db()
        return row

    def finish(self, row):
        from apps.router.pending_queue import _store_ios_turn_reply

        _store_ios_turn_reply(self.tenant, [row], "original job result")
        row.delete()

    def drain(self):
        return drain_pending_messages_for_tenant_task(str(self.tenant.id), "ios", str(self.thread.id))

    def app(self, row):
        return AppChatMessage.objects.get(tenant=self.tenant, client_msg_id=row.payload["client_msg_id"])

    def test_second_after_threshold_posts_separate_session_and_keeps_main_lease(self):
        main = self.main()
        second = self.row("quick question")
        result = self.drain()
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(self.app(second).reply_text, "side answer")
        self.assertEqual(self.app(main).status, "pending")
        main.refresh_from_db()
        self.assertGreater(main.delivery_in_flight_until, timezone.now())
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["user"], f"thread:{self.thread.id}:side:{main.id.hex[:12]}")
        self.assertIn("compare those three vacuums", payload["messages"][0]["content"])
        self.assertNotIn("NBHD side", json.dumps(second.payload))
        self.assertEqual(self.app(second).user_text, "quick question")
        self.usage.assert_called_once()

    def test_real_main_and_side_replies_keep_their_poll_keys(self):
        main = self.row("long job")
        side = None

        def post(*args, **kwargs):
            nonlocal side
            if ":side:" in kwargs["json"]["user"]:
                return _ok_chat_response("quick answer")
            running = PendingMessage.objects.get(pk=main.pk)
            PendingMessage.objects.filter(pk=main.pk).update(turn_started_at=timezone.now() - timedelta(seconds=12))
            side = self.row("quick question")
            self.drain()
            self.assertEqual(PendingMessage.objects.get(pk=main.pk).lease_token, running.lease_token)
            return _ok_chat_response("long answer")

        self.post.side_effect = post
        self.drain()
        self.assertEqual(self.app(main).reply_text, "long answer")
        self.assertEqual(self.app(side).reply_text, "quick answer")
        self.assertEqual(self.post.call_count, 2)

    def test_arrival_at_two_seconds_schedules_threshold_and_runs_without_new_arrival(self):
        main = self.main(age=2)
        second = self.row()
        self.assertEqual(self.drain()["delivered"], 0)
        self.post.assert_not_called()
        self.assertEqual(self.publish.call_args.kwargs["delay_seconds"], SIDE_LANE_MIN_MAIN_AGE_SECONDS - 2)
        PendingMessage.objects.filter(pk=main.pk).update(turn_started_at=timezone.now() - timedelta(seconds=10))
        self.drain()
        self.assertEqual(self.app(second).status, "ready")

    def test_attachments_hibernation_waking_and_failed_health_wait(self):
        main = self.main()
        for flag in ("is_image", "is_document", "is_voice", "attachment_paths"):
            with self.subTest(attachment=flag):
                row = self.row(**{flag: True})
                self.assertEqual(self.drain()["delivered"], 0)
                row.delete()
        second = self.row()
        for state in ("hibernated", "waking", "down"):
            with self.subTest(state=state):
                self.tenant.hibernated_at = timezone.now() if state == "hibernated" else None
                self.tenant.save(update_fields=["hibernated_at"])
                AppChatMessage.objects.filter(pk=self.app(second).pk).update(
                    waking_at=timezone.now() if state == "waking" else None
                )
                self.health.return_value = state != "down"
                self.assertEqual(self.drain()["delivered"], 0)
        self.post.assert_not_called()
        main.refresh_from_db()
        self.assertIsNotNone(main.delivery_in_flight_until)

    @override_settings(ONE_QUESTION_TENANT_IDS="*")
    def test_ephemeral_reply_rule_preserves_main_and_side_label_taps(self):
        from apps.router.reply_shape import ONE_QUESTION_NOTE

        self.assertLess(len(ONE_QUESTION_NOTE.split()), 30)
        # Both lanes enter the same last transport seam after PII annotation.
        for side in (False, True):
            with self.subTest(side=side):
                PendingMessage.objects.all().delete()
                AppChatMessage.objects.all().delete()
                if side:
                    self.main()
                row = self.row("Post it")
                with patch("apps.pii.redactor.annotate_model_context", side_effect=lambda text, *_: text):
                    self.drain()
                content = self.post.call_args.kwargs["json"]["messages"][0]["content"]
                self.assertTrue(content.startswith(ONE_QUESTION_NOTE + "\n"))
                self.assertEqual(content.splitlines()[-1], "Post it")
                self.assertNotIn(ONE_QUESTION_NOTE, self.app(row).user_text)
                self.assertNotIn(ONE_QUESTION_NOTE, json.dumps(row.payload))
                self.assertEqual(row.user_text, "Post it")

    @override_settings(ONE_QUESTION_TENANT_IDS="*")
    def test_handoff_reply_shape_is_logged_once_and_silence_is_not_a_reply(self):
        from apps.router.side_lane import store_handoff_reply

        row = self.row()
        row.payload = {"side_handoff": True, "side_client_msg_ids": []}
        row.save(update_fields=["payload"])
        with self.assertNoLogs("apps.router.reply_shape"):
            store_handoff_reply(self.tenant, [row], "[[side-recap-no-action]]")
        with self.assertLogs("apps.router.reply_shape") as logs:
            store_handoff_reply(self.tenant, [row], "Top three?\n[[quick-replies: A | B]]")
            store_handoff_reply(self.tenant, [row], "Top three?\n[[quick-replies: A | B]]")
        self.assertEqual(len(logs.records), 1)
        self.assertTrue(logs.records[0].getMessage().endswith("questions=1 buttons=1"))

    def test_three_messages_during_side_turn_coalesce_in_order(self):
        self.main()
        first = self.row("first side")
        later = []

        def post(*args, **kwargs):
            if not later:
                later.extend(self.row(text) for text in ("one", "two", "three"))
                self.assertEqual(self.drain()["delivered"], 0)
            return _ok_chat_response("one answer")

        self.post.side_effect = post
        self.drain()
        self.assertEqual(self.app(first).status, "ready")
        self.drain()
        content = self.post.call_args.kwargs["json"]["messages"][0]["content"]
        current = content.split("Current message:\n", 1)[1]
        self.assertLess(current.index("one"), current.index("two"))
        self.assertLess(current.index("two"), current.index("three"))
        self.assertEqual([self.app(r).status for r in later], ["ready"] * 3)
        self.assertEqual([self.app(r).reply_text for r in later], ["", "", "one answer"])
        self.assertEqual(self.post.call_count, 2)

    def test_retry_lease_expiry_and_stale_worker_cannot_duplicate_or_clear_replacement(self):
        self.main()
        second = self.row()
        old, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
        self.assertEqual(self.drain()["delivered"], 0)
        PendingMessage.objects.filter(pk=second.pk).update(
            delivery_in_flight_until=timezone.now() - timedelta(seconds=1)
        )
        new, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
        self.assertNotEqual(old[0].lease_token, new[0].lease_token)
        with self.assertRaises(LeaseLost):
            lock_owned_batch(self.tenant, old)
        from apps.router.pending_queue import _store_ios_turn_reply

        with self.assertRaises(LeaseLost):
            _store_ios_turn_reply(self.tenant, old, "late old answer")
        second.refresh_from_db()
        self.assertEqual(second.lease_token, new[0].lease_token)
        _store_ios_turn_reply(self.tenant, new, "answer")
        # Crash after durable reply, before queue retirement: the next drain
        # recovers without sending that user message a second time.
        self.drain()
        self.post.assert_not_called()
        self.assertEqual(self.app(second).reply_text, "answer")
        self.assertEqual(PendingMessage.objects.filter(payload__side_handoff=True).count(), 0)

    def test_transport_failure_retries_only_side_and_preserves_main(self):
        main = self.main()
        second = self.row()
        self.post.side_effect = httpx.ReadTimeout("offline")
        with self.assertRaises(RuntimeError):
            self.drain()
        main.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(main.delivery_attempts, 0)
        self.assertIsNotNone(main.delivery_in_flight_until)
        self.assertEqual(second.delivery_attempts, 1)
        self.post.side_effect = None
        self.drain()
        self.assertEqual(self.app(second).reply_text, "side answer")

    def test_main_finishes_during_side_then_cancellation_reaches_main(self):
        main = self.main()
        side = self.row("cancel that job and compare only two")

        def post(*args, **kwargs):
            if ":side:" in kwargs["json"]["user"]:
                from apps.router.pending_queue import _store_ios_turn_reply

                _store_ios_turn_reply(self.tenant, [main], "original job result")
                PendingMessage.objects.filter(pk=main.pk).delete()
                return _ok_chat_response("I'll pick it up as soon as it finishes")
            return _ok_chat_response("I have applied that change")

        self.post.side_effect = post
        with self.captureOnCommitCallbacks(execute=True):
            self.drain()
        self.assertTrue(self.publish.called)
        self.assertEqual(self.app(side).status, "ready")
        self.drain()
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["user"], f"thread:{self.thread.id}")
        self.assertIn("cancel that job and compare only two", payload["messages"][0]["content"])
        self.assertEqual(self.app(main).reply_text, "")
        followup = AppChatMessage.objects.get(client_msg_id__startswith="side-resume-")
        self.assertEqual(followup.user_text, "")
        self.assertEqual(followup.reply_text, "I have applied that change")
        self.assertEqual(self.drain()["delivered"], 0)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.usage.call_args.kwargs["event_type"], "chat_recap")

    def test_retrying_main_precedes_its_side_handoff_after_rejection(self):
        main = self.main()
        self.row("actually just give me three things")
        self.drain()
        # A queued handoff from the previous runtime must also wait for a retrying main.
        handoff = PendingMessage.objects.create(
            tenant=self.tenant,
            channel="ios",
            channel_user_id=str(self.thread.pk),
            side_main_id=main.id,
            payload={"side_handoff": True, "side_client_msg_ids": [], "user_param": f"thread:{self.thread.pk}"},
        )
        self.assertEqual(handoff.side_main_id, main.id)
        PendingMessage.objects.filter(pk=main.pk).update(delivery_in_flight_until=None)
        self.post.side_effect = httpx.HTTPStatusError(
            "rejected", request=httpx.Request("POST", "http://offline"), response=httpx.Response(503)
        )
        with self.assertRaises(RuntimeError):
            self.drain()
        self.post.side_effect = None
        self.post.reset_mock()
        self.drain()
        self.assertNotIn("earlier-from-you", self.post.call_args.kwargs["json"]["messages"][0]["content"])
        self.assertEqual(self.app(main).status, "ready")
        self.drain()
        self.assertIn("earlier-from-you", self.post.call_args.kwargs["json"]["messages"][0]["content"])
        self.assertEqual(self.post.call_count, 2)

    def test_deleted_thread_retires_rows_before_claim_and_after_post(self):
        for during_post in (False, True):
            with self.subTest(during_post=during_post):
                self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user)
                row = self.row()
                self.post.reset_mock()
                if during_post:

                    def post(*args, **kwargs):
                        self.thread.delete()
                        return _ok_chat_response("too late")

                    self.post.side_effect = post
                else:
                    self.thread.delete()
                # Deleting the object clears its in-memory pk; the queue still
                # contains its original UUID, as a real stale drain does.
                thread_id = row.channel_user_id
                for _ in range(2):
                    drain_pending_messages_for_tenant_task(str(self.tenant.id), "ios", thread_id)
                row.refresh_from_db()
                self.assertEqual(row.delivery_status, "failed")
                self.assertIsNone(row.delivery_in_flight_until)
                self.assertEqual(self.post.call_count, int(during_post))
                self.post.side_effect = None

    def test_deleted_thread_terminalizer_retires_without_handoff_or_post(self):
        from apps.router.pending_queue import _terminalize_failed_queue_rows

        row = self.row()
        self.thread.delete()
        for _ in range(2):
            _terminalize_failed_queue_rows(
                tenant=self.tenant,
                tenant_id=str(self.tenant.id),
                channel="ios",
                channel_user_id=row.channel_user_id,
                error="dropped",
            )
        self.assertEqual(PendingMessage.objects.get(pk=row.pk).delivery_status, "failed")
        self.post.assert_not_called()
        self.publish.assert_not_called()

    def test_handoff_silent_marker_is_removed_inside_visible_text(self):
        from apps.router.side_lane import store_handoff_reply

        row = self.row()
        row.payload = {"side_handoff": True, "side_client_msg_ids": []}
        row.save(update_fields=["payload"])
        store_handoff_reply(self.tenant, [row], "Noted. [[side-recap-no-action]]")
        reply = AppChatMessage.objects.get(client_msg_id__startswith="side-resume-")
        self.assertEqual(reply.reply_text, "Noted.")
        self.assertEqual(reply.user_text, "")

    def test_exhausted_handoff_writes_one_assistant_only_resend_notice(self):
        main = self.main()
        self.row("cancel that job")
        self.drain()
        self.finish(main)
        self.redact.side_effect = lambda text, *a, **kw: RedactionOutcome(text, False, "failed")
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self.drain()
        for _ in range(2):
            self.drain()
        notice = AppChatMessage.objects.get(client_msg_id__startswith="side-resume-")
        self.assertEqual(notice.thread_id, self.thread.id)
        self.assertEqual(notice.user_text, "")
        self.assertEqual(
            notice.reply_text, "I couldn't act on a note you sent while the job was running. Please send it again."
        )
        self.assertEqual(notice.status, "ready")

    def test_terminal_handoff_prepares_encryption_before_retirement_transaction(self):
        from django.db import transaction

        row = self.row()
        row.payload = {"side_handoff": True, "side_client_msg_ids": []}
        row.delivery_attempts = 3
        row.save(update_fields=["payload", "delivery_attempts"])
        original = transaction.atomic
        depth = [0]

        @contextmanager
        def tracked_atomic(*args, **kwargs):
            with original(*args, **kwargs):
                depth[0] += 1
                try:
                    yield
                finally:
                    depth[0] -= 1

        def encrypt(*args):
            self.assertEqual(depth[0], 0, "key unwrap must precede the retirement transaction")
            return None

        with (
            patch("apps.router.pending_queue.transaction.atomic", side_effect=tracked_atomic),
            patch("apps.router.chat_views._encrypt_chat_value", side_effect=encrypt) as encryption,
        ):
            self.drain()
        encryption.assert_called_once()
        self.assertEqual(AppChatMessage.objects.filter(client_msg_id__startswith="side-resume-").count(), 1)

    def test_side_preserves_one_shot_context_for_main(self):
        main = self.main()
        self.row("quick one")
        with (
            patch(
                "apps.router.proactive_context.surface_proactive_context", return_value="one-shot proactive\n"
            ) as proactive,
            patch("apps.router.thread_recap.build_thread_recap_block", return_value="one-shot recap\n") as recap,
        ):
            self.drain()
            proactive.assert_not_called()
            recap.assert_not_called()
            PendingMessage.objects.filter(pk=main.pk).update(delivery_in_flight_until=None)
            self.drain()
            proactive.assert_called_once()
            recap.assert_called_once()
            content = self.post.call_args.kwargs["json"]["messages"][0]["content"]
            self.assertIn("one-shot proactive", content)
            self.assertIn("one-shot recap", content)

    def test_lease_loss_during_failure_recovery_returns_the_outer_skip_result(self):
        for recovery in ("ordinary", "boot", "wake"):
            with self.subTest(recovery=recovery):
                PendingMessage.objects.all().delete()
                self.tenant.hibernated_at = timezone.now() if recovery == "wake" else None
                self.tenant.last_wake_at = timezone.now() if recovery == "boot" else None
                self.tenant.save(update_fields=["hibernated_at", "last_wake_at"])
                self.row("retry")
                self.post.side_effect = httpx.ReadTimeout("offline")
                with (
                    patch(
                        "apps.router.pending_queue._delivery_failure_has_down_container",
                        return_value=recovery != "ordinary",
                    ),
                    patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True),
                    patch("apps.router.pending_queue.save_owned_row", side_effect=LeaseLost("replaced")),
                ):
                    self.assertEqual(self.drain(), {"delivered": 0, "failed": 0, "dropped": 0, "skipped_in_flight": 1})

    def test_unconfirmed_context_is_omitted_and_full_text_checked_before_truncation(self):
        main = self.main()
        full = "x" * 3000 + " private-tail"
        PendingMessage.objects.filter(pk=main.pk).update(user_text=full)
        self.row()
        self.redact.return_value = RedactionOutcome(full, False, "failed")
        self.redact.side_effect = None
        self.drain()
        self.assertIn(full, [c.args[0] for c in self.redact.call_args_list])
        content = self.post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertNotIn("private-tail", content)
        self.assertNotIn("x" * 600, content)

    def test_progress_uses_real_session_shape_and_never_crosses_lanes(self):
        main = self.main()
        second = self.row()
        claim_chat_batch(self.tenant, str(self.thread.id), 120)
        base = AppChatMessage.objects.filter(tenant=self.tenant, status="pending")
        session = f"agent:main:openai-user:thread:{self.thread.id}"
        self.assertEqual(progress_rows(base, self.tenant.id, session, "").get().pk, self.app(main).pk)
        self.assertEqual(
            progress_rows(base, self.tenant.id, session + f":side:{main.id.hex[:12]}", "").get().pk, self.app(second).pk
        )
        self.assertFalse(progress_rows(base, self.tenant.id, "", "").exists())

    def test_feature_flag_on_me_response(self):
        client = APIClient()
        client.force_authenticate(self.user)
        self.assertIs(client.get("/api/v1/tenants/me/").data["keep_chatting"], True)
        with override_settings(KEEP_CHATTING_TENANT_IDS=""):
            self.assertIs(client.get("/api/v1/tenants/me/").data["keep_chatting"], False)

    def test_unflagged_and_other_channels_keep_old_claim_path(self):
        self.main()
        self.row()
        with override_settings(KEEP_CHATTING_TENANT_IDS=""):
            self.assertEqual(self.drain()["delivered"], 0)
        for channel in ("line", "telegram"):
            for row in PendingMessage.objects.all():
                row.channel = channel
                row.save(update_fields=["channel"])
            result = drain_pending_messages_for_tenant_task(str(self.tenant.id), channel, str(self.thread.id))
            self.assertEqual(result["delivered"], 0)
        self.post.assert_not_called()

    @override_settings(SUBAGENT_TENANT_IDS="")
    def test_unflagged_generated_config_and_prompts_are_byte_identical(self):
        from apps.orchestrator.config_generator import generate_openclaw_config
        from apps.orchestrator.personas import render_workspace_files

        with override_settings(KEEP_CHATTING_TENANT_IDS=""):
            baseline_config = json.dumps(generate_openclaw_config(self.tenant), sort_keys=True).encode()
            baseline_prompts = json.dumps(
                render_workspace_files("neighbor", tenant=self.tenant), sort_keys=True
            ).encode()
        with override_settings(KEEP_CHATTING_TENANT_IDS="00000000-0000-4000-8000-000000000999"):
            self.assertEqual(
                json.dumps(generate_openclaw_config(self.tenant), sort_keys=True).encode(), baseline_config
            )
            self.assertEqual(
                json.dumps(render_workspace_files("neighbor", tenant=self.tenant), sort_keys=True).encode(),
                baseline_prompts,
            )
        self.assertEqual(json.loads(baseline_config)["agents"]["defaults"]["maxConcurrent"], 2)

    def test_side_credit_failure_does_not_terminalize_the_running_main(self):
        main = self.main()
        side = self.row()
        with (
            patch("apps.router.pending_queue._looks_like_openrouter_credit_limit", return_value=True),
            patch("apps.router.pending_queue._handle_openrouter_credit_limit", return_value="circuit_tripped"),
        ):
            self.assertEqual(self.drain()["terminal"], "budget_exhausted")
        self.assertEqual(self.app(main).status, "pending")
        self.assertEqual(self.app(side).error, "budget_exhausted")
        main.refresh_from_db()
        self.assertIsNotNone(main.delivery_in_flight_until)

    def test_ingress_budget_applies_before_side_lane_enqueue(self):
        self.main()
        client = APIClient()
        client.force_authenticate(self.user)
        with patch("apps.router.chat_views.check_budget", return_value="monthly_limit"):
            response = client.post(
                "/api/v1/chat/messages/", {"client_msg_id": "budget-test", "text": "quick question"}, format="json"
            )
        self.assertEqual(response.data["error"], "budget_exhausted")
        self.assertEqual(PendingMessage.objects.count(), 1)
        self.post.assert_not_called()

    def test_side_log_has_only_required_numbers_and_ids(self):
        self.main()
        self.row("secret text must not be logged")
        with self.assertLogs("apps.router.side_lane", level="INFO") as logs:
            self.drain()
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(logs.output[0], r"side_lane tenant=[0-9a-f]{8} main_age_s=\d+ batch=1 post_ms=\d+ status=200$")
        self.assertNotIn("secret", logs.output[0])

    def test_main_recap_precedes_an_older_waiting_attachment(self):
        main = self.main()
        attachment = self.row("photo", is_image=True)
        self.row("cancel that job")
        self.drain()
        self.assertEqual(self.app(attachment).status, "pending")
        self.finish(main)
        self.post.return_value = _ok_chat_response("[[side-recap-no-action]]")
        self.drain()
        self.assertIn("earlier-from-you", self.post.call_args.kwargs["json"]["messages"][0]["content"])
        self.assertEqual(self.app(attachment).status, "pending")
        self.assertEqual(AppChatMessage.objects.filter(client_msg_id__startswith="side-resume-").count(), 0)

    def test_expired_main_lease_reuses_request_identity_and_fences_old_reply(self):
        main = self.row("long job")
        old, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
        PendingMessage.objects.filter(pk=main.pk).update(delivery_in_flight_until=timezone.now() - timedelta(seconds=1))
        new, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
        self.assertEqual(old[0].batch_id, new[0].batch_id)
        self.assertNotEqual(old[0].lease_token, new[0].lease_token)
        from apps.router.pending_queue import _store_ios_turn_reply

        with self.assertRaises(LeaseLost):
            _store_ios_turn_reply(self.tenant, old, "stale result")
        with self.assertRaises(LeaseLost):
            _store_ios_turn_reply(self.tenant, old, None)
        _store_ios_turn_reply(self.tenant, new, "main result")
        self.drain()
        self.assertEqual(self.app(main).reply_text, "main result")
        self.post.assert_not_called()

    def test_failed_side_answer_still_hands_cancellation_to_main(self):
        main = self.main()
        side = self.row("cancel the job")
        self.post.return_value = _ok_chat_response("")
        self.drain()
        self.assertEqual(self.app(side).error, "empty_response")
        self.finish(main)
        self.post.return_value = _ok_chat_response("I picked up the cancellation")
        self.drain()
        self.assertIn("cancel the job", self.post.call_args.kwargs["json"]["messages"][0]["content"])
        self.assertEqual(
            AppChatMessage.objects.get(client_msg_id__startswith="side-resume-").reply_text,
            "I picked up the cancellation",
        )

    def test_side_conversation_does_not_release_running_document_write_guard(self):
        from apps.router.document_write_guard import assert_write_allowed_for_document_turn

        self.tenant.document_ingestion_enabled = True
        main = self.main()
        main.payload["is_document"] = True
        main.save(update_fields=["payload"])
        AppChatMessage.objects.filter(pk=self.app(main).pk).update(
            attachment_path="workspace/media/inbound/doc_fixture.pdf"
        )
        self.row("a plain side question")
        response = assert_write_allowed_for_document_turn(self.tenant)
        self.assertEqual(response.status_code, 409)
        with override_settings(KEEP_CHATTING_TENANT_IDS=""):
            self.assertIsNone(assert_write_allowed_for_document_turn(self.tenant))

    def test_successful_boot_probe_clears_waking_before_long_main_post(self):
        main = self.row("slow first turn after waking")
        AppChatMessage.objects.filter(pk=self.app(main).pk).update(waking_at=timezone.now())

        def post(*args, **kwargs):
            self.assertIsNone(self.app(main).waking_at)
            return _ok_chat_response("answer")

        self.post.side_effect = post
        self.drain()
        self.health.assert_called()

    def test_long_side_change_is_not_truncated_out_of_main_handoff(self):
        main = self.main()
        text = "context " * 300 + "cancel that job and change the destination"
        self.row(text)
        self.drain()
        self.finish(main)
        self.drain()
        content = self.post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn(text, content)

    def test_unconfirmed_handoff_redaction_retries_without_consuming_changes(self):
        main = self.main()
        self.row("cancel that job")
        self.drain()
        self.finish(main)
        calls = self.post.call_count
        self.redact.side_effect = lambda text, *a, **kw: RedactionOutcome(text, False, "failed")
        with self.assertRaises(RuntimeError):
            self.drain()
        self.assertEqual(self.post.call_count, calls)
        self.assertTrue(PendingMessage.objects.filter(payload__side_handoff=True, delivery_status="pending").exists())
        self.redact.side_effect = lambda text, *a, **kw: RedactionOutcome(text, True, "ok")
        self.drain()
        self.assertIn("cancel that job", self.post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_old_server_insert_uses_database_main_lane_default(self):
        from django.db import connection

        row_id = uuid.uuid4()
        with connection.cursor() as cursor:
            # Simulate the pre-migration ORM: no new lane or lease metadata in
            # the INSERT. A Python-only default would break this during rollout.
            cursor.execute(
                "INSERT INTO pending_messages "
                "(id, tenant_id, channel, channel_user_id, payload, user_text, created_at, "
                "delivery_status, delivery_attempts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [row_id, self.tenant.id, "ios", str(self.thread.id), "{}", "old writer", timezone.now(), "pending", 0],
            )
        row = PendingMessage.objects.get(pk=row_id)
        self.assertEqual(row.lane, PendingMessage.Lane.MAIN)
        self.assertIsNone(row.lease_token)

    def test_a_handoff_that_becomes_long_work_still_allows_side_chat(self):
        from apps.router.pending_queue import _store_ios_turn_reply

        original = self.main()
        self.row("redo that long job using the latest numbers")
        self.drain()
        _store_ios_turn_reply(self.tenant, [original], "original result")
        PendingMessage.objects.filter(pk=original.pk).delete()
        handoff, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
        self.assertTrue(handoff[0].payload["side_handoff"])
        PendingMessage.objects.filter(pk=handoff[0].pk).update(turn_started_at=timezone.now() - timedelta(seconds=12))
        question = self.row("quick question during the changed job")
        self.drain()
        self.assertEqual(self.app(question).status, "ready")
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["user"], f"thread:{self.thread.id}:side:{handoff[0].id.hex[:12]}")
        self.assertIn("redo that long job using the latest numbers", payload["messages"][0]["content"])
        self.assertGreater(PendingMessage.objects.get(pk=handoff[0].pk).delivery_in_flight_until, timezone.now())
