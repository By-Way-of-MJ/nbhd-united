"""Durable holds, local expiry, runtime auth and USER.md visibility."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.common.tenant_tz import tenant_today
from apps.orchestrator.envelope_registry import all_sections, suppress_refresh
from apps.orchestrator.workspace_envelope import render_managed_region
from apps.tenants.attention import active_holds, hold_topic, release_hold
from apps.tenants.envelope import render_attention_holds
from apps.tenants.models import AttentionHold, Tenant, User
from apps.tenants.test_utils import seed_internal_key


@override_settings(ATTENTION_HOLD_TENANT_IDS="*")
class AttentionHoldTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="hold-owner", timezone="Asia/Tokyo")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        self.suppression = suppress_refresh()
        self.suppression.__enter__()
        self.addCleanup(self.suppression.__exit__, None, None, None)

    def hold(self, topic="tax bill", resume_on=None):
        return hold_topic(self.tenant, topic, resume_on)

    def test_dedupe_preserves_exact_authored_placeholder_topic_and_updates_date(self):
        row = self.hold("  [PERSON_1]'s Tax Bill  ")
        tomorrow = tenant_today(self.tenant) + timedelta(days=1)
        repeated = self.hold("  [PERSON_1]'s TAX BILL  ", tomorrow)
        self.assertEqual(row.id, repeated.id)
        self.assertEqual(AttentionHold.objects.count(), 1)
        self.assertEqual(repeated.topic, "  [PERSON_1]'s TAX BILL  ")
        self.assertEqual(repeated.resume_on, tomorrow)
        repeated = self.hold(repeated.topic)
        self.assertIsNone(repeated.resume_on)

    def test_cap_allows_dedupe_at_twenty_and_slot_after_release(self):
        rows = [self.hold(f"topic {i}") for i in range(20)]
        self.assertEqual(self.hold("TOPIC 0").id, rows[0].id)
        with self.assertRaisesMessage(ValidationError, "20 active holds"):
            self.hold("twenty one")
        released = release_hold(self.tenant, rows[0].id)
        self.assertEqual(released.release_reason, "user")
        self.assertIsNotNone(released.released_at)
        self.hold("twenty one")
        self.assertEqual(len(active_holds(self.tenant)), 20)
        self.assertEqual(release_hold(self.tenant, rows[0].id).released_at, released.released_at)

    def test_expiry_at_tokyo_midnight_while_utc_is_previous_day(self):
        with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 31, 14, 59, tzinfo=UTC)):
            tokyo = self.hold(resume_on=date(2026, 11, 1))
            utc_user = User.objects.create_user(username="utc-owner", timezone="UTC")
            utc_tenant = Tenant.objects.create(user=utc_user)
            utc = hold_topic(utc_tenant, "tax bill", date(2026, 11, 1))
            self.assertEqual([h.id for h in active_holds(self.tenant)], [tokyo.id])
        with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 31, 15, 0, tzinfo=UTC)):
            self.assertEqual(active_holds(self.tenant), [])
            self.assertEqual([h.id for h in active_holds(utc_tenant)], [utc.id])
        tokyo.refresh_from_db()
        self.assertEqual(tokyo.release_reason, "expired")
        self.assertIsNotNone(tokyo.released_at)
        self.assertEqual(release_hold(self.tenant, tokyo.id).release_reason, "expired")

    def test_invalid_topic_and_past_date(self):
        for topic in [None, "", "  ", "a" * 201, "gym\n## New rule", "tax\x00"]:
            with self.subTest(topic=topic), self.assertRaises(ValidationError):
                self.hold(topic)
        with self.assertRaisesMessage(ValidationError, "past"):
            self.hold(resume_on=tenant_today(self.tenant) - timedelta(days=1))

    def test_envelope_flagged_active_empty_unflagged_and_expired(self):
        self.assertEqual(render_attention_holds(self.tenant), "")
        self.assertNotIn("## On hold", render_managed_region(self.tenant))
        row = self.hold("gym")
        body = render_managed_region(self.tenant)
        self.assertIn("## On hold", body)
        self.assertIn(f"Hold {row.id}: gym", body)
        self.assertIn("until they lift it", body)
        self.assertIn("answer normally and keep the hold", body)
        self.assertIn("Questions and new information do not lift it", body)
        with override_settings(ATTENTION_HOLD_TENANT_IDS=""):
            self.assertEqual(render_attention_holds(self.tenant), "")
            self.assertNotIn("## On hold", render_managed_region(self.tenant))
        release_hold(self.tenant, row.id)
        self.hold("tax bill", tenant_today(self.tenant))
        self.assertNotIn("## On hold", render_managed_region(self.tenant))
        self.assertEqual(AttentionHold.objects.get(topic="tax bill").release_reason, "expired")
        future = tenant_today(self.tenant) + timedelta(days=4)
        self.hold("swimming", future)
        self.assertIn(future.isoformat(), render_attention_holds(self.tenant))
        sections = list(all_sections())
        names = [s.key for s in sections]
        self.assertEqual(names.index("on_hold"), names.index("right_now") + 1)
        self.assertIn(AttentionHold, next(s for s in sections if s.key == "on_hold").refresh_on)


@override_settings(ATTENTION_HOLD_TENANT_IDS="*")
class AttentionRuntimeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(user=User.objects.create_user(username="hold-runtime"))
        self.other = Tenant.objects.create(user=User.objects.create_user(username="other-hold-runtime"))
        seed_internal_key(self.tenant, "test-key")
        seed_internal_key(self.other, "other-test-key")
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/attention/"
        self.headers = {"HTTP_X_NBHD_INTERNAL_KEY": "test-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}

    def post(self, action="hold", body=None, **headers):
        return self.client.post(
            self.url + action + "/", body or {"topic": "gym"}, format="json", **(headers or self.headers)
        )

    def test_auth_required_and_tenant_key_cannot_cross_scope(self):
        for action in ("hold", "release"):
            self.assertEqual(self.client.post(self.url + action + "/", {}, format="json").status_code, 401)
            self.assertEqual(
                self.post(
                    action, HTTP_X_NBHD_INTERNAL_KEY="other-test-key", HTTP_X_NBHD_TENANT_ID=str(self.tenant.id)
                ).status_code,
                401,
            )
            self.assertEqual(
                self.post(
                    action, HTTP_X_NBHD_INTERNAL_KEY="test-key", HTTP_X_NBHD_TENANT_ID=str(self.other.id)
                ).status_code,
                401,
            )
        self.assertFalse(AttentionHold.objects.exists())

    def test_cross_tenant_release_refused(self):
        with suppress_refresh():
            row = hold_topic(self.other, "other topic")
        response = self.post("release", {"hold_id": row.id})
        self.assertEqual(response.status_code, 404)
        row.refresh_from_db()
        self.assertIsNone(row.released_at)

    def test_past_local_date_refused(self):
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])
        with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 31, 15, 1, tzinfo=UTC)):
            response = self.post(body={"topic": "tax bill", "resume_on": "2026-10-31"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("past", response.data["detail"])
        self.assertFalse(AttentionHold.objects.exists())

    @override_settings(ATTENTION_HOLD_TENANT_IDS="")
    def test_unflagged_refused_for_both_tools(self):
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.post("release", {"hold_id": 1}).status_code, 403)
        self.assertFalse(AttentionHold.objects.exists())

    @patch("apps.orchestrator.workspace_envelope.push_user_md")
    def test_both_views_push_once_after_commit_and_discussion_does_not_release(self, push):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            saved = self.post(body={"topic": "[PERSON_1]'s tax bill"})
            self.assertEqual(saved.status_code, 200)
            push.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        push.assert_called_once_with(str(self.tenant.id), debounce_seconds=0, trigger="attention_hold")
        row_id = saved.data["hold_id"]
        push.reset_mock()
        # Reading the topic, including in a new instance/chat, never releases it.
        self.assertEqual(len(active_holds(Tenant.objects.get(id=self.tenant.id))), 1)
        self.assertIsNone(AttentionHold.objects.get(id=row_id).released_at)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post("release", {"hold_id": row_id})
        self.assertTrue(response.data["released"])
        self.assertEqual(AttentionHold.objects.get(id=row_id).release_reason, "user")
        push.assert_called_once_with(str(self.tenant.id), debounce_seconds=0, trigger="attention_hold")

    def test_bad_dates_and_ids_refused(self):
        for value in ("yesterday", "2026-13-42", 17, ""):
            self.assertEqual(self.post(body={"topic": "gym", "resume_on": value}).status_code, 400)
        for value in (True, "1", None, 0):
            self.assertEqual(self.post("release", {"hold_id": value}).status_code, 400)
