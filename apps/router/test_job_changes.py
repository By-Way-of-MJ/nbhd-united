import json
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from pydantic import ValidationError

from apps.router import test_keep_chatting as keep_tests
from apps.router.job_changes import JobChange, classify_job_change
from apps.router.models import AppChatMessage, PendingMessage
from apps.router.test_pending_queue import _ok_chat_response


class JobChangeCheckTests(SimpleTestCase):
    def test_examples_use_structured_output_and_have_honest_exits(self):
        tenant = type("Tenant", (), {"id": "tenant-id"})()
        examples = {
            "is the plan done yet?": "none",
            "quick one, what time is my dentist visit?": "none",
            "actually skip the day by day, just give me the top 3 things": "change",
            "never mind, forget the plan": "cancel",
            "make it two weeks instead": "change",
        }
        for text, kind in examples.items():
            with self.subTest(text=text), patch("apps.router.job_changes.chat_completion") as call:
                call.return_value = (
                    {"choices": [{"message": {"content": json.dumps({"changes_job": kind != "none", "kind": kind})}}]},
                    "model",
                )
                self.assertEqual(classify_job_change(tenant, "plan the move", [text]).kind, kind)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.kwargs["timeout"], 6)
                self.assertEqual(
                    call.call_args.kwargs["response_format"]["json_schema"]["schema"], JobChange.model_json_schema()
                )
        for error in (requests.Timeout(), RuntimeError(), ValueError()):
            with (
                patch("apps.router.job_changes.chat_completion", side_effect=error),
                self.assertLogs("apps.router.job_changes") as logs,
            ):
                self.assertEqual(classify_job_change(tenant, "private", ["private"]).kind, "none")
            self.assertIn("job_change_check_failed", logs.output[0])
            self.assertNotIn("private", " ".join(logs.output))
        with self.assertRaises(ValidationError):
            JobChange(changes_job=False, kind="cancel")


