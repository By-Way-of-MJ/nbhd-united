"""Owner block edits retain concurrent writes, PII receipts and save hooks."""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.pii.testsupport import neural_ran
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

from .blocks import split_markdown_blocks
from .document_views import _author_owner_document
from .models import Document


class BlockReplaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="block-owner", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            layer1_placeholder_writes=True,
            pii_entity_map={"[PERSON_1]": {"name": "Alice"}, "[PERSON_2]": {"name": "Bob"}},
        )
        self.doc = Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug="2026-09-29",
            title="Daily",
            markdown="# Day\n\n### 09:00\nFirst entry\n\n## Later\n[PERSON_1]\n",
            pii_receipts={
                "title": {"state": "placeholder", "redactions": [], "writer": "owner"},
                "markdown": {"state": "placeholder", "redactions": [{"placeholder": "[PERSON_1]"}], "writer": "owner"},
            },
        )
        self.url = "/api/v1/journal/documents/daily/2026-09-29/blocks/replace/"
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.detector = patch("apps.pii.redactor._detect_pii", side_effect=neural_ran([]))
        self.detector.start()
        self.addCleanup(self.detector.stop)

    def replace(self, index=2, original="### 09:00\nFirst entry\n\n", replacement="### 09:00\nEdited", url=None):
        return self.client.post(
            url or self.url, {"index": index, "original": original, "replacement": replacement}, format="json"
        )

    def test_replace_preserves_other_stored_bytes_and_authors_known_pii(self):
        before = split_markdown_blocks(self.doc.markdown)
        title_receipt = self.doc.pii_receipts["title"]
        with patch("apps.journal.document_views._author_owner_document", wraps=_author_owner_document) as author:
            response = self.replace(replacement="### 09:00\nBob")
        self.assertEqual(response.status_code, 200)
        author.assert_called_once()
        self.assertEqual(author.call_args.kwargs, {"seam": "journal.document.block_replace", "field": "markdown"})
        self.doc.refresh_from_db()
        after = split_markdown_blocks(self.doc.markdown)
        self.assertEqual(after[:2], before[:2])
        self.assertEqual(after[3:], before[3:])
        self.assertEqual(after[2], "### 09:00\n[PERSON_2]\n\n")
        self.assertIn("Bob", response.data["markdown"])
        self.assertNotIn("Bob", self.doc.markdown)
        self.assertEqual(self.doc.pii_receipts["title"], title_receipt)
        self.assertEqual(
            {r["placeholder"] for r in self.doc.pii_receipts["markdown"]["redactions"]}, {"[PERSON_1]", "[PERSON_2]"}
        )

    def test_original_is_rehydrated_exactly_like_get(self):
        response = self.replace(index=3, original="## Later\nAlice\n", replacement="## Later\nBob\n")
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.markdown.endswith("## Later\n[PERSON_2]\n"))
        self.assertEqual(self.doc.pii_receipts["markdown"]["redactions"], [{"placeholder": "[PERSON_2]"}])

    def test_stale_original_returns_fresh_document_without_authoring_or_saving(self):
        before = self.doc.markdown, self.doc.pii_receipts, self.doc.updated_at
        with patch("apps.journal.document_views._author_owner_document") as author:
            response = self.replace(original="stale")
        author.assert_not_called()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "block_changed")
        self.assertIn("Alice", response.data["document"]["markdown"])
        self.doc.refresh_from_db()
        self.assertEqual((self.doc.markdown, self.doc.pii_receipts, self.doc.updated_at), before)

    def test_out_of_range_indices_conflict(self):
        for index in (-1, 99):
            with self.subTest(index=index):
                response = self.replace(index=index)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["document"]["id"], str(self.doc.pk))

    def test_assistant_append_during_authoring_survives_earlier_block_edit(self):
        seed_internal_key(self.tenant, "test-runtime-key")
        runtime = APIClient()

        def append_then_author(*args, **kwargs):
            response = runtime.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/append/",
                {"content": "Assistant addition", "date": self.doc.slug},
                format="json",
                HTTP_X_NBHD_INTERNAL_KEY="test-runtime-key",
                HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
            )
            self.assertEqual(response.status_code, 201)
            return _author_owner_document(*args, **kwargs)

        with patch("apps.journal.document_views._author_owner_document", side_effect=append_then_author):
            response = self.replace()
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertIn("Edited\n\n## Later", self.doc.markdown)
        self.assertIn("Assistant addition", self.doc.markdown)
        self.assertIn("[PERSON_1]", self.doc.markdown)

    def test_target_change_during_authoring_conflicts_under_row_lock(self):
        def change_then_author(*args, **kwargs):
            Document.objects.filter(pk=self.doc.pk).update(markdown="# Concurrent edit\n")
            return _author_owner_document(*args, **kwargs)

        with patch("apps.journal.document_views._author_owner_document", side_effect=change_then_author):
            response = self.replace()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["document"]["markdown"], "# Concurrent edit\n")
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.markdown, "# Concurrent edit\n")

    def test_delete_block_removes_its_receipt_placeholder(self):
        response = self.replace(index=3, original="## Later\nAlice\n", replacement="")
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.markdown, "# Day\n\n### 09:00\nFirst entry\n\n")
        self.assertEqual(self.doc.pii_receipts["markdown"]["redactions"], [])

    def test_preamble_can_be_empty_and_whitespace_is_preserved(self):
        response = self.replace(index=0, original="", replacement="  intro  ")
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.markdown.startswith("  intro  \n\n# Day"))

    def test_last_block_does_not_gain_newline(self):
        response = self.replace(index=3, original="## Later\nAlice\n", replacement="## End\n  trailing  ")
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.markdown.endswith("## End\n  trailing  "))

    def test_existing_newline_is_not_normalized(self):
        response = self.replace(replacement="### 09:00\nEdited\n")
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertIn("Edited\n## Later", self.doc.markdown)

    def test_typed_lifecycle_guards_match_patch(self):
        self.tenant.experimental_typed_journal_lifecycle = True
        self.tenant.save(update_fields=["experimental_typed_journal_lifecycle"])
        for kind in ("tasks", "goal"):
            response = self.replace(url=f"/api/v1/journal/documents/{kind}/test/blocks/replace/")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["error"], "typed_lifecycle_readonly")

    def test_legacy_document_with_compound_slug_is_editable(self):
        doc = Document.objects.create(
            tenant=self.tenant, kind="weekly", slug="week-ahead/2026-W40", title="Week", markdown="# Week"
        )
        response = self.replace(
            index=1,
            original="# Week",
            replacement="# Edited",
            url=f"/api/v1/journal/documents/weekly/{doc.slug}/blocks/replace/",
        )
        self.assertEqual(response.status_code, 200)
        doc.refresh_from_db()
        self.assertEqual(doc.markdown, "# Edited")

    def test_missing_or_foreign_document_is_not_created(self):
        other = Tenant.objects.create(user=User.objects.create_user(username="other-block-owner"), status="active")
        foreign = Document.objects.create(
            tenant=other, kind="ideas", slug="private", title="Private", markdown="# Private"
        )
        for slug in ("private", "absent"):
            response = self.replace(url=f"/api/v1/journal/documents/ideas/{slug}/blocks/replace/")
            self.assertEqual(response.status_code, 404)
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), 1)
        foreign.refresh_from_db()
        self.assertEqual(foreign.markdown, "# Private")

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertIn(self.replace().status_code, (401, 403))

    def test_malformed_payloads_return_400(self):
        for data in (
            {},
            {"index": True, "original": "", "replacement": ""},
            {"index": "1", "original": "", "replacement": ""},
            {"index": 1.0, "original": "", "replacement": ""},
            {"index": 1, "original": 12, "replacement": ""},
            {"index": 1, "original": "", "replacement": None},
        ):
            with self.subTest(data=data):
                self.assertEqual(self.client.post(self.url, data, format="json").status_code, 400)

    def test_save_queues_same_memory_sync_as_patch_after_commit(self):
        with patch("apps.cron.publish.publish_task") as publish, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.replace().status_code, 200)
        self.assertTrue(
            any(
                call.args[:2] == ("sync_documents_to_workspace", str(self.tenant.pk)) for call in publish.call_args_list
            )
        )

    def test_authoring_outside_endpoint_transaction_and_locked_recheck(self):
        # TestCase itself owns an outer atomic block: compare nesting depth.
        depth = len(connection.atomic_blocks)

        def author(*args, **kwargs):
            self.assertEqual(len(connection.atomic_blocks), depth)
            return _author_owner_document(*args, **kwargs)

        with (
            patch("apps.journal.document_views._author_owner_document", side_effect=author),
            CaptureQueriesContext(connection) as queries,
        ):
            self.assertEqual(self.replace().status_code, 200)
        self.assertTrue(any("FOR UPDATE" in query["sql"] and "journal_document" in query["sql"] for query in queries))
