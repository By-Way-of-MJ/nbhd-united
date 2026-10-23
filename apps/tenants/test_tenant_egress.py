"""Tenant row egress: keep the two large JSON columns off request-path SELECTs.

Measured on prod, a ``tenants`` row averages 44 KB (max 333 KB), mostly
``cron_jobs_snapshot`` (~22 KB) and ``openclaw_migration`` (~18 KB). Every
authenticated request and every runtime/tool call used to ship both back from
the DB. ``TenantManager`` now defers them, and ``select_related("tenant")``
paths defer them explicitly. These tests pin that shape so a future full-row
read cannot quietly restore the egress.
"""

from __future__ import annotations

import re

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.cron.cache import read_jobs_from_cache
from apps.integrations.runtime_views import _load_tenant_or_404
from apps.orchestrator.hibernation import _load_fallback_cron_jobs
from apps.tenants.models import Tenant
from apps.tenants.pat_models import PersonalAccessToken, generate_pat
from apps.tenants.services import create_tenant

HEAVY_COLUMNS = ("cron_jobs_snapshot", "openclaw_migration")
# Word boundaries keep the small boolean ``openclaw_migration_cron_fenced``
# column out of the match. Only the two JSON columns are the egress problem.
HEAVY_COLUMN_RE = re.compile(r"\b(?:cron_jobs_snapshot|openclaw_migration)\b")
SELECT_LIST_RE = re.compile(r"\bSELECT\b(.*?)\bFROM\b", re.IGNORECASE | re.DOTALL)


def _selected_column_lists(captured_queries):
    for query in captured_queries:
        match = SELECT_LIST_RE.search(query["sql"])
        if match:
            yield query["sql"], match.group(1)


class TenantHeavyColumnEgressTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Egress Test", telegram_chat_id=930_000_001)
        self.snapshot = {
            "jobs": [{"id": "job-1", "name": "User Job", "enabled": True}],
            "snapshot_at": "2026-10-23T00:00:00+00:00",
        }
        self.migration = {"status": "ROLLED_BACK", "owner_token": "opaque-token"}
        Tenant.objects.filter(pk=self.tenant.pk).update(
            cron_jobs_snapshot=self.snapshot,
            openclaw_migration=self.migration,
        )

    def _assert_no_heavy_columns(self, captured):
        offenders = [
            sql for sql, columns in _selected_column_lists(captured.captured_queries) if HEAVY_COLUMN_RE.search(columns)
        ]
        self.assertEqual(
            offenders,
            [],
            "request path selected a multi-KB tenant JSON column:\n" + (offenders[0] if offenders else ""),
        )

    def _jwt_client(self) -> APIClient:
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.tenant.user).access_token}")
        return client

    def _pat_client(self) -> APIClient:
        raw_token, prefix, token_hash = generate_pat()
        PersonalAccessToken.objects.create(
            user=self.tenant.user,
            name="egress-test",
            token_prefix=prefix,
            token_hash=token_hash,
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
        return client

    def test_jwt_request_does_not_select_heavy_tenant_columns(self):
        client = self._jwt_client()

        with CaptureQueriesContext(connection) as captured:
            response = client.get("/api/v1/tenants/profile/")

        self.assertEqual(response.status_code, 200)
        self._assert_no_heavy_columns(captured)

    def test_pat_request_does_not_select_heavy_tenant_columns(self):
        client = self._pat_client()

        with CaptureQueriesContext(connection) as captured:
            response = client.get("/api/v1/tenants/profile/")

        self.assertEqual(response.status_code, 200)
        self._assert_no_heavy_columns(captured)

    def test_load_tenant_or_404_does_not_select_heavy_columns(self):
        with CaptureQueriesContext(connection) as captured:
            tenant, failure = _load_tenant_or_404(self.tenant.id)

        self.assertIsNone(failure)
        self.assertEqual(tenant.id, self.tenant.id)
        self._assert_no_heavy_columns(captured)

    def test_manager_loaded_tenant_still_reads_both_values(self):
        tenant = Tenant.objects.get(pk=self.tenant.pk)

        self.assertEqual(tenant.get_deferred_fields(), set(HEAVY_COLUMNS))
        self.assertEqual(tenant.cron_jobs_snapshot, self.snapshot)
        self.assertEqual(tenant.openclaw_migration, self.migration)

    def test_cron_cache_reader_still_returns_snapshot_jobs(self):
        tenant = Tenant.objects.get(pk=self.tenant.pk)

        self.assertEqual(read_jobs_from_cache(tenant), self.snapshot["jobs"])

    def test_hibernation_fallback_reader_still_returns_snapshot_jobs(self):
        tenant = Tenant.objects.get(pk=self.tenant.pk)

        jobs = _load_fallback_cron_jobs(tenant)

        self.assertEqual([job["id"] for job in jobs], ["job-1"])
