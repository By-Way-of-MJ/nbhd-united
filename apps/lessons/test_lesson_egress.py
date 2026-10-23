"""Egress guard: the 1536-dimension lesson vector must not be shipped off the
DB on paths that never read it.

``Lesson.embedding`` is ~19 KB/row as text. Only the galaxy/constellation
affinity math (and the semantic search distance expression) actually needs the
column; everything else serializes representations that exclude it. These
tests pin that boundary on the captured SQL itself, so a future "select
everything" regression fails here instead of showing up in pg_stat_statements.
"""

from __future__ import annotations

from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.journal.models import Workspace
from apps.tenants.services import create_tenant

from .agent_context import recent_active_stars
from .clustering import generate_cluster_labels
from .models import Lesson, TutoringSession
from .services import search_lessons

EMBEDDING = [0.1] * 1536
LESSON_EMBEDDING_COLUMN = '"lessons"."embedding"'


def _split_top_level(text: str) -> list[str]:
    """Split a SQL select list on commas that are not inside parens/quotes."""
    items: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        items.append("".join(current).strip())
    return items


def select_columns(sql: str) -> list[str]:
    """The top-level items of a statement's first SELECT list (between ``SELECT`` and ``FROM``)."""
    upper = sql.upper()
    select_at = upper.find("SELECT ")
    if select_at == -1:
        return []
    body_start = select_at + len("SELECT ")
    depth = 0
    quote: str | None = None
    for index in range(body_start, len(sql)):
        char = sql[index]
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and upper.startswith(" FROM ", index):
            return _split_top_level(sql[body_start:index])
    return _split_top_level(sql[body_start:])


class LessonEmbeddingEgressTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Egress Tenant", telegram_chat_id=919191)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

        now = timezone.now()
        self.approved = [
            Lesson.objects.create(
                tenant=self.tenant,
                text=f"Approved lesson {index}",
                context="conversation",
                source_type="conversation",
                tags=["focus", "journal"],
                status="approved",
                cluster_id=index % 2,
                cluster_label="Focus",
                embedding=EMBEDDING,
                last_visited_at=now,
            )
            for index in range(6)
        ]
        self.pending = Lesson.objects.create(
            tenant=self.tenant,
            text="Pending lesson",
            context="conversation",
            source_type="conversation",
            tags=["focus"],
            status="pending",
            embedding=EMBEDDING,
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _assert_no_lesson_embedding_egress(self, context: CaptureQueriesContext) -> None:
        """No statement may select the bare lesson vector.

        A ``CosineDistance`` annotation legitimately *references* the column
        inside its expression (``lessons.embedding <=> $1``); that reference
        does not load the vector into Python. A bare selected column does.
        """
        for query in context.captured_queries:
            sql = query["sql"]
            columns = select_columns(sql)
            self.assertNotIn(
                LESSON_EMBEDDING_COLUMN,
                columns,
                f"lesson embedding selected as a column: {sql}",
            )
            for column in columns:
                if LESSON_EMBEDDING_COLUMN in column:
                    self.assertIn(
                        "<=>",
                        column,
                        f"lesson embedding selected outside a cosine-distance expression: {column}",
                    )

    def _assert_selects_lesson_embedding(self, context: CaptureQueriesContext) -> None:
        self.assertTrue(
            any(LESSON_EMBEDDING_COLUMN in select_columns(query["sql"]) for query in context.captured_queries),
            "expected at least one statement to load the lesson embedding (affinity math reads it)",
        )

    def _get(self, url: str):
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response, context

    # ── Read paths that must not ship the vector ─────────────────────────

    def test_lesson_list_does_not_ship_embedding(self):
        response, context = self._get("/api/v1/lessons/")
        self.assertEqual(len(response.json()), 6)
        self._assert_no_lesson_embedding_egress(context)

    def test_pending_list_does_not_ship_embedding(self):
        response, context = self._get("/api/v1/lessons/pending/")
        self.assertEqual([item["id"] for item in response.json()], [self.pending.id])
        self._assert_no_lesson_embedding_egress(context)

    def test_clusters_view_does_not_ship_embedding(self):
        response, context = self._get("/api/v1/lessons/clusters/")
        self.assertEqual(len(response.json()), 2)
        self._assert_no_lesson_embedding_egress(context)

    def test_generate_cluster_labels_does_not_ship_embedding(self):
        with CaptureQueriesContext(connection) as context:
            labeled = generate_cluster_labels(self.tenant)
        # ``generate_cluster_labels`` iterates the distinct cluster ids; the
        # model's default ordering can surface each id once per row, so assert
        # at least one label per seeded cluster rather than an exact count.
        self.assertGreaterEqual(labeled, 2)
        self._assert_no_lesson_embedding_egress(context)
        self.assertEqual(
            Lesson.objects.filter(tenant=self.tenant, status="approved").exclude(cluster_label="").count(),
            6,
        )

    def test_search_lessons_does_not_ship_embedding(self):
        with (
            patch("apps.lessons.services.generate_embedding", return_value=EMBEDDING),
            CaptureQueriesContext(connection) as context,
        ):
            results = list(search_lessons(tenant=self.tenant, query="focus", limit=5))
        self.assertEqual(len(results), 5)
        self._assert_no_lesson_embedding_egress(context)

    def test_recent_active_stars_does_not_ship_embedding(self):
        with CaptureQueriesContext(connection) as context:
            stars = recent_active_stars(self.tenant, limit=5)
        self.assertEqual(len(stars), 5)
        self._assert_no_lesson_embedding_egress(context)

    def test_workspace_list_does_not_ship_description_embedding(self):
        Workspace.objects.create(
            tenant=self.tenant,
            name="Work",
            slug="work",
            description="budget meetings",
            description_embedding=EMBEDDING,
        )
        response, context = self._get("/api/v1/workspaces/")
        self.assertTrue(response.json()["workspaces"])
        for query in context.captured_queries:
            self.assertNotIn(
                '"journal_workspaces"."description_embedding"',
                select_columns(query["sql"]),
                f"workspace description embedding selected as a column: {query['sql']}",
            )

    def test_tutoring_insights_does_not_ship_star_embedding(self):
        TutoringSession.objects.create(star=self.approved[0])
        response, context = self._get("/api/v1/lessons/galaxy/insights/")
        self.assertTrue(response.json())
        for query in context.captured_queries:
            for column in select_columns(query["sql"]):
                self.assertNotIn(
                    LESSON_EMBEDDING_COLUMN,
                    column,
                    f"star embedding selected on the tutoring insights join: {query['sql']}",
                )

    # ── Positive guard: the affinity paths still read the vector ─────────

    def test_constellation_still_reads_embedding_for_affinity(self):
        response, context = self._get("/api/v1/lessons/constellation/")
        self.assertTrue(response.json()["affinity_edges"])
        self._assert_selects_lesson_embedding(context)

    def test_galaxy_still_reads_embedding_for_affinity(self):
        response, context = self._get("/api/v1/lessons/galaxy/")
        affinity = [edge for edge in response.json()["edges"] if edge["connection_type"] == "affinity"]
        self.assertTrue(affinity)
        self._assert_selects_lesson_embedding(context)
