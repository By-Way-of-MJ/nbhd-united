"""Paired inbound-availability contracts; enabled behavior is the baseline."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core import mail
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.actions import messaging
from apps.actions.models import ActionStatus, ActionType, PendingAction
from apps.friends import notifications as friends
from apps.lessons import notifications as lessons
from apps.router.cron_delivery import resolve_user_channel
from apps.router.line_quota_handlers import handle_exhausted, handle_pre_warn, handle_recovered
from apps.router.models import DeviceToken, LineQuotaState
from apps.router.pending_queue import DeliveryState, relay_ai_response_to_telegram
from apps.tenants import telegram_service
from apps.tenants.emails import send_welcome_email
from apps.tenants.models import Tenant, User
from apps.tenants.serializers import TenantSerializer
from apps.tenants.telegram_models import TelegramLinkToken


class AvailabilityContracts:
    enabled = True

    def setUp(self):
        super().setUp()
        self.enterContext(
            override_settings(
                TELEGRAM_INBOUND_ENABLED=self.enabled,
                TELEGRAM_BOT_TOKEN="test-token",
                LINE_CHANNEL_ACCESS_TOKEN="test-line",
                TELEGRAM_WEBHOOK_SECRET="test-secret",
                EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
            )
        )
        cache.clear()
        self.user = User.objects.create_user(username="availability", email="availability@example.test")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def link(self, line=False):
        self.user.telegram_user_id = 12345
        self.user.telegram_chat_id = 12345
        self.user.line_user_id = "line-test" if line else None
        self.user.save()

    def device(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token="a" * 64)

    def test_serializer_and_identity_endpoints(self):
        self.assertTrue(TenantSerializer().fields["telegram_available"].read_only)
        self.assertIs(TenantSerializer(self.tenant).data["telegram_available"], self.enabled)
        for path in ("/api/v1/auth/me/", "/api/v1/tenants/me/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            data = response.data.get("tenant", response.data)
            self.assertIs(data["telegram_available"], self.enabled)
            self.assertIs(self.client.get(path).data.get("tenant", data)["telegram_available"], self.enabled)

    def test_generate_link_and_unlinked_status(self):
        response = self.client.post("/api/v1/tenants/telegram/generate-link/")
        self.assertEqual(response.status_code, 200 if self.enabled else 409)
        self.assertEqual(TelegramLinkToken.objects.count(), int(self.enabled))
        status = self.client.get("/api/v1/tenants/telegram/status/")
        self.assertEqual(status.status_code, 200 if self.enabled else 409)
        if self.enabled:
            self.assertFalse(status.data["linked"])
            self.assertIn("t.me/", response.data["deep_link"])
        else:
            self.assertEqual(response.data, status.data)
            self.assertEqual(response.data["code"], "telegram_unavailable")
            self.assertTrue(response.data["detail"])

    def test_linked_status_and_unlink(self):
        self.link()
        self.assertTrue(self.client.get("/api/v1/tenants/telegram/status/").data["linked"])
        self.assertEqual(self.client.post("/api/v1/tenants/telegram/unlink/").status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_user_id)

    def start_update(self):
        token = telegram_service.generate_link_token(self.user)
        return token, {
            "update_id": 9901,
            "message": {"text": f"/start {token.token}", "chat": {"id": 12345}, "from": {"id": 12345}},
        }

    def assert_consumed(self, token):
        token.refresh_from_db()
        self.user.refresh_from_db()
        self.assertIs(token.used, self.enabled)
        self.assertEqual(self.user.telegram_chat_id, 12345 if self.enabled else None)

    def test_start_service(self):
        token, _ = self.start_update()
        success, detail = telegram_service.process_start_token(12345, 12345, "", "", token.token)
        self.assertIs(success, self.enabled)
        if not self.enabled:
            self.assertIn("unavailable", detail.lower())
        self.assert_consumed(token)

    def test_poller_start(self):
        from apps.router.poller import TelegramPoller

        token, update = self.start_update()
        poller = TelegramPoller.__new__(TelegramPoller)
        with patch.object(poller, "_execute_telegram_response") as send:
            poller._handle_update(update)
        send.assert_called_once()
        self.assert_consumed(token)

    def test_webhook_start(self):
        token, update = self.start_update()
        response = self.client.post(
            reverse("telegram-webhook"), update, format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-secret"
        )
        self.assertEqual(response.status_code, 200)
        self.assert_consumed(token)

    def test_resolver_device_first(self):
        self.link(line=True)
        self.device()
        self.assertEqual(resolve_user_channel(self.user), "app")

    def test_resolver_line_fallback(self):
        self.link(line=True)
        self.assertEqual(resolve_user_channel(self.user), "telegram" if self.enabled else "line")

    def test_resolver_app_feed_fallback(self):
        self.link()
        self.assertEqual(resolve_user_channel(self.user), "telegram" if self.enabled else "app")

    def test_gate_app_fallback_and_explicit_origin(self):
        self.link()
        self.device()
        telegram = Mock(return_value=messaging.GateSendResult(True, "123"))
        for origin in (None, "telegram"):
            action = PendingAction.objects.create(
                tenant=self.tenant,
                action_type=ActionType.CALENDAR_CREATE,
                action_payload={},
                display_summary="Create event",
            )
            with patch.dict(messaging._SENDERS, {"telegram": (telegram, None)}):
                self.assertTrue(messaging.send_gate_confirmation(self.tenant, action, originating_channel=origin))
            action.refresh_from_db()
            self.assertEqual(action.status, ActionStatus.PENDING)
            self.assertEqual(action.platform_channel, "telegram" if self.enabled else "app")
        self.assertEqual(telegram.call_count, 2 if self.enabled else 0)

    def test_unsupported_gate_remains_unresolved(self):
        self.link()
        # With and without an app surface, gmail cannot become an app approval.
        for has_device in (False, True):
            if has_device:
                self.device()
            action = PendingAction.objects.create(
                tenant=self.tenant, action_type=ActionType.GMAIL_TRASH, action_payload={}, display_summary="Trash mail"
            )
            sender = Mock(return_value=messaging.GateSendResult(True, "123"))
            with patch.dict(messaging._SENDERS, {"telegram": (sender, None)}):
                self.assertIs(messaging.send_gate_confirmation(self.tenant, action), self.enabled)
            action.refresh_from_db()
            self.assertEqual(action.status, ActionStatus.PENDING)
            self.assertIsNone(action.responded_at)
            self.assertEqual(sender.call_count, int(self.enabled))

    def test_gate_line_unchanged(self):
        self.link(line=True)
        self.assertEqual(messaging._resolve_gate_channel(self.user, originating_channel="line"), "line")
        self.assertEqual(messaging._resolve_gate_channel(self.user), "telegram" if self.enabled else "line")

    def test_lesson_preferred_and_fallback(self):
        self.link(line=True)
        for preferred in ("telegram", "line"):
            self.user.preferred_channel = preferred
            telegram, line = Mock(return_value=True), Mock(return_value=preferred == "telegram")
            with patch.dict(lessons._SENDERS, telegram=telegram, line=line):
                result = lessons.send_lesson_approval_buttons(self.tenant, Mock())
            self.assertEqual(telegram.call_count, int(self.enabled))
            self.assertEqual(result, self.enabled or preferred == "telegram")
            self.assertEqual(line.call_count, int(not self.enabled or preferred == "line"))

    def test_friend_preferred_and_fallback(self):
        self.link(line=True)
        friendship = SimpleNamespace(addressee=self.tenant, status="pending")
        for preferred in ("telegram", "line"):
            self.user.preferred_channel = preferred
            telegram, line = Mock(return_value=True), Mock(return_value=preferred == "telegram")
            with patch.dict(friends._SENDERS, telegram=telegram, line=line):
                result = friends.notify_wave_received(friendship)
            self.assertEqual(telegram.call_count, int(self.enabled))
            self.assertEqual(result, self.enabled or preferred == "telegram")
            self.assertEqual(line.call_count, int(not self.enabled or preferred == "line"))

    def test_queued_reply_with_callbacks_fails_visibly(self):
        with patch("apps.router.pending_queue._send_telegram_html_chunks") as send:
            send.return_value = SimpleNamespace(state=DeliveryState.SENT)
            result = relay_ai_response_to_telegram(self.tenant, 12345, "Choose [[button:Yes|yes]]")
        self.assertEqual(result.state, DeliveryState.SENT if self.enabled else DeliveryState.FAILED)
        self.assertEqual(send.call_count, int(self.enabled))
        if self.enabled:
            self.assertEqual(
                send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "agent:yes"
            )
        else:
            self.assertEqual(result.detail, "telegram_unavailable")

    def test_queue_failure_notices_do_not_invite_dead_replies(self):
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message
        from apps.router.pending_queue import (
            _send_apology_for_dropped_pending_message,
            _send_apology_for_stale_pending_message,
        )

        self.link()
        message = SimpleNamespace(channel="telegram", channel_user_id="12345", user_text="Original message")
        for send_notice, args in (
            (_send_apology_for_dropped_pending_message, (self.tenant, message)),
            (_send_apology_for_stale_pending_message, (self.tenant, message, 3600)),
        ):
            with self.subTest(notice=send_notice.__name__):
                with patch("apps.router.pending_queue.httpx.post") as post:
                    send_notice(*args)
                post.assert_called_once()
                text = post.call_args.kwargs["json"]["text"]
                self.assertEqual("unavailable" in text, not self.enabled)
                if not self.enabled:
                    self.assertIn("NBHD app or LINE", text)
                    self.assertNotIn("send it again", text)
                    self.assertNotIn("please resend", text)
        with patch("apps.router.services.send_telegram_message") as post:
            _send_apology_for_dropped_message(self.tenant, message)
        post.assert_called_once()
        self.assertEqual("unavailable" in post.call_args.args[1], not self.enabled)

    def test_welcome_email(self):
        self.assertTrue(send_welcome_email(self.tenant))
        message = mail.outbox[-1]
        for body in (message.body, message.alternatives[0][0]):
            self.assertEqual("connect Telegram or LINE" in body, self.enabled)
            if not self.enabled:
                self.assertNotIn("Telegram", body)
            self.assertIn("LINE", body)

    def test_quota_exhaustion_linked(self):
        self.link(line=True)
        self.user.preferred_channel = "line"
        self.user.save()
        state = LineQuotaState.get()
        state.line_quota_exhausted_at = timezone.now()
        state.save()
        result = handle_exhausted()
        self.user.refresh_from_db()
        self.assertEqual(result["flipped"], int(self.enabled))
        self.assertEqual(self.user.preferred_channel, "telegram" if self.enabled else "line")
        self.assertEqual(bool(self.user.preferences.get("channel_flipped_by_quota")), self.enabled)
        self.assertEqual("Telegram" in mail.outbox[-1].body, self.enabled)

    def test_quota_exhaustion_unlinked(self):
        self.user.line_user_id = "line-test"
        self.user.preferred_channel = "line"
        self.user.save()
        state = LineQuotaState.get()
        state.line_quota_exhausted_at = timezone.now()
        state.save()
        self.assertEqual(handle_exhausted()["emailed_line_only"], 1)
        self.assertEqual("connect Telegram" in mail.outbox[-1].body, self.enabled)
        self.assertEqual("Telegram" in mail.outbox[-1].subject, self.enabled)

    def test_quota_recovery(self):
        self.user.preferences = {"channel_flipped_by_quota": True}
        self.user.save()
        state = LineQuotaState.get()
        state.line_quota_exhausted_notified_at = timezone.now()
        state.save()
        self.assertEqual(handle_recovered()["emailed"], 1)
        self.assertEqual("Telegram" in mail.outbox[-1].body, self.enabled)
        self.assertEqual("Telegram" in mail.outbox[-1].subject, self.enabled)

    @override_settings(PLATFORM_OWNER_EMAIL="owner@example.test")
    def test_quota_owner_warning(self):
        state = LineQuotaState.get()
        state.line_quota_limit, state.line_quota_used = 100, 95
        state.save()
        self.assertTrue(handle_pre_warn())
        self.assertEqual("connect Telegram" in mail.outbox[-1].body, self.enabled)

    def test_low_level_one_way_transport_still_works(self):
        from apps.router.services import send_telegram_message

        with patch("apps.router.services.httpx.post") as post:
            post.return_value.status_code = 200
            self.assertTrue(send_telegram_message(12345, "One-way notification"))
        post.assert_called_once()

    def test_webhook_ordinary_message_availability(self):
        with patch("apps.router.views.resolve_tenant_by_chat_id", return_value=None) as resolve:
            response = self.client.post(
                reverse("telegram-webhook"),
                {"update_id": 9902, "message": {"text": "Hello", "chat": {"id": 12345}}},
                format="json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-secret",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(resolve.call_count, int(self.enabled))
        if not self.enabled:
            self.assertEqual(response.json()["code"], "telegram_unavailable")

    def test_selecting_telegram_preference(self):
        self.link(line=True)
        self.user.preferred_channel = "line"
        self.user.save()
        response = self.client.patch(
            "/api/v1/tenants/line/preferred-channel/", {"preferred_channel": "telegram"}, format="json"
        )
        self.assertEqual(response.status_code, 200 if self.enabled else 409)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_channel, "telegram" if self.enabled else "line")
        if not self.enabled:
            self.assertEqual(response.data["code"], "telegram_unavailable")

    def test_line_linking_webhook_unchanged(self):
        from apps.router.line_service import generate_link_token
        from apps.router.line_webhook import LineWebhookView

        token = generate_link_token(self.user)
        event = {
            "type": "message",
            "source": {"userId": "line-test"},
            "message": {"type": "text", "text": f"link_{token.token}"},
        }
        with (
            patch("apps.router.line_webhook._send_line_reply"),
            patch("apps.router.line_webhook._send_line_push"),
            patch.object(LineWebhookView, "_get_line_profile_name", return_value="Test"),
        ):
            LineWebhookView()._handle_message(event)
        token.refresh_from_db()
        self.user.refresh_from_db()
        self.assertTrue(token.used)
        self.assertEqual(self.user.line_user_id, "line-test")

    def test_gate_api_visible_undeliverable(self):
        from apps.tenants.test_utils import seed_internal_key

        self.link()
        self.tenant.model_tier = "pro"
        self.tenant.save()
        seed_internal_key(self.tenant, "test-internal")
        sender = Mock(return_value=messaging.GateSendResult(True, "123"))
        with patch.dict(messaging._SENDERS, {"telegram": (sender, None)}):
            response = self.client.post(
                reverse("gate-request", kwargs={"tenant_id": self.tenant.id}),
                {
                    "action_type": ActionType.GMAIL_DELETE,
                    "payload": {"message_id": "msg-1"},
                    "display_summary": "Delete test email",
                },
                format="json",
                HTTP_X_INTERNAL_KEY="test-internal",
                HTTP_X_TENANT_ID=str(self.tenant.id),
            )
        self.assertEqual(response.status_code, 202 if self.enabled else 422)
        self.assertEqual(response.data["status"], "pending" if self.enabled else "undeliverable")
        action = PendingAction.objects.get(id=response.data["action_id"])
        # Existing no-surface API failure expires the row; it never executes it.
        self.assertEqual(action.status, ActionStatus.PENDING if self.enabled else ActionStatus.EXPIRED)
        self.assertIsNone(action.responded_at)
        if not self.enabled:
            self.assertNotIn("connect Telegram", response.data["message"])

    def extraction(self, *, force_line=False):
        from apps.journal.extraction import run_extraction_for_tenant

        self.link(line=force_line)
        self.tenant.experimental_typed_journal_lifecycle = True
        with ExitStack() as stack:
            for path, value in {
                "_get_daily_note_content": "A sufficiently long daily note.",
                "_call_extraction_llm": ({}, {}),
                "record_usage": None,
            }.items():
                stack.enter_context(patch(f"apps.journal.extraction.{path}", return_value=value))
            stack.enter_context(patch("apps.journal.reconciliation.gather_reconciliation_context", return_value={}))
            stack.enter_context(patch("apps.journal.reconciliation.apply_reconciliation_deltas", return_value=[Mock()]))
            stack.enter_context(patch("apps.journal.extraction._build_summary_text", return_value="Summary"))
            if force_line:
                stack.enter_context(patch("apps.router.cron_delivery.resolve_user_channel", return_value="line"))
            telegram = stack.enter_context(patch("apps.journal.extraction._deliver_summary_telegram"))
            line = stack.enter_context(patch("apps.journal.extraction._deliver_summary_line", return_value=False))
            app = stack.enter_context(patch("apps.router.proactive_context.record_proactive_outbound"))
            run_extraction_for_tenant(self.tenant)
        return telegram, line, app

    def test_extraction_primary(self):
        telegram, line, app = self.extraction()
        self.assertEqual(telegram.call_count, int(self.enabled))
        self.assertEqual(app.call_count, int(not self.enabled))
        line.assert_not_called()

    def test_extraction_line_failure(self):
        telegram, line, app = self.extraction(force_line=True)
        line.assert_called_once()
        self.assertEqual(telegram.call_count, int(self.enabled))
        self.assertEqual(app.call_count, int(not self.enabled))

    def test_profile_preference_write(self):
        self.link()
        self.user.preferred_channel = "line"
        self.user.save(update_fields=["preferred_channel"])
        response = self.client.patch("/api/v1/tenants/profile/", {"preferred_channel": "telegram"}, format="json")
        self.assertEqual(response.status_code, 200 if self.enabled else 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_channel, "telegram" if self.enabled else "line")

    def test_pending_queue_terminalizes_before_model(self):
        from apps.router.models import PendingMessage
        from apps.router.pending_queue import drain_pending_messages_for_tenant_task
        from apps.router.test_pending_queue import _ok_chat_response

        self.link()
        self.tenant.container_fqdn = "test.invalid"
        self.tenant.save(update_fields=["container_fqdn"])
        rows = [
            PendingMessage.objects.create(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="12345",
                user_text="hello",
                payload={"message_text": "hello", "user_param": "12345"},
            )
            for _ in range(2)
        ]
        with patch("apps.router.pending_queue.httpx.post", return_value=_ok_chat_response("reply")) as post:
            result = drain_pending_messages_for_tenant_task(str(self.tenant.id), "telegram", "12345")
            drain_pending_messages_for_tenant_task(str(self.tenant.id), "telegram", "12345")
        model_calls = [call for call in post.call_args_list if "/v1/chat/completions" in call.args[0]]
        self.assertEqual(len(model_calls), int(self.enabled))
        if self.enabled:
            self.assertEqual(result["delivered"], 2)
            self.assertFalse(PendingMessage.objects.filter(id__in=[r.id for r in rows]).exists())
        else:
            self.assertEqual(result["delivered"], 0)
            for row in rows:
                row.refresh_from_db()
                self.assertEqual(row.delivery_status, PendingMessage.Status.FAILED)
                self.assertEqual(row.delivery_attempts, 0)
                self.assertIsNone(row.delivery_in_flight_until)
            self.assertEqual(post.call_count, 2)
            for call in post.call_args_list:
                self.assertIn("unavailable", call.kwargs["json"]["text"])

    def test_buffered_queue_terminalizes_before_model(self):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task
        from apps.router.models import BufferedMessage
        from apps.router.test_pending_queue import _ok_chat_response

        self.link()
        self.tenant.container_fqdn = "test.invalid"
        self.tenant.save(update_fields=["container_fqdn"])
        row = BufferedMessage.objects.create(tenant=self.tenant, channel="telegram", user_text="hello", payload={})
        with (
            patch("httpx.post", return_value=_ok_chat_response("reply")) as post,
            patch("apps.router.services.send_telegram_message") as notice,
            patch("apps.router.pending_queue.relay_ai_response_to_telegram", return_value=True) as relay,
        ):
            result = deliver_buffered_messages_task(str(self.tenant.id))
            deliver_buffered_messages_task(str(self.tenant.id))
        self.assertEqual(post.call_count, int(self.enabled))
        self.assertEqual(relay.call_count, int(self.enabled))
        self.assertEqual(notice.call_count, int(not self.enabled))
        if self.enabled:
            self.assertEqual(result["delivered"], 1)
            self.assertFalse(BufferedMessage.objects.filter(pk=row.pk).exists())
        else:
            self.assertEqual(result["delivered"], 0)
            row.refresh_from_db()
            self.assertEqual(row.delivery_status, BufferedMessage.Status.FAILED)
            self.assertEqual(row.delivery_attempts, 0)
            self.assertIsNone(row.delivery_in_flight_until)
            self.assertIn("unavailable", notice.call_args.args[1])

    def test_cron_no_gateway_exposes_undeliverable(self):
        from apps.actions.origin import OriginStamp
        from apps.cron.gate import cron_action_state, request_cron_action
        from apps.cron.models import CronJob

        self.device()
        for linked in (True, False):
            with self.subTest(linked=linked):
                self.user.telegram_chat_id = 12345 if linked else None
                self.user.save(update_fields=["telegram_chat_id"])
                with patch.dict(
                    messaging._SENDERS, {"telegram": (Mock(return_value=messaging.GateSendResult(True, "123")), None)}
                ):
                    result = request_cron_action(
                        self.tenant,
                        cron_request_id=f"availability-{linked}",
                        pattern="pure_reminder",
                        name=f"Reminder {linked}",
                        schedule={"kind": "cron", "expr": "0 8 * * 2", "tz": "UTC"},
                        typed_payload={"text": "Take out trash"},
                        reason="Requested",
                        origin_stamp=OriginStamp(),
                    )
                action = PendingAction.objects.get(pk=result["action_id"])
                delivered = self.enabled and linked
                self.assertEqual(result["state"], "pending_approval" if delivered else "approval_undeliverable")
                self.assertEqual(cron_action_state(action)["state"], result["state"])
                self.assertEqual(action.status, ActionStatus.PENDING)
                self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())
                if not delivered:
                    self.assertEqual(result["delivery_state"], "no_channel")
                    self.assertIn("not delivered", result["detail"])

    def test_cron_failed_transport_is_not_a_sent_prompt(self):
        from apps.cron.gate import cron_action_state

        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CRON_CREATE,
            action_payload={},
            display_summary="Reminder",
            delivery_state="failed",
            expires_at=timezone.now(),
        )
        result = cron_action_state(action)
        self.assertEqual(result["state"], "approval_undeliverable")
        self.assertEqual(result["delivery_state"], "failed")
        self.assertEqual(action.status, ActionStatus.PENDING)


class TelegramEnabledTests(AvailabilityContracts, TestCase):
    enabled = True


class TelegramDisabledTests(AvailabilityContracts, TestCase):
    enabled = False


class AvailabilityCacheTests(TestCase):
    def test_identity_caches_vary_with_availability(self):
        user = User.objects.create_user(username="cache-availability")
        Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
        client = APIClient()
        client.force_authenticate(user)
        cache.clear()
        for enabled in (True, False, True):
            with override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                for path in ("/api/v1/auth/me/", "/api/v1/tenants/me/"):
                    response = client.get(path)
                    data = response.data.get("tenant", response.data)
                    self.assertIs(data["telegram_available"], enabled)

    def test_tag_bump_invalidates_both_availability_variants(self):
        from apps.common.cache import bump_tag

        user = User.objects.create_user(username="tag-availability", display_name="Before")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
        client = APIClient()
        cache.clear()
        paths = ("/api/v1/auth/me/", "/api/v1/tenants/me/")
        for enabled in (True, False):
            with override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                for path in paths:
                    client.force_authenticate(User.objects.get(pk=user.pk))
                    self.assertEqual(client.get(path).status_code, 200)
        User.objects.filter(pk=user.pk).update(display_name="After")
        bump_tag(tenant.id, "tenant")
        for enabled in (True, False):
            with override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                for path in paths:
                    client.force_authenticate(User.objects.get(pk=user.pk))
                    data = client.get(path).data
                    tenant_data = data.get("tenant", data)
                    self.assertEqual(tenant_data["user"]["display_name"], "After")
                    self.assertIs(tenant_data["telegram_available"], enabled)


class AvailabilitySettingTests(SimpleTestCase):
    def test_poller_env_truthiness_contract(self):
        import ast
        from pathlib import Path

        tree = ast.parse((Path(__file__).resolve().parents[2] / "config/settings/base.py").read_text())
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "TELEGRAM_INBOUND_ENABLED" for target in node.targets)
        )
        expression = compile(ast.Expression(assignment.value), "base.py", "eval")
        import os
        import re
        import subprocess

        import environ

        root = Path(__file__).resolve().parents[2]
        startup = subprocess.check_output(["git", "show", "origin/main:startup.sh"], cwd=root, text=True)
        case = re.search(r'case "\$\{TELEGRAM_POLLER_ENABLED-true\}" in.*?\nesac', startup, re.S).group()
        for value in (
            None,
            "",
            " ",
            "true",
            "1",
            "yes",
            "on",
            "tRuE",
            "YeS",
            "false",
            "0",
            "no",
            "off",
            "FaLsE",
            "oFf",
            " false ",
            "\tOFF\n",
            '"false"',
            "'off'",
            "$ALIAS",
            "garbage",
        ):
            with self.subTest(value=value), patch.dict(os.environ, {"ALIAS": "false"}):
                if value is None:
                    os.environ.pop("TELEGRAM_POLLER_ENABLED", None)
                else:
                    os.environ["TELEGRAM_POLLER_ENABLED"] = value
                shell = subprocess.run(
                    ["bash", "-c", case + '\nprintf "%s" "$POLLER_ENABLED"'], text=True, capture_output=True, check=True
                ).stdout
                self.assertIs(eval(expression, {"env": environ.Env(), "os": os}), shell == "true")

    def test_helper_reads_overridden_setting_each_time(self):
        from apps.router.telegram_availability import telegram_inbound_enabled

        for enabled in (True, False, True):
            with override_settings(TELEGRAM_INBOUND_ENABLED=enabled):
                self.assertIs(telegram_inbound_enabled(), enabled)
