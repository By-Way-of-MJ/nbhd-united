"""A completed model turn with a failed relay must not be called delivered."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.orchestrator.hibernation import deliver_buffered_messages_task
from apps.router.models import BufferedMessage
from apps.router.pending_queue import DeliveryState, SendResult
from apps.router.test_pending_queue import _make_tenant, _make_user, _ok_chat_response


@override_settings(TELEGRAM_INBOUND_ENABLED=True)
class BufferedRelayFailureTests(TestCase):
    def test_failed_relay_terminalizes_without_repeating_model(self):
        tenant = _make_tenant(_make_user(telegram_chat_id=12345))
        row = BufferedMessage.objects.create(tenant=tenant, channel="telegram", user_text="hello", payload={})
        with (
            patch("httpx.post", return_value=_ok_chat_response("reply")) as model,
            patch(
                "apps.router.pending_queue.relay_ai_response_to_telegram",
                return_value=SendResult(DeliveryState.FAILED, detail="relay_failed"),
            ),
            patch("apps.router.services.send_telegram_message") as notice,
        ):
            result = deliver_buffered_messages_task(str(tenant.id))
            deliver_buffered_messages_task(str(tenant.id))
        self.assertEqual(result["delivered"], 0)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, BufferedMessage.Status.FAILED)
        model.assert_called_once()
        notice.assert_called_once()