@override_settings(KEEP_CHATTING_TENANT_IDS="*", NBHD_INTERNAL_API_KEY="test-key")
class JobPublicationTests(TestCase):
    setUp = keep_tests.KeepChattingTests.setUp
    row = keep_tests.KeepChattingTests.row
    main = keep_tests.KeepChattingTests.main
    drain = keep_tests.KeepChattingTests.drain
    app = keep_tests.KeepChattingTests.app

    def finish(self, main, text="THE CANCELLED FULL PLAN"):
        from apps.router.pending_queue import _store_ios_turn_reply

        _store_ios_turn_reply(self.tenant, [main], text)
        main.delete()

    def test_change_suppresses_raw_main_push_and_has_one_handoff(self):
        main = self.main()
        self.row("actually skip the day by day, just give me the top 3 things")
        self.drain()
        self.change_check.assert_called_once()
        with patch("apps.router.pending_queue._dispatch_push") as push:
            self.finish(main)
        push.assert_not_called()
        self.assertEqual(self.app(main).reply_text, "")
        self.assertEqual(self.app(main).status, "ready")
        self.assertEqual(PendingMessage.objects.filter(payload__side_handoff=True).count(), 1)
        self.post.return_value = _ok_chat_response("[[side-recap-no-action]]")
        self.drain()
        self.drain()
        self.assertEqual(self.post.call_count, 2)
        self.assertFalse(
            any("CANCELLED FULL PLAN" in text for text in AppChatMessage.objects.values_list("reply_text", flat=True))
        )

    def test_cancelled_job_cannot_refill_partial_preview(self):
        from apps.router.side_lane import progress_rows

        main = self.main()
        AppChatMessage.objects.filter(client_msg_id=main.payload["client_msg_id"]).update(partial_text="old plan")
        self.row("cancel the job")
        self.drain()
        self.assertEqual(self.app(main).partial_text, "")
        self.assertFalse(
            progress_rows(AppChatMessage.objects.all(), self.tenant.id, "", main.payload["client_msg_id"]).exists()
        )

    def test_cancel_handoff_cannot_republish_a_plan(self):
        self.change_check.side_effect = None
        self.change_check.return_value = JobChange(changes_job=True, kind="cancel")
        main = self.main()
        self.row("never mind, forget the plan")
        self.drain()
        self.finish(main)
        self.post.return_value = _ok_chat_response("Here is THE CANCELLED FULL PLAN anyway")
        self.drain()
        self.assertEqual(self.post.call_count, 2)
        self.assertFalse(
            any("CANCELLED FULL PLAN" in text for text in AppChatMessage.objects.values_list("reply_text", flat=True))
        )
        self.assertFalse(AppChatMessage.objects.filter(client_msg_id__startswith="side-resume-").exists())

    def test_status_never_posts_recap_and_next_main_gets_passive_context_once(self):
        main = self.main()
        side = self.row("is the plan done yet?")
        self.drain()
        self.finish(main, "plan")
        self.drain()
        self.assertEqual(self.post.call_count, 1)
        self.assertFalse(PendingMessage.objects.filter(payload__side_handoff=True).exists())
        self.row("what did I ask while you worked?")
        self.drain()
        content = self.post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("earlier-from-you", content)
        self.assertIn(side.user_text, content)
        self.row("thanks")
        self.drain()
        self.assertNotIn("earlier-from-you", self.post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_retry_reuses_check_and_checked_placeholder_inputs(self):
        import httpx

        self.main()
        row = self.row("make it two weeks instead")
        self.post.side_effect = httpx.ReadTimeout("offline")
        with self.assertRaises(RuntimeError):
            self.drain()
        row.refresh_from_db()
        self.assertIn("job_change", row.payload)
        self.post.side_effect = None
        self.drain()
        self.change_check.assert_called_once()
        self.assertNotIn("earlier-from-you", json.dumps(row.payload))

    def test_empty_ordinary_reply_leaves_passive_context_for_next_turn(self):
        main = self.main()
        self.row("is the plan done yet?")
        self.drain()
        self.finish(main, "plan")
        self.row("what did I ask?")
        self.post.return_value = _ok_chat_response("")
        self.drain()
        self.row("try again, what did I ask?")
        self.post.return_value = _ok_chat_response("You asked whether the plan was done.")
        self.drain()
        self.assertIn("is the plan done yet?", self.post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_cancel_during_a_long_adjustment_also_suppresses_its_result(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.router.side_lane import claim_chat_batch, store_handoff_reply

        main = self.main()
        self.row("actually make it two weeks")
        self.drain()
        self.finish(main)
        handoff, _ = claim_chat_batch(self.tenant, str(self.thread.pk), 120)
        PendingMessage.objects.filter(pk=handoff[0].pk).update(turn_started_at=timezone.now() - timedelta(seconds=12))
        self.change_check.side_effect = None
        self.change_check.return_value = JobChange(changes_job=True, kind="cancel")
        self.row("never mind, forget the plan")
        self.drain()
        with patch("apps.router.pending_queue._dispatch_push") as push:
            store_handoff_reply(self.tenant, handoff, "THE CANCELLED TWO WEEK PLAN")
        push.assert_not_called()
        self.assertFalse(AppChatMessage.objects.filter(client_msg_id=f"side-resume-{handoff[0].id.hex}").exists())

    def test_publication_is_fenced_before_the_first_side_pii_pass(self):
        from apps.router import side_lane
        from apps.router.pending_queue import _store_ios_turn_reply

        main = self.main()
        self.row("actually skip the plan")
        original = side_lane._excerpt
        seen = []

        def excerpt(*args, **kwargs):
            if not seen:
                seen.append(True)
                with self.assertRaisesRegex(RuntimeError, "side_job_change_pending"):
                    _store_ios_turn_reply(self.tenant, [main], "OLD PLAN")
            return original(*args, **kwargs)

        with patch("apps.router.side_lane._excerpt", side_effect=excerpt):
            self.drain()
        self.assertTrue(seen)
        self.finish(main)
        self.assertEqual(self.app(main).reply_text, "")


@override_settings(
    KEEP_CHATTING_TENANT_IDS="*", NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=False
)
class ConcurrentJobPublicationTests(TransactionTestCase):
    setUp = keep_tests.KeepChattingTests.setUp
    row = keep_tests.KeepChattingTests.row
    main = keep_tests.KeepChattingTests.main
    drain = keep_tests.KeepChattingTests.drain
    app = keep_tests.KeepChattingTests.app

    def test_post_overlaps_check_and_main_waits_then_suppresses_change_or_cancel(self):
        from threading import Event

        from apps.router.job_changes import JobChangePending
        from apps.router.pending_queue import _store_ios_turn_reply

        for kind in ("change", "cancel"):
            with self.subTest(kind=kind):
                main = self.main()
                self.row("please " + kind + " the day plan")
                entered, release = Event(), Event()

                def classify(*args, entered=entered, release=release, kind=kind):
                    from django.db import connection

                    self.assertFalse(connection.in_atomic_block)
                    entered.set()
                    self.assertTrue(release.wait(5), "POST never ran while the check was pending")
                    return JobChange(changes_job=True, kind=kind)

                def post(*args, entered=entered, release=release, main=main, **kwargs):
                    try:
                        self.assertTrue(entered.wait(5))
                        self.thread.refresh_from_db()
                        entry = self.thread.side_jobs[str(main.id)]
                        from django.utils import timezone

                        remaining = entry["checking_until"] - timezone.now().timestamp()
                        self.assertGreater(remaining, 20)
                        self.assertLessEqual(remaining, 30)
                        with self.assertRaises(JobChangePending):
                            _store_ios_turn_reply(self.tenant, [main], "OLD FULL DAY PLAN")
                    finally:
                        release.set()
                    return _ok_chat_response("I am still here.")

                self.change_check.side_effect = classify
                self.post.side_effect = post
                self.drain()
                self.thread.refresh_from_db()
                self.assertNotIn("checking_until", self.thread.side_jobs[str(main.id)])
                _store_ios_turn_reply(self.tenant, [main], "OLD FULL DAY PLAN")
                self.assertEqual(self.app(main).reply_text, "")
                self.assertEqual(self.thread.side_jobs[str(main.id)]["kind"], kind)
                self.assertTrue(PendingMessage.objects.filter(payload__side_job_id=str(main.id)).exists())
                main.delete()
                # Remove the scheduled handoff to isolate the next subcase.
                PendingMessage.objects.filter(payload__side_handoff=True).delete()

    def test_failed_or_timed_out_check_frees_fence_and_replay_does_not_classify(self):
        import httpx

        from apps.router.job_changes import check_batch
        from apps.router.side_lane import claim_chat_batch

        for error in (requests.Timeout(), RuntimeError("unavailable")):
            with self.subTest(error=type(error).__name__):
                main = self.main()
                side = self.row("never mind the plan")
                self.change_check.side_effect = error
                self.post.side_effect = httpx.ReadTimeout("offline")
                with self.assertRaises(RuntimeError):
                    self.drain()
                side.refresh_from_db()
                self.assertEqual(side.payload["job_change"], {"changes_job": False, "kind": "none"})
                self.thread.refresh_from_db()
                self.assertNotIn("checking_until", self.thread.side_jobs[str(main.id)])
                calls = self.change_check.call_count
                batch, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
                check_batch(self.tenant, batch, main.id, [], "day plan")
                self.assertEqual(self.change_check.call_count, calls)
                PendingMessage.objects.all().delete()

    def test_worker_sets_tenant_rls_and_closes_connections_on_success_and_failure(self):
        from threading import get_ident

        from django.db import connection, connections

        from apps.router.job_changes import concurrent_checks
        from apps.tenants.middleware import set_rls_context

        owner = get_ident()
        closed = []
        close = connections.close_all

        def close_worker():
            closed.append(get_ident())
            close()

        def check(tenant, *args):
            self.assertNotEqual(get_ident(), owner)
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('app.tenant_id'), current_setting('app.service_role')")
                self.assertEqual(cursor.fetchone(), (str(tenant.pk), "true"))
            if args[0][0]:
                raise RuntimeError("check failed")

        with (
            patch("apps.tenants.middleware.set_rls_context", wraps=set_rls_context) as rls,
            patch("django.db.connections.close_all", side_effect=close_worker),
            patch("apps.router.job_changes.check_batch", side_effect=check),
        ):
            with concurrent_checks() as start:
                start(self.tenant, [False])
            with self.assertRaisesRegex(RuntimeError, "check failed"), concurrent_checks() as start:
                start(self.tenant, [True])
        self.assertEqual(len(closed), 2)
        self.assertNotIn(owner, closed)
        self.assertEqual(rls.call_count, 2)
        rls.assert_called_with(tenant_id=self.tenant.pk, service_role=True)

    def test_expired_check_fence_does_not_hold_main_forever(self):
        from django.utils import timezone

        from apps.router.job_changes import finish_main
        from apps.router.side_lane import claim_chat_batch

        main = self.main()
        self.row("hi")
        claim_chat_batch(self.tenant, str(self.thread.id), 120)
        self.thread.refresh_from_db()
        self.thread.side_jobs[str(main.id)]["checking_until"] = timezone.now().timestamp() - 1
        self.thread.save(update_fields=["side_jobs"])
        self.assertFalse(finish_main(self.tenant, [main]))
