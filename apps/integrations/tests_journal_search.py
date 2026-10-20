"""Tests for the RuntimeJournalSearchView endpoint."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.models import Document, DocumentChunk
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeJournalSearchViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Search Tenant", telegram_chat_id=808080)
        seed_internal_key(self.tenant)
        self.other_tenant = create_tenant(display_name="Other Tenant", telegram_chat_id=909090)

        self.doc1 = Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug="2026-02-10",
            title="Daily Note 2026-02-10",
            markdown="Today I worked on the **garden** project and planted tomatoes.",
        )
        self.doc2 = Document.objects.create(
            tenant=self.tenant,
            kind="goal",
            slug="fitness",
            title="Fitness Goal",
            markdown="Run a marathon by end of year. Training plan includes weekly long runs.",
        )
        self.doc3 = Document.objects.create(
            tenant=self.other_tenant,
            kind="daily",
            slug="2026-02-10",
            title="Other Tenant Note",
            markdown="This tenant also has a garden but should not appear.",
        )

    def _url(self, tenant_id=None):
        tid = tenant_id or self.tenant.id
        return f"/api/v1/integrations/runtime/{tid}/journal/search/"

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(tenant_id or self.tenant.id),
        }

    def test_search_returns_matching_documents(self):
        response = self.client.get(self._url(), {"q": "garden"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["query"], "garden")
        self.assertGreaterEqual(body["count"], 1)
        slugs = [r["slug"] for r in body["results"]]
        self.assertIn("2026-02-10", slugs)

    def test_search_respects_kind_filter(self):
        response = self.client.get(self._url(), {"q": "marathon", "kind": "goal"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertGreaterEqual(body["count"], 1)
        for result in body["results"]:
            self.assertEqual(result["kind"], "goal")

    def test_search_kind_filter_excludes_other_kinds(self):
        response = self.client.get(self._url(), {"q": "garden", "kind": "goal"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)

    def test_empty_query_returns_400(self):
        response = self.client.get(self._url(), {"q": ""}, **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_missing_query_returns_400(self):
        response = self.client.get(self._url(), **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_search_does_not_leak_other_tenants_docs(self):
        response = self.client.get(self._url(), {"q": "garden"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        slugs_and_titles = [(r["slug"], r["title"]) for r in body["results"]]
        for slug, title in slugs_and_titles:
            self.assertNotEqual(title, "Other Tenant Note")

    def test_requires_internal_auth(self):
        response = self.client.get(self._url(), {"q": "garden"})
        self.assertEqual(response.status_code, 401)

    def test_respects_limit_parameter(self):
        response = self.client.get(self._url(), {"q": "garden", "limit": "1"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertLessEqual(body["count"], 1)

    @patch("apps.lessons.services.generate_embedding")
    def test_semantic_search_rescues_meaning_match(self, mock_embedding):
        for index in range(12):
            Document.objects.create(
                tenant=self.tenant,
                kind="daily",
                slug=f"2026-03-{index + 1:02d}",
                title=f"Daily Note 2026-03-{index + 1:02d}",
                markdown=f"Today's photo walk around the neighbourhood, number {index}.",
            )
        rescue = Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug="2026-03-20",
            title="Daily Note 2026-03-20",
            markdown="Nearest studios in Horie: PADstudio, a short walk from the river.",
        )
        query_vector = [0.01] * 1536
        DocumentChunk.objects.create(
            tenant=self.tenant,
            document=rescue,
            chunk_index=0,
            text="Nearest studios in Horie: PADstudio, a short walk from the river.",
            embedding=query_vector,
            source_date="2026-03-20",
        )
        mock_embedding.return_value = query_vector

        response = self.client.get(self._url(), {"q": "photo places Horie"}, **self._headers())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["results"][0]["slug"], rescue.slug)
        self.assertIn(body["results"][0]["match"], {"semantic", "both"})

    @patch("apps.lessons.services.generate_embedding")
    def test_embedding_failure_falls_back_to_keyword_results(self, mock_embedding):
        # No chunk exists yet, so the keyword-only response is the baseline the
        # failing semantic arm must reproduce exactly.
        keyword_body = self.client.get(self._url(), {"q": "garden"}, **self._headers()).json()

        DocumentChunk.objects.create(
            tenant=self.tenant,
            document=self.doc1,
            chunk_index=0,
            text="Today I worked on the garden project and planted tomatoes.",
            embedding=[0.02] * 1536,
            source_date="2026-02-10",
        )
        mock_embedding.side_effect = RuntimeError("embedding provider unavailable")

        response = self.client.get(self._url(), {"q": "garden"}, **self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), keyword_body)
        mock_embedding.assert_called_once()
