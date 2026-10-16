"""Runtime task candidates are filtered and bounded before serialization."""

from datetime import date, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.renderers import JSONRenderer

from apps.journal.lifecycle_serializers import TaskSerializer
from apps.journal.models import Goal, Task
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeTaskListTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Task list", telegram_chat_id=818182)
        seed_internal_key(self.tenant)
        self.now = timezone.now()

    def get(self, **params):
        return self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/",
            params,
            HTTP_X_NBHD_INTERNAL_KEY="shared-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    def task(self, age, **kwargs):
        kwargs.setdefault("tenant", self.tenant)
        kwargs.setdefault("title", "Review paperwork")
        task = Task.objects.create(**kwargs)
        Task.objects.filter(pk=task.pk).update(updated_at=self.now - timedelta(days=age))
        task.refresh_from_db()
        return task

    def assert_legacy_bytes(self, response, qs):
        # This is the pre-candidate endpoint's exact response construction.
        rows = TaskSerializer(qs, many=True).data
        expected = JSONRenderer().render({"tenant_id": str(self.tenant.id), "tasks": rows, "count": len(rows)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, expected)

    def test_no_new_params_preserves_bytes_default_order_and_unlimited_results(self):
        for age in range(55):
            self.task(age, status="open" if age % 2 else "done", description="Full details", related_ref={"id": "x"})
        response = self.get()
        self.assert_legacy_bytes(response, Task.objects.filter(tenant=self.tenant))
        self.assertEqual(response.json()["count"], 55)
        self.assertEqual(
            list(response.json()["tasks"][0]),
            [
                "id",
                "title",
                "description",
                "pillar",
                "status",
                "due_date",
                "completed_at",
                "parent_goal_id",
                "related_ref",
                "created_at",
                "updated_at",
            ],
        )

    def test_existing_filters_preserve_bytes(self):
        goal = Goal.objects.create(tenant=self.tenant, title="Organize")
        match = self.task(20, pillar="gravity", parent_goal=goal, due_date=date(2026, 10, 10))
        self.task(30, pillar="gravity", parent_goal=goal, due_date=date(2026, 10, 11))
        self.task(40, pillar="gravity", due_date=date(2026, 10, 10))
        params = dict(
            status="open",
            pillar="gravity",
            parent_goal_id=str(goal.id),
            due_before="2026-10-10",
            due_after="2026-10-10",
        )
        self.assert_legacy_bytes(self.get(**params), Task.objects.filter(pk=match.pk))
        self.assertEqual(self.get(**params, stale_days=14, order="updated_at", limit=1).json()["count"], 1)

    def test_staleness_is_strict_and_scoped_to_tenant_and_status(self):
        oldest = self.task(30)
        next_oldest = self.task(20)
        boundary = self.task(14)
        self.task(1)
        self.task(50, status="in_progress")
        self.task(60, status="done")
        other = create_tenant(display_name="Other", telegram_chat_id=818183)
        self.task(70, tenant=other)
        with patch("apps.integrations.runtime_views.tz.now", return_value=self.now):
            response = self.get(status="open", stale_days=14, order="updated_at", limit=25)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["tasks"]], [str(oldest.id), str(next_oldest.id)])
        self.assertNotIn(str(boundary.id), response.content.decode())
        self.assertEqual(response.json()["count"], 2)

    def test_order_has_stable_ties_and_limit_is_applied_in_sql(self):
        tasks = [self.task(30) for _ in range(27)]
        expected = sorted(str(task.id) for task in tasks)[:25]
        with CaptureQueriesContext(connection) as queries:
            response = self.get(status="open", stale_days=14, order="updated_at", limit=25, fields="compact")
        self.assertEqual([row["id"] for row in response.json()["tasks"]], expected)
        self.assertEqual(response.json()["count"], 25)
        task_queries = [query["sql"] for query in queries if 'FROM "journal_tasks"' in query["sql"]]
        self.assertEqual(len(task_queries), 1)
        self.assertIn("LIMIT 25", task_queries[0])
        self.assertNotIn('"description"', task_queries[0])

    def test_new_params_can_be_used_independently(self):
        oldest = self.task(400)
        middle = self.task(2)
        newest = self.task(0)
        for days, count in ((1, 2), (365, 1)):
            with self.subTest(stale_days=days):
                self.assertEqual(self.get(stale_days=days).json()["count"], count)
        for limit, count in ((1, 1), (50, 3)):
            with self.subTest(limit=limit):
                response = self.get(limit=limit).json()
                self.assertEqual(response["count"], count)
                self.assertEqual(response["tasks"][0]["id"], str(newest.id))
        self.assertEqual(
            [row["id"] for row in self.get(order="updated_at").json()["tasks"]],
            [str(task.id) for task in (oldest, middle, newest)],
        )

    def test_invalid_candidate_params_return_400(self):
        for field, values in {
            "stale_days": ["", "0", "366", "-1", "1.5", "true", "abc", "9" * 5000],
            "limit": ["", "0", "51", "-1", "1.5", "true", "abc"],
            "order": ["", "-updated_at", "created_at", "title"],
            "fields": ["", "full", "title"],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value[:20]):
                    response = self.get(**{field: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"], "invalid_request")

    def test_compact_rows_include_goal_context_without_full_details_or_n_plus_one(self):
        goal = Goal.objects.create(tenant=self.tenant, title="Organize", description="Large goal detail")
        tasks = [self.task(20 + age, parent_goal=goal, description="Large task detail") for age in range(3)]
        orphan = self.task(10)
        with CaptureQueriesContext(connection) as queries:
            response = self.get(fields="compact")
        rows = response.json()["tasks"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(rows[0]["id"], str(orphan.id))
        self.assertIsNone(rows[0]["parent_goal"])
        self.assertEqual(rows[1]["id"], str(tasks[0].id))
        self.assertEqual(rows[1]["parent_goal"], {"id": str(goal.id), "title": "Organize"})
        self.assertEqual(set(rows[1]), {"id", "title", "status", "pillar", "due_date", "updated_at", "parent_goal"})
        lifecycle_queries = [
            q for q in queries if 'FROM "journal_tasks"' in q["sql"] or 'FROM "journal_goals"' in q["sql"]
        ]
        self.assertEqual(len(lifecycle_queries), 1)

    def test_compact_does_not_expose_cross_tenant_goal_title(self):
        other = create_tenant(display_name="Other", telegram_chat_id=818183)
        goal = Goal.objects.create(tenant=other, title="Other tenant goal")
        self.task(20, parent_goal=goal)
        self.assertIsNone(self.get(fields="compact").json()["tasks"][0]["parent_goal"])

    def test_compact_task_and_nested_goal_titles_keep_runtime_redaction(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Theo Smith"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        goal = Goal.objects.create(tenant=self.tenant, title="Help Theo Smith")
        self.task(20, title="Call [PERSON_1]", parent_goal=goal)
        response = self.get(fields="compact")
        row = response.json()["tasks"][0]
        self.assertEqual(row["title"], "Call [PERSON_1]")
        self.assertEqual(row["parent_goal"]["title"], "Help [PERSON_1]")
        self.assertNotIn("Theo Smith", response.content.decode())

    def test_empty_candidates_keep_response_envelope(self):
        self.task(0)
        self.assertEqual(
            self.get(stale_days=14, order="updated_at", limit=25, fields="compact").json(),
            {"tenant_id": str(self.tenant.id), "tasks": [], "count": 0},
        )
