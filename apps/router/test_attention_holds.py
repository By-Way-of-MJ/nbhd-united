"""Proactive delivery holds: structured check, exemptions and unconsumed claims."""

import json
from datetime import timedelta
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.common.tenant_tz import tenant_today
from apps.cron.models import CronJob, CronJobSource
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator.envelope_registry import suppress_refresh
from apps.router.cron_delivery import _rate_counts
from apps.router.models import DeliveryAttempt, DeviceToken, ProactiveOutbound
from apps.tenants.attention import hold_topic, release_hold
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key


def answer(hold_id=None):
    return {
        "choices": [{"message": {"content": json.dumps({"raises_hold": hold_id is not None, "hold_id": hold_id})}}]
    }, "test-model"


@override_settings(ATTENTION_HOLD_TENANT_IDS="*", NBHD_DELIVERY_DEDUP_TENANTS="", HEARTBEAT_NUDGE_DEDUP_TENANT_IDS="*")
class AttentionDeliveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="hold-delivery", timezone="Asia/Tokyo")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        seed_internal_key(self.tenant, "test-key")
        DeviceToken.objects.create(tenant=self.tenant, user=self.user, token="i" * 64)
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        self.headers = {"HTTP_X_NBHD_INTERNAL_KEY": "test-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}
        self.enterContext(patch("apps.router.proactive_context._dispatch_ios_push"))
        self.check = self.enterContext(patch("apps.router.attention_holds.chat_completion", return_value=answer()))
        with suppress_refresh():
            self.hold = hold_topic(self.tenant, "[PERSON_1]'s tax bill")
        _rate_counts.clear()

    def post(self, message="How is the tax bill?", job="hourly-heartbeat", **headers):
        return self.client.post(
            self.url, {"message": message}, format="json", HTTP_X_NBHD_JOB_NAME=job, **self.headers, **headers
        )

    def cron(self, source, name="custom-reminder", gateway_job_id="reminder-1"):
        with suppress_cronjob_reconcile():
            return CronJob.objects.create(tenant=self.tenant, name=name, source=source, gateway_job_id=gateway_job_id)

    def test_block_writes_no_outbound_and_instruction_is_plain_and_hold_stays(self):
        self.check.return_value = answer(self.hold.id)
        with self.assertLogs("apps.router.attention_holds", level="INFO") as logs:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["delivered"])
        self.assertEqual(response.data["skipped"], "on_hold")
        self.assertIn("Drop that part and send again", response.data["detail"])
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=self.tenant).exists())
        self.assertFalse(DeliveryAttempt.objects.filter(tenant=self.tenant).exists())
        self.hold.refresh_from_db()
        self.assertIsNone(self.hold.released_at)
        self.assertIn(f"hold_block tenant={str(self.tenant.id)[:8]}", logs.output[0])
        self.assertIn(f"hold={self.hold.id}", logs.output[0])
        self.assertIn("job=hourly-heartbeat", logs.output[0])
        self.assertNotIn("tax bill", " ".join(logs.output))
        self.assertNotIn("How is", " ".join(logs.output))

    def test_pass_delivers_and_uses_structured_short_single_attempt_in_placeholder_space(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        response = self.post("Enjoy the sunshine, [PERSON_1].")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "sent")
        self.assertEqual(
            ProactiveOutbound.objects.get(tenant=self.tenant).message_text, "Enjoy the sunshine, [PERSON_1]."
        )
        self.check.assert_called_once()
        args, kwargs = self.check.call_args
        self.assertIsInstance(args[0], str)  # One model, no fallback/retry list.
        self.assertNotIn("Alice", json.dumps(args[1]))
        self.assertIn("[PERSON_1]", json.dumps(args[1]))
        self.assertEqual(kwargs["timeout"], 6)
        self.assertFalse(kwargs["record_health"])
        self.assertEqual(kwargs["reasoning"], {"enabled": False})
        self.assertEqual(kwargs["max_tokens"], 256)
        self.assertEqual(kwargs["response_format"]["type"], "json_schema")
        self.assertEqual(kwargs["response_format"]["json_schema"]["schema"]["required"], ["raises_hold", "hold_id"])

    def test_timeout_unavailability_malformed_output_unknown_hold_all_deliver(self):
        for result in [
            requests.Timeout("PRIVATE provider error"),
            RuntimeError("PRIVATE key absent"),
            ValueError("PRIVATE parse"),
            ({"choices": [{"message": {"content": "not json"}}]}, "model"),
            answer(999999),
        ]:
            with self.subTest(result=type(result).__name__):
                self.check.reset_mock()
                self.check.side_effect = result if isinstance(result, Exception) else None
                if not isinstance(result, Exception):
                    self.check.return_value = result
                with self.assertLogs("apps.router.attention_holds", level="WARNING") as logs:
                    response = self.post(message="Hello")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["status"], "sent")
                self.check.assert_called_once()
                self.assertIn("hold_check_failed tenant=", logs.output[0])
                self.assertNotIn("PRIVATE", " ".join(logs.output))
                self.assertNotIn("tax bill", " ".join(logs.output))
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=self.tenant).count(), 5)

    def test_no_active_holds_make_no_model_call(self):
        with suppress_refresh():
            release_hold(self.tenant, self.hold.id)
        self.assertEqual(self.post().data["status"], "sent")
        self.check.assert_not_called()

    def test_expired_holds_make_no_model_call(self):
        self.tenant.attention_holds.update(resume_on=tenant_today(self.tenant) - timedelta(days=1))
        self.assertEqual(self.post().data["status"], "sent")
        self.check.assert_not_called()
        self.hold.refresh_from_db()
        self.assertEqual(self.hold.release_reason, "expired")

    @override_settings(ATTENTION_HOLD_TENANT_IDS="")
    def test_unflagged_never_even_reads_holds(self):
        with patch("apps.router.attention_holds.active_holds") as reads:
            self.assertEqual(self.post().data["status"], "sent")
        reads.assert_not_called()
        self.check.assert_not_called()

    def test_subagent_result_is_a_requested_answer_and_skips(self):
        self.assertEqual(self.post(job="_subagent_result").data["status"], "sent")
        self.check.assert_not_called()

    def test_user_made_reminder_skips_by_runtime_id_even_with_system_job_name(self):
        self.cron(CronJobSource.USER)
        self.assertEqual(self.post(job="hourly-heartbeat", HTTP_X_NBHD_CRON_JOB_ID="reminder-1").data["status"], "sent")
        self.check.assert_not_called()

    def test_user_made_reminder_skips_by_name_with_no_gateway_mapping(self):
        self.cron(CronJobSource.USER)
        self.assertEqual(
            self.post(job="custom-reminder", HTTP_X_NBHD_CRON_JOB_ID="not-yet-mapped").data["status"], "sent"
        )
        self.check.assert_not_called()

    def test_system_job_runtime_id_overrides_model_supplied_user_job_name(self):
        self.cron(CronJobSource.USER)
        self.cron(CronJobSource.SYSTEM, "Morning Briefing", "system-1")
        self.check.return_value = answer(self.hold.id)
        self.assertEqual(
            self.post(job="custom-reminder", HTTP_X_NBHD_CRON_JOB_ID="system-1").data["skipped"], "on_hold"
        )
        self.check.assert_called_once()

    def test_default_and_blank_job_names_are_checked(self):
        for job in ("", "Morning Briefing", "hourly-heartbeat"):
            self.check.reset_mock()
            self.check.return_value = answer(self.hold.id)
            self.assertEqual(self.post(job=job).data["skipped"], "on_hold")
            self.check.assert_called_once()

    def test_block_leaves_about_nudge_claim_free_for_rewritten_send(self):
        marker = "[about: reminder:9e8d7c6b-2222-4b3c-9d4e-abcdef123456]\n"
        self.check.return_value = answer(self.hold.id)
        first = self.post(marker + "How is [PERSON_1]'s tax bill?")
        self.assertEqual(first.data["skipped"], "on_hold")
        self.assertFalse(DeliveryAttempt.objects.filter(tenant=self.tenant).exists())
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=self.tenant).exists())
        self.check.return_value = answer()
        rewritten = self.post(marker + "A quiet moment outside might be nice.")
        self.assertEqual(rewritten.data["status"], "sent")
        attempt = DeliveryAttempt.objects.get(tenant=self.tenant)
        self.assertEqual(attempt.state, DeliveryAttempt.State.SENT)
        self.assertEqual(
            ProactiveOutbound.objects.get(tenant=self.tenant).message_text, "A quiet moment outside might be nice."
        )
        duplicate = self.post(marker + "A quiet moment outside might be nice.")
        self.assertEqual(duplicate.data["status"], "duplicate_suppressed")
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=self.tenant).count(), 1)
        self.assertNotIn("[about:", self.check.call_args.args[1][1]["content"])

    @override_settings(NBHD_DELIVERY_DEDUP_TENANTS="*")
    def test_block_also_leaves_hourly_delivery_claim_free(self):
        self.check.return_value = answer(self.hold.id)
        self.assertEqual(self.post().data["skipped"], "on_hold")
        self.assertFalse(DeliveryAttempt.objects.filter(tenant=self.tenant).exists())
        self.check.return_value = answer()
        self.assertEqual(self.post("Hello").data["status"], "sent")
        self.assertEqual(DeliveryAttempt.objects.get(tenant=self.tenant).state, "sent")
