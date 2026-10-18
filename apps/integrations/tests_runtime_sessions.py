"""Tests for the YardTalk session distillation runtime endpoints.

Covers:
- RuntimeSessionsPendingView (list undistilled non-test sessions)
- RuntimeSessionsRecentView (read recent sessions regardless of filing status)
- RuntimeSessionMarkProcessedView (mark distilled, idempotent)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.session_models import Session
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


def _make_session(
    tenant,
    *,
    project="proj",
    session_start=None,
    test_mode=False,
    processed_at=None,
    processed_summary=None,
    summary="A session.",
):
    start = session_start or datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    end = start.replace(hour=start.hour + 1)
    return Session.objects.create(
        tenant=tenant,
        source="yardtalk-mac/0.1.0",
        project=project,
        session_start=start,
        session_end=end,
        summary=summary,
        test_mode=test_mode,
        processed_at=processed_at,
        processed_summary=processed_summary or {},
    )


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeSessionsPendingViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Pending Tenant", telegram_chat_id=911001)
        seed_internal_key(self.tenant)
        self.other_tenant = create_tenant(display_name="Other Tenant", telegram_chat_id=911002)

    def _url(self, tenant_id=None):
        tid = tenant_id or self.tenant.id
        return f"/api/v1/integrations/runtime/{tid}/sessions/pending/"

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(tenant_id or self.tenant.id),
        }

    def test_returns_only_unprocessed_non_test_sessions(self):
        pending = _make_session(self.tenant, project="pending-one")
        _make_session(
            self.tenant,
            project="processed-one",
            processed_at=datetime(2026, 5, 7, 13, 0, tzinfo=UTC),
            processed_summary={"notes": "filed"},
        )
        _make_session(self.tenant, project="test-one", test_mode=True)

        response = self.client.get(self._url(), **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["sessions"][0]["id"], str(pending.id))
        self.assertEqual(body["sessions"][0]["project"], "pending-one")

    def test_orders_by_session_start_desc(self):
        older = _make_session(
            self.tenant,
            project="older",
            session_start=datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
        )
        newer = _make_session(
            self.tenant,
            project="newer",
            session_start=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
        )

        response = self.client.get(self._url(), **self._headers())
        body = response.json()
        ids = [s["id"] for s in body["sessions"]]
        self.assertEqual(ids, [str(newer.id), str(older.id)])

    def test_respects_limit_param(self):
        for i in range(5):
            _make_session(
                self.tenant,
                project=f"p{i}",
                session_start=datetime(2026, 5, 7, 9 + i, 0, tzinfo=UTC),
            )

        response = self.client.get(self._url(), {"limit": 2}, **self._headers())
        body = response.json()
        self.assertEqual(body["count"], 2)

    def test_caps_limit_at_25(self):
        for i in range(30):
            _make_session(
                self.tenant,
                project=f"p{i}",
                session_start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC).replace(minute=i % 60),
            )

        response = self.client.get(self._url(), {"limit": 999}, **self._headers())
        body = response.json()
        self.assertEqual(body["count"], 25)

    def test_invalid_limit_returns_400(self):
        response = self.client.get(self._url(), {"limit": "abc"}, **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_other_tenant_sessions_not_visible(self):
        _make_session(self.other_tenant, project="other-tenant-session")

        response = self.client.get(self._url(), **self._headers())
        body = response.json()
        self.assertEqual(body["count"], 0)

    def test_requires_internal_auth(self):
        _make_session(self.tenant)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 401)

    def test_returns_full_payload_fields(self):
        session = _make_session(self.tenant, project="full-fields")
        session.accomplishments = ["did a thing"]
        session.next_steps = ["do another thing"]
        session.references = {"clip_ids": ["abc"]}
        session.save()

        response = self.client.get(self._url(), **self._headers())
        body = response.json()
        s = body["sessions"][0]
        self.assertEqual(s["accomplishments"], ["did a thing"])
        self.assertEqual(s["next_steps"], ["do another thing"])
        self.assertEqual(s["references"], {"clip_ids": ["abc"]})
        # All distillation-relevant keys present
        for key in (
            "id",
            "summary",
            "blockers",
            "session_start",
            "session_end",
            "project",
            "project_identity",
            "project_type",
            "source",
        ):
            self.assertIn(key, s)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeSessionsRecentViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Recent Tenant", telegram_chat_id=911005)
        seed_internal_key(self.tenant)
        self.other_tenant = create_tenant(display_name="Other Tenant", telegram_chat_id=911006)
        self.now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
        clock = patch("apps.integrations.runtime_views.tz.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/sessions/recent/"
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _get(self, **params):
        return self.client.get(self.url, params, **self.headers)

    def test_includes_processed_and_pending_without_mutating(self):
        pending = _make_session(self.tenant)
        processed = _make_session(
            self.tenant,
            session_start=self.now - timedelta(hours=2),
            processed_at=self.now - timedelta(hours=1),
            processed_summary={"notes": "filed"},
        )
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tenant_id"], str(self.tenant.id))
        self.assertEqual(body["count"], 2)
        self.assertEqual([s["id"] for s in body["sessions"]], [str(processed.id), str(pending.id)])
        self.assertEqual(body["sessions"][0]["processed_at"], processed.processed_at.isoformat())
        self.assertIsNone(body["sessions"][1]["processed_at"])
        pending.refresh_from_db()
        processed.refresh_from_db()
        self.assertIsNone(pending.processed_at)
        self.assertEqual(pending.processed_summary, {})
        self.assertEqual(processed.processed_summary, {"notes": "filed"})
        self.assertEqual(processed.processed_at, self.now - timedelta(hours=1))

    def test_excludes_test_mode_and_other_tenant(self):
        _make_session(self.tenant, test_mode=True)
        _make_session(self.other_tenant)
        self.assertEqual(self._get().json()["sessions"], [])

    def test_default_days_uses_start_with_inclusive_boundary(self):
        boundary = _make_session(self.tenant, session_start=self.now - timedelta(days=7))
        # Ends inside the window, but starts outside it.
        _make_session(self.tenant, session_start=self.now - timedelta(days=7, seconds=1))
        _make_session(self.tenant, session_start=self.now + timedelta(seconds=1))
        self.assertEqual([s["id"] for s in self._get().json()["sessions"]], [str(boundary.id)])

    def test_custom_days_and_90_day_cap(self):
        recent = _make_session(self.tenant, session_start=self.now - timedelta(days=2))
        older = _make_session(self.tenant, session_start=self.now - timedelta(days=90))
        _make_session(self.tenant, session_start=self.now - timedelta(days=90, seconds=1))
        self.assertEqual(self._get(days=1).json()["count"], 0)
        self.assertEqual(self._get(days=2).json()["count"], 1)
        self.assertEqual(
            [s["id"] for s in self._get(days=999).json()["sessions"]],
            [str(recent.id), str(older.id)],
        )

    def test_project_matches_name_or_identity_case_insensitively(self):
        named = _make_session(self.tenant, project="My YardTalk App")
        identified = _make_session(self.tenant, project="desktop", session_start=self.now)
        identified.project_identity = "https://example.test/yardtalk/repo"
        identified.save(update_fields=["project_identity"])
        _make_session(self.tenant, project="unrelated")
        self.assertEqual(
            [s["id"] for s in self._get(project="  YARDtalk  ").json()["sessions"]],
            [str(identified.id), str(named.id)],
        )
        self.assertEqual(self._get(project="missing").json()["count"], 0)
        self.assertEqual(self._get(project=" ").json()["count"], 3)

    def test_limit_default_custom_and_cap(self):
        sessions = [_make_session(self.tenant, session_start=self.now - timedelta(minutes=i)) for i in range(30)]
        for limit, expected in ((None, 10), (2, 2), (999, 25)):
            with self.subTest(limit=limit):
                params = {} if limit is None else {"limit": limit}
                body = self._get(**params).json()
                self.assertEqual(body["count"], expected)
                self.assertEqual(
                    [s["id"] for s in body["sessions"]],
                    [str(s.id) for s in sessions[:expected]],
                )

    def test_bad_numeric_params_return_400(self):
        for name in ("days", "limit"):
            for value in ("abc", "1.5", "0", "-1"):
                with self.subTest(name=name, value=value):
                    response = self._get(**{name: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"], "invalid_request")
                    self.assertTrue(response.json()["detail"].startswith(name))

    def test_requires_internal_auth(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(
            self.client.get(self.url, **{**self.headers, "HTTP_X_NBHD_INTERNAL_KEY": "bad"}).status_code,
            401,
        )

    def test_tenant_header_mismatch_rejected(self):
        response = self.client.get(self.url, **{**self.headers, "HTTP_X_NBHD_TENANT_ID": str(self.other_tenant.id)})
        self.assertEqual(response.status_code, 401)

    def test_payload_matches_pending_serializer_plus_processed_at(self):
        session = _make_session(self.tenant)
        session.accomplishments = ["shipped"]
        session.blockers = ["waiting"]
        session.next_steps = ["review"]
        session.references = {"clip_ids": ["abc"]}
        # Dark sidecars must not change the existing pending read path.
        session.summary_enc = b"dark-sidecar"
        session.save()
        pending = self.client.get(self.url.replace("recent/", "pending/"), **self.headers)
        expected = {**pending.json()["sessions"][0], "processed_at": None}
        self.assertEqual(self._get().json()["sessions"][0], expected)
        self.assertNotIn("processed_at", pending.json()["sessions"][0])
        self.assertNotIn("summary_enc", expected)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeSessionMarkProcessedViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Mark Tenant", telegram_chat_id=911003)
        seed_internal_key(self.tenant)
        self.other_tenant = create_tenant(display_name="Other Tenant", telegram_chat_id=911004)
        self.session = _make_session(self.tenant)

    def _url(self, session_id=None, tenant_id=None):
        tid = tenant_id or self.tenant.id
        sid = session_id or self.session.id
        return f"/api/v1/integrations/runtime/{tid}/sessions/{sid}/mark-processed/"

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(tenant_id or self.tenant.id),
        }

    def test_marks_session_processed_and_stores_summary(self):
        summary = {
            "daily_note_date": "2026-05-07",
            "note": "Filed Alice exactly",
            "tasks_added": ["t1"],
            "memory_updated": True,
        }
        response = self.client.post(
            self._url(),
            data=summary and {"processed_summary": summary},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], str(self.session.id))
        self.assertFalse(body["already_processed"])
        self.assertEqual(body["processed_summary"], summary)
        self.assertNotIn("pii_receipts", body)

        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.processed_at)
        self.assertEqual(self.session.processed_summary, summary)
        self.assertEqual(
            self.session.pii_receipts["processed_summary"],
            {"state": "bypass", "writer": "runtime"},
        )

    def test_flag_on_stores_and_returns_processed_summary_in_placeholder_space(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        summary = {
            "note": "Filed for Alice",
            "nested": {"reviewer": "Alice"},
        }

        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring._detect_pii", return_value=[]),
        ):
            response = self.client.post(
                self._url(),
                data={"processed_summary": summary},
                content_type="application/json",
                **self._headers(),
            )

        placeholder_summary = {
            "note": "Filed for [PERSON_1]",
            "nested": {"reviewer": "[PERSON_1]"},
        }
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processed_summary"], placeholder_summary)
        self.assertNotIn("pii_receipts", response.json())
        self.session.refresh_from_db()
        self.assertEqual(self.session.processed_summary, placeholder_summary)
        self.assertEqual(self.session.pii_receipts["processed_summary"]["writer"], "runtime")
        self.assertEqual(self.session.pii_receipts["processed_summary"]["state"], "unconfirmed")
        self.assertEqual(self.session.pii_receipts["processed_summary"]["reason"], "detector-deferred")

    def test_idempotent_when_already_processed(self):
        first = self.client.post(
            self._url(),
            data={"processed_summary": {"first": True}},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(first.status_code, 200)
        first_processed_at = first.json()["processed_at"]

        second = self.client.post(
            self._url(),
            data={"processed_summary": {"second": True}},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertTrue(body["already_processed"])
        # Original processed_at is preserved; summary is NOT overwritten.
        self.assertEqual(body["processed_at"], first_processed_at)
        self.assertEqual(body["processed_summary"], {"first": True})

    def test_omitted_summary_defaults_to_empty_dict(self):
        response = self.client.post(
            self._url(),
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processed_summary"], {})

    def test_invalid_summary_type_returns_400(self):
        response = self.client.post(
            self._url(),
            data={"processed_summary": "not a dict"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_session_not_found_returns_404(self):
        bogus_id = "00000000-0000-0000-0000-000000000000"
        response = self.client.post(
            self._url(session_id=bogus_id),
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    def test_other_tenant_session_returns_404(self):
        # A session that belongs to other_tenant: caller hits their own
        # tenant URL but with that session_id — should 404, never reveal it.
        other_session = _make_session(self.other_tenant)
        response = self.client.post(
            self._url(session_id=other_session.id),
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    def test_requires_internal_auth(self):
        response = self.client.post(
            self._url(),
            data={},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
