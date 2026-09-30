"""Runtime reminder reads and cancellation use create-tool auth boundaries."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.cron.models import CronJob, CronJobSource
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeCronReminderTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Reminders", telegram_chat_id=838383)
        seed_internal_key(self.tenant)
        self.tenant.openclaw_version = "2026.9.4"
        self.tenant.save(update_fields=["openclaw_version"])
        self.writer = self.enterContext(patch("apps.cron.share_cron_sync.write_tenant_crons_file"))
        self.base = f"/api/v1/integrations/runtime/{self.tenant.id}/crons/"
        self.headers = {"HTTP_X_NBHD_INTERNAL_KEY": "shared-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}
        self.cron = CronJob.objects.create(
            tenant=self.tenant,
            name="Japanese class",
            source=CronJobSource.USER,
            data={"schedule": {"kind": "cron", "expr": "30 19 * * 3", "tz": "Asia/Tokyo"}},
        )

    def cancel(self, data, **headers):
        return self.client.post(self.base + "cancel/", data, content_type="application/json", **headers)

    def test_auth_required_for_both_endpoints_and_tenant_scope(self):
        other = create_tenant(display_name="Other", telegram_chat_id=848484)
        for headers in [
            {},
            self.headers | {"HTTP_X_NBHD_INTERNAL_KEY": "bad"},
            self.headers | {"HTTP_X_NBHD_TENANT_ID": str(other.id)},
        ]:
            self.assertEqual(self.client.get(self.base + "reminders/", **headers).status_code, 401)
            self.assertEqual(self.cancel({"cron_id": self.cron.pk}, **headers).status_code, 401)
        self.writer.assert_not_called()

    def test_list_cancel_and_disabled_listing(self):
        response = self.client.get(self.base + "reminders/", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reminders"][0]["id"], self.cron.pk)
        result = self.cancel({"cron_id": self.cron.pk}, **self.headers)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertTrue(result.json()["cancelled"])
        self.assertEqual(result.json()["name"], "Japanese class")
        self.assertEqual(result.json()["schedule"], "Every Wednesday at 19:30 (Asia/Tokyo)")
        self.assertEqual(self.client.get(self.base + "reminders/", **self.headers).json()["reminders"], [])
        disabled = self.client.get(self.base + "reminders/?include_disabled=true", **self.headers).json()["reminders"]
        self.assertFalse(disabled[0]["enabled"])
        self.assertTrue(self.cancel({"cron_id": self.cron.pk}, **self.headers).json()["already_cancelled"])

    def test_system_foreign_and_missing_rows_have_same_404(self):
        system = CronJob.objects.create(tenant=self.tenant, name="Morning Briefing", source=CronJobSource.SYSTEM)
        other = create_tenant(display_name="Other", telegram_chat_id=848484)
        foreign = CronJob.objects.create(tenant=other, name="Foreign")
        for pk in [system.pk, foreign.pk, foreign.pk + 999]:
            response = self.cancel({"cron_id": pk}, **self.headers)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"error": "reminder_not_found"})
        self.writer.assert_not_called()

    def test_invalid_ids_return_400(self):
        for invalid in [None, True, "1", 1.5, 0, -1]:
            self.assertEqual(self.cancel({"cron_id": invalid}, **self.headers).status_code, 400)

    def test_fence_returns_409_without_mutation(self):
        self.tenant.openclaw_migration_cron_fenced = True
        self.tenant.save(update_fields=["openclaw_migration_cron_fenced"])
        response = self.cancel({"cron_id": self.cron.pk}, **self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "assistant_updating")
        self.cron.refresh_from_db()
        self.assertTrue(self.cron.enabled)
        self.writer.assert_not_called()

    def test_propagation_failure_is_not_success(self):
        self.writer.side_effect = RuntimeError("unavailable")
        response = self.cancel({"cron_id": self.cron.pk}, **self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("cancelled", response.json())

    def test_cancel_verifies_origin(self):
        with patch("apps.actions.origin.verify_origin_stamp") as verify:
            self.assertEqual(
                self.cancel({"cron_id": self.cron.pk, "origin": {"v": 1}}, **self.headers).status_code, 200
            )
        self.assertEqual(verify.call_args.args[0].pk, self.tenant.pk)
        self.assertEqual(verify.call_args.args[1], {"v": 1})
