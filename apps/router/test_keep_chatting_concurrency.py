"""PostgreSQL concurrency checks for independently executing chat drains."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from apps.integrations import runtime_views
from apps.journal.models import Document
from apps.router.models import ChatThread, PendingMessage
from apps.router.side_lane import claim_chat_batch
from apps.router.test_pending_queue import _make_tenant, _make_user


@override_settings(KEEP_CHATTING_TENANT_IDS="*", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ConcurrentLaneTests(TransactionTestCase):
    def setUp(self):
        self.tenant = _make_tenant(_make_user())
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.tenant.user, is_main=True)
        self.enterContext(patch("apps.cron.publish.publish_task"))
        self.enterContext(patch("apps.router.pending_queue._is_tenant_container_live", return_value=True))

    def run_concurrently(self, callbacks):
        def run(callback):
            try:
                return callback()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, callback) for callback in callbacks]
            return [future.result(timeout=15) for future in futures]

    def row(self):
        return PendingMessage.objects.create(
            tenant=self.tenant,
            channel="ios",
            channel_user_id=str(self.thread.id),
            payload={"client_msg_id": uuid.uuid4().hex},
            user_text="hello",
        )

    def test_two_drains_claim_one_side_batch_and_leave_main_owned(self):
        main = self.row()
        main.delivery_in_flight_until = timezone.now() + timedelta(seconds=120)
        main.turn_started_at = timezone.now() - timedelta(seconds=12)
        main.lease_token = uuid.uuid4()
        main.save(update_fields=["delivery_in_flight_until", "turn_started_at", "lease_token"])
        side = self.row()
        barrier = Barrier(2)

        def claim():
            barrier.wait(timeout=10)
            batch, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
            return [str(row.id) for row in batch]

        results = self.run_concurrently([claim, claim])
        self.assertEqual(sorted(len(result) for result in results), [0, 1])
        self.assertEqual([item for result in results for item in result], [str(side.id)])
        main.refresh_from_db()
        self.assertIsNotNone(main.lease_token)
        self.assertGreater(main.delivery_in_flight_until, timezone.now())

    def test_two_initial_drains_cannot_split_and_overlap_the_main_batch(self):
        rows = [self.row(), self.row()]
        barrier = Barrier(2)

        def claim():
            barrier.wait(timeout=10)
            batch, _ = claim_chat_batch(self.tenant, str(self.thread.id), 120)
            return [str(row.id) for row in batch]

        results = self.run_concurrently([claim, claim])
        self.assertEqual(sorted(len(result) for result in results), [0, 2])
        self.assertEqual(set(item for result in results for item in result), {str(row.id) for row in rows})

    def _parallel_document_writes(self, *, memory):
        day = "2026-11-01"
        doc = Document.objects.create(
            tenant=self.tenant,
            kind="memory" if memory else "daily",
            slug="long-term" if memory else day,
            title="note",
            markdown="# Note\n",
        )
        barrier = Barrier(2)
        author = runtime_views._author_runtime_document

        def author_together(*args, **kwargs):
            if args[1] in ("first fragment", "second fragment"):
                barrier.wait(timeout=10)
            return author(*args, **kwargs)

        def write(label):
            data = (
                {"markdown": label + " fragment", "section": label}
                if memory
                else {"content": label + " fragment", "date": day}
            )
            request = (APIRequestFactory().put if memory else APIRequestFactory().post)(
                "/offline/", data, format="json"
            )
            view = runtime_views.RuntimeUserMemoryView if memory else runtime_views.RuntimeDailyNoteAppendView
            return view.as_view()(request, tenant_id=self.tenant.id).status_code

        with (
            patch("apps.integrations.runtime_views._internal_auth_or_401", return_value=None),
            patch("apps.integrations.runtime_views._load_tenant_or_404", return_value=(self.tenant, None)),
            patch("apps.integrations.runtime_views._author_runtime_document", side_effect=author_together),
        ):
            self.assertEqual(
                self.run_concurrently([lambda: write("first"), lambda: write("second")]), [200 if memory else 201] * 2
            )
        doc.refresh_from_db()
        self.assertIn("first fragment", doc.markdown)
        self.assertIn("second fragment", doc.markdown)

    def test_two_daily_note_appends_both_survive(self):
        self._parallel_document_writes(memory=False)

    def test_two_memory_section_updates_both_survive(self):
        self._parallel_document_writes(memory=True)
