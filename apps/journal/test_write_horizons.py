"""Tenant-local quick-log timestamps and inclusive completion filtering."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.pii.testsupport import neural_ran
from apps.tenants.models import Tenant, User

from .models import Document, Task


class AppendTimestampTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="local-log", timezone="Asia/Tokyo", display_name="")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.doc = Document.objects.create(
            tenant=self.tenant, kind="daily", slug="2026-09-29", title="Day", markdown="# Day"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = "/api/v1/journal/documents/daily/2026-09-29/append/"
        detector = patch("apps.pii.redactor._detect_pii", side_effect=neural_ran([]))
        detector.start()
        self.addCleanup(detector.stop)

    def test_default_timestamp_uses_tenant_timezone_across_utc_midnight(self):
        with patch("apps.journal.document_views.timezone.now", return_value=datetime(2026, 9, 28, 16, 5, tzinfo=UTC)):
            response = self.client.post(self.url, {"content": "Log"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("### 01:05\n", response.data["markdown"])

    def test_timezone_front_door_falls_back_to_utc(self):
        self.user.timezone = "Invalid/Zone"
        self.user.save(update_fields=["timezone"])
        with patch("apps.journal.document_views.timezone.now", return_value=datetime(2026, 9, 29, 16, 5, tzinfo=UTC)):
            response = self.client.post(self.url, {"content": "Log"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("### 16:05\n", response.data["markdown"])

    def test_explicit_timestamp_is_preserved(self):
        response = self.client.post(self.url, {"content": "Log", "time": "09:45"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("### 09:45\n", response.data["markdown"])

    def test_invalid_time_is_400_and_does_not_write(self):
        for value in ("", "9:45", "09:5", "09:45:00", " 09:45", "09:45\n", "09:45\n# injected", None, 945):
            with self.subTest(value=value):
                response = self.client.post(self.url, {"content": "Log", "time": value}, format="json")
                self.assertEqual(response.status_code, 400)
                self.doc.refresh_from_db()
                self.assertEqual(self.doc.markdown, "# Day")


class CompletedAfterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="done-week", timezone="Asia/Tokyo")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.boundary = datetime(2026, 9, 27, 15, tzinfo=UTC)  # Monday midnight in Tokyo.
        self.before = Task.objects.create(
            tenant=self.tenant, title="Before", status="done", completed_at=self.boundary - timedelta(microseconds=1)
        )
        self.equal = Task.objects.create(
            tenant=self.tenant, title="Boundary", status="done", completed_at=self.boundary
        )
        self.after = Task.objects.create(
            tenant=self.tenant, title="After", status="done", completed_at=self.boundary + timedelta(days=1)
        )
        Task.objects.create(tenant=self.tenant, title="Uncompleted", status="open")
        Task.objects.create(tenant=self.tenant, title="Legacy done", status="done")
        other = Tenant.objects.create(user=User.objects.create_user(username="other-done-week"), status="active")
        Task.objects.create(tenant=other, title="Other tenant", status="done", completed_at=self.boundary)

    def filtered(self, value, **params):
        return self.client.get("/api/v1/journal/tasks/", {"completed_after": value, **params})

    def test_date_is_inclusive_tenant_local_midnight_and_composes_with_status(self):
        response = self.filtered("2026-09-28", status="done")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["id"] for row in response.data}, {str(self.equal.pk), str(self.after.pk)})

    def test_aware_datetimes_respect_offsets_and_include_exact_boundary(self):
        for value in ("2026-09-28T00:00:00+09:00", "2026-09-27T15:00:00Z", "2026-09-27T08:00:00-07:00"):
            with self.subTest(value=value):
                response = self.filtered(value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual({row["id"] for row in response.data}, {str(self.equal.pk), str(self.after.pk)})

    def test_naive_datetime_uses_tenant_timezone(self):
        response = self.filtered("2026-09-28T00:00:00.000001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [str(self.after.pk)])

    def test_absent_filter_retains_legacy_done_rows(self):
        response = self.client.get("/api/v1/journal/tasks/", {"status": "done"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 4)

    def test_malformed_completed_after_returns_400(self):
        for value in ("yesterday", "2026-02-30", "2026-09-28T25:00:00"):
            with self.subTest(value=value):
                response = self.filtered(value)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["error"], "completed_after must be an ISO date or datetime")
