"""Canonical reminder ownership, cancellation and publication contracts."""

from unittest.mock import patch

from django.test import TestCase

from apps.actions.models import ActionAuditLog, ActionType, CronDispatch, PendingAction
from apps.cron.gateway_client import GatewayError
from apps.cron.models import CronCreationPath, CronJob, CronJobSource, CronPattern
from apps.cron.services import TypedCronError, cancel_user_cron, list_user_crons
from apps.cron.share_cron_sync import _desired_jobs
from apps.tenants.services import create_tenant


class CancelUserCronTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Cancel", telegram_chat_id=918181)
        self.tenant.openclaw_version = "2026.9.4"
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["openclaw_version", "postgres_cron_canonical"])
        self.writer = self.enterContext(patch("apps.cron.share_cron_sync.write_tenant_crons_file"))
        self.gateway = self.enterContext(patch("apps.cron.gateway_client.invoke_gateway_tool"))
        self.enqueue = self.enterContext(patch("apps.cron.signals._enqueue_regen", return_value=True))

    def cron(self, **kwargs):
        defaults = {
            "tenant": self.tenant,
            "name": "Japanese class",
            "source": CronJobSource.USER,
            "data": {
                "schedule": {"kind": "cron", "expr": "30 19 * * 3", "tz": "Asia/Tokyo"},
                "payload": {"kind": "agentTurn", "message": "Class time"},
            },
        }
        return CronJob.objects.create(**(defaults | kwargs))

    def test_file_sync_cancellation_removes_desired_job_and_is_idempotent(self):
        cron = self.cron()
        self.assertIn(f"nbhd:{cron.pk}", [job["declarationKey"] for job in _desired_jobs(self.tenant)])
        result = cancel_user_cron(self.tenant, cron.pk)
        self.assertEqual(result["schedule"], "Every Wednesday at 19:30 (Asia/Tokyo)")
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["already_cancelled"])
        cron.refresh_from_db()
        self.assertFalse(cron.enabled)
        self.assertNotIn(f"nbhd:{cron.pk}", [job["declarationKey"] for job in _desired_jobs(self.tenant)])
        self.assertTrue(cancel_user_cron(self.tenant, cron.pk)["already_cancelled"])
        self.assertEqual(self.writer.call_count, 2)
        self.gateway.assert_not_called()

    def test_list_filters_ownership_and_disabled_and_includes_agent_one_shots(self):
        user = self.cron()
        agent = self.cron(
            name="Agent reminder",
            source=CronJobSource.AGENT,
            managed=False,
            data={"schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"}},
        )
        disabled = self.cron(name="Disabled", enabled=False)
        self.cron(name="System", source=CronJobSource.SYSTEM)
        self.cron(name="_sync:internal", source=CronJobSource.AGENT)
        other = create_tenant(display_name="Other", telegram_chat_id=928282)
        self.cron(tenant=other, name="Other tenant")
        self.assertEqual({r["id"] for r in list_user_crons(self.tenant)}, {user.pk, agent.pk})
        self.assertEqual(
            {r["id"] for r in list_user_crons(self.tenant, include_disabled=True)}, {user.pk, agent.pk, disabled.pk}
        )
        self.assertTrue(cancel_user_cron(self.tenant, agent.pk)["cancelled"])

    def test_system_and_internal_rows_refused_even_when_misclassified(self):
        cases = [
            {"name": "Any system row", "source": CronJobSource.SYSTEM},
            {"name": "Any fuel row", "source": CronJobSource.FUEL_SESSION},
            *(
                {"name": name}
                for name in [
                    "Morning Briefing",
                    "Evening Check-in",
                    "Heartbeat Check-in",
                    "Weekly Reflection",
                    "Background Tasks",
                    "Task Hygiene",
                    "_fuel:12345678",
                    "_sync:foo",
                    "heartbeat-main",
                    "skill-collection-review-main",
                    "_core:welcome",
                ]
            ),
            {"name": "Internal", "creation_path": CronCreationPath.INTERNAL},
            {"name": "Renamed system", "pattern": CronPattern.TASK_HYGIENE},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                cron = self.cron(**fields)
                with self.assertRaises(CronJob.DoesNotExist):
                    cancel_user_cron(self.tenant, cron.pk)
                cron.refresh_from_db()
                self.assertTrue(cron.enabled)
        self.assertEqual(list_user_crons(self.tenant), [])
        self.writer.assert_not_called()
        self.gateway.assert_not_called()

    def test_other_tenant_and_missing_id_are_not_found(self):
        other = create_tenant(display_name="Other", telegram_chat_id=928282)
        cron = self.cron(tenant=other)
        for pk in [cron.pk, cron.pk + 999]:
            with self.assertRaises(CronJob.DoesNotExist):
                cancel_user_cron(self.tenant, pk)
        self.writer.assert_not_called()

    def test_fenced_cancellation_does_not_mutate_or_publish(self):
        cron = self.cron()
        self.tenant.openclaw_migration_cron_fenced = True
        with self.assertRaises(TypedCronError) as caught:
            cancel_user_cron(self.tenant, cron.pk)
        self.assertEqual(caught.exception.code, "assistant_updating")
        cron.refresh_from_db()
        self.assertTrue(cron.enabled)
        self.writer.assert_not_called()

    def test_failed_file_publication_retries_disabled_row(self):
        cron = self.cron()
        self.writer.side_effect = RuntimeError("file unavailable")
        with self.assertRaises(GatewayError):
            cancel_user_cron(self.tenant, cron.pk)
        cron.refresh_from_db()
        self.assertFalse(cron.enabled)
        self.writer.side_effect = None
        self.assertTrue(cancel_user_cron(self.tenant, cron.pk)["already_cancelled"])
        self.assertEqual(self.writer.call_count, 2)

    def test_pre94_canonical_uses_existing_reconcile_queue(self):
        self.tenant.openclaw_version = "2026.5.28"
        cron = self.cron()
        self.enqueue.reset_mock()
        cancel_user_cron(self.tenant, cron.pk)
        self.enqueue.assert_called_once_with(str(self.tenant.id))
        self.writer.assert_not_called()
        self.gateway.assert_not_called()
        self.enqueue.return_value = False
        with self.assertRaises(GatewayError):
            cancel_user_cron(self.tenant, cron.pk)

    def test_pre94_one_shot_and_legacy_use_gateway_removal(self):
        self.tenant.openclaw_version = "2026.5.28"
        cron = self.cron(
            managed=False, gateway_job_id="gateway-one", data={"schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"}}
        )
        cancel_user_cron(self.tenant, cron.pk)
        self.gateway.assert_called_with(self.tenant, "cron.remove", {"jobId": "gateway-one"}, error_log_level=40)
        self.tenant.postgres_cron_canonical = False
        recurring = self.cron(name="Legacy", gateway_job_id="gateway-two")
        cancel_user_cron(self.tenant, recurring.pk)
        self.gateway.assert_called_with(self.tenant, "cron.remove", {"jobId": "gateway-two"}, error_log_level=40)
        self.writer.assert_not_called()

    def test_pre94_missing_gateway_id_resolves_name(self):
        self.tenant.openclaw_version = "2026.5.28"
        self.tenant.postgres_cron_canonical = False
        cron = self.cron()
        self.gateway.side_effect = [{"details": {"jobs": [{"id": "resolved", "name": cron.name}]}}, {}]
        cancel_user_cron(self.tenant, cron.pk)
        self.gateway.assert_called_with(self.tenant, "cron.remove", {"jobId": "resolved"}, error_log_level=40)

    def test_cancellation_telemetry_retains_only_metadata(self):
        from apps.actions.origin import OriginStamp
        from apps.platform_logs.models import ToolContractEvent

        cron = self.cron()
        cancel_user_cron(self.tenant, cron.pk, origin_stamp=OriginStamp(kind="cron", run_id="run-123"))
        event = ToolContractEvent.objects.get(tenant_id=self.tenant.id, tool_name="cron-cancel-reminder")
        self.assertEqual(event.outcome, "accepted")
        self.assertEqual(
            event.detail,
            {"cron_id": cron.pk, "already_cancelled": False, "origin_kind": "cron", "origin_run_id": "run-123"},
        )

    def test_gated_create_gets_one_cancellation_audit(self):
        cron = self.cron()
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CRON_CREATE,
            action_payload={"name": cron.name},
            display_summary="Create reminder",
        )
        CronDispatch.objects.create(action=action, cron=cron, kind="cron", state="executed")
        cancel_user_cron(self.tenant, cron.pk)
        cancel_user_cron(self.tenant, cron.pk)
        self.assertEqual(ActionAuditLog.objects.filter(tenant=self.tenant, result="cancelled").count(), 1)
