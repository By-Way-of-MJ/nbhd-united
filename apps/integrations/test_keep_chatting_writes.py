"""Controlled interleavings for mutation paths shared by the two chat lanes."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.integrations import runtime_views
from apps.journal.models import Goal
from apps.tenants.models import UserSituation
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class ConcurrentRuntimeWritesTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Concurrent writes", telegram_chat_id=198231)
        seed_internal_key(self.tenant)
        self.headers = {"HTTP_X_NBHD_INTERNAL_KEY": "shared-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}
        self.enterContext(patch("apps.cron.publish.publish_task"))
        self.enterContext(patch("apps.orchestrator.services.update_tenant_config"))

    def test_goal_patch_preserves_the_other_lanes_untouched_text_and_receipt(self):
        goal = Goal.objects.create(tenant=self.tenant, title="old", description="before")
        original = runtime_views._author_runtime_lifecycle_input

        def interleave(*args, **kwargs):
            Goal.objects.filter(pk=goal.pk).update(
                description="other lane", pii_receipts={"description": {"state": "unconfirmed", "reason": "other-lane"}}
            )
            return original(*args, **kwargs)

        with patch("apps.integrations.runtime_views._author_runtime_lifecycle_input", side_effect=interleave):
            response = self.client.patch(
                f"/api/v1/integrations/runtime/{self.tenant.id}/goals/{goal.id}/",
                {"title": "new"},
                content_type="application/json",
                **self.headers,
            )
        self.assertEqual(response.status_code, 200, response.content)
        goal.refresh_from_db()
        self.assertEqual(goal.title, "new")
        self.assertEqual(goal.description, "other lane")
        self.assertEqual(goal.pii_receipts["description"]["reason"], "other-lane")

    def test_goal_patch_does_not_resurrect_a_row_deleted_during_authoring(self):
        goal = Goal.objects.create(tenant=self.tenant, title="old")
        original = runtime_views._author_runtime_lifecycle_input

        def interleave(*args, **kwargs):
            Goal.objects.filter(pk=goal.pk).delete()
            return original(*args, **kwargs)

        with patch("apps.integrations.runtime_views._author_runtime_lifecycle_input", side_effect=interleave):
            response = self.client.patch(
                f"/api/v1/integrations/runtime/{self.tenant.id}/goals/{goal.id}/",
                {"title": "new"},
                content_type="application/json",
                **self.headers,
            )
        self.assertEqual(response.status_code, 404, response.content)
        self.assertFalse(Goal.objects.filter(pk=goal.pk).exists())

    def test_timezone_dismissal_cannot_dismiss_a_new_phone_observation(self):
        situation = UserSituation.objects.create(tenant=self.tenant, device_tz="America/New_York")
        from django.db.models.query import QuerySet

        original = QuerySet.update

        def interleave(query, **kwargs):
            if query.model is UserSituation and "tz_offer_dismissed_for" in kwargs:
                original(UserSituation.objects.filter(pk=situation.pk), device_tz="Europe/London")
            return original(query, **kwargs)

        with patch.object(QuerySet, "update", interleave):
            response = self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/situation/tz-offer-dismiss/",
                content_type="application/json",
                **self.headers,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["dismissed_for"], "")
        situation.refresh_from_db()
        self.assertEqual(situation.device_tz, "Europe/London")
        self.assertEqual(situation.tz_offer_dismissed_for, "")
