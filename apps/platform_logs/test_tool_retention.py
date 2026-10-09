"""The daily purge must only remove expired diagnostic tool events."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.platform_logs.models import PlatformIssueLog, ToolContractEvent
from apps.platform_logs.tasks import purge_tool_events_task
from apps.router.models import AppChatMessage, ChatThread
from apps.tenants.models import Tenant, User


class ScheduledToolRetentionTests(TestCase):
    def test_strict_cutoff_preserves_conversation_and_other_platform_data(self):
        now = timezone.now()
        cutoff = now - timedelta(days=90)
        user = User.objects.create_user(username="retention-owner", password=None)
        tenant = Tenant.objects.create(user=user)
        thread = ChatThread.objects.create(tenant=tenant, user=user)
        message = AppChatMessage.objects.create(
            tenant=tenant,
            user=user,
            thread=thread,
            client_msg_id="retention-fixture",
            user_text="Test conversation",
            reply_text="Test reply",
        )
        issue = PlatformIssueLog.objects.create(tenant=tenant, summary="Test diagnostic issue")
        for row in (message, issue):
            type(row).objects.filter(pk=row.pk).update(created_at=cutoff - timedelta(days=30))
        protected_models = (User, Tenant, ChatThread, AppChatMessage, PlatformIssueLog)
        before = {model: list(model.objects.values()) for model in protected_models}

        events = []
        for timestamp in (cutoff - timedelta(microseconds=1), cutoff, cutoff + timedelta(microseconds=1)):
            # Orphan tenant IDs are intentional: telemetry has no tenant FK.
            event = ToolContractEvent.objects.create(
                tenant_id=uuid.uuid4(), tool_name="runtime-test", outcome=ToolContractEvent.Outcome.ACCEPTED
            )
            ToolContractEvent.objects.filter(pk=event.pk).update(created_at=timestamp)
            events.append(event)

        with patch("apps.platform_logs.management.commands.purge_tool_events.timezone.now", return_value=now):
            purge_tool_events_task()

        self.assertSetEqual(set(ToolContractEvent.objects.values_list("id", flat=True)), {e.id for e in events[1:]})
        for model in protected_models:
            with self.subTest(model=model.__name__):
                self.assertEqual(list(model.objects.values()), before[model])
