"""iOS paywall gate (Phase D free tier), dark behind IOS_PAYWALL_ENFORCE_TENANT_IDS.

The server refuses the assistant only when ALL hold: the flag opens the tenant,
the iOS client declares ``X-NBHD-Client: ios/<v>/<b>; paywall=2`` (or higher),
and the tenant is paused (suspended, no trial, not paying, not budget-exempt).
Everyone else — web, Telegram/LINE, every shipped iOS build (``paywall=1``) — is
served exactly as before.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.entitlement import ai_paused
from apps.billing.models import AppStoreSubscription
from apps.router import chat_shape_views, siri_views, talk_route_views
from apps.router.chat_gates import ios_paywall_version
from apps.router.chat_views import _get_or_create_main_thread
from apps.router.models import AppChatMessage
from apps.tenants.models import Tenant, User

PAYWALL_1 = "ios/2.4.2/108; paywall=1"
PAYWALL_2 = "ios/2.4.4/110; paywall=2"
SUBSCRIPTION_REQUIRED = {"status": "error", "error": "subscription_required"}


def _header(value: str | None) -> dict:
    return {} if value is None else {"HTTP_X_NBHD_CLIENT": value}


class IOSPaywallVersionParseTest(SimpleTestCase):
    def _version(self, value):
        return ios_paywall_version(RequestFactory().get("/", **_header(value)))

    def test_header_table(self):
        cases = [
            (None, 0),
            ("", 0),
            ("   ", 0),
            (PAYWALL_1, 1),
            (PAYWALL_2, 2),
            ("ios/2.4.4/110;paywall=3", 3),
            ("  ios/2.4.4/110 ;  locale=ja ; PayWall = 2 ; extra  ", 2),
            ("ios/2.4.4/110; locale=ja; paywall=2; paywall=0", 2),  # first one wins
            ("ios/2.4.4/110", 0),
            ("ios/2.4.4/110; paywall", 0),
            ("ios/2.4.4/110; paywall=", 0),
            ("ios/2.4.4/110; paywall=abc", 0),
            ("ios/2.4.4/110; paywall=2abc", 0),
            ("ios/2.4.4/110; paywall=-2", 0),
            ("ios/2.4.4/110; paywall=2.0", 0),
            ("ios/2.4.4/110; paywall=٢", 0),  # Arabic-Indic digit two
            ("ios/2.4.4/110; paywall=" + "9" * 400, 0),  # absurdly large
            ("ios/2.4.4/110; paywall=9999999", 0),  # 7 digits: junk, not a contract
            ("ios/2.4.4/110; paywall=999999", 999999),
            ("web/1.0; paywall=2", 0),
            ("android/1.0/1; paywall=2", 0),
            ("iOS/2.4.4/110; paywall=2", 0),  # the app always sends lowercase
            ("paywall=2", 0),
            ("garbage;;;===;", 0),
            ("ios/;=;paywall==2", 0),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(self._version(value), expected)

    def test_never_raises_on_odd_request_objects(self):
        class NoMeta:
            pass

        self.assertEqual(ios_paywall_version(NoMeta()), 0)


def _make_tenant(*, status=Tenant.Status.SUSPENDED, is_trial=False, budget_exempt=False, stripe="") -> Tenant:
    user = User.objects.create_user(
        username=f"paywall_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        preferred_channel="telegram",
    )
    return Tenant.objects.create(
        user=user,
        status=status,
        is_trial=is_trial,
        trial_ends_at=timezone.now() + timedelta(days=7) if is_trial else None,
        is_budget_exempt=budget_exempt,
        stripe_subscription_id=stripe,
        container_fqdn="oc-paywall.example.com",
    )


def _apple(tenant, *, entitles: bool) -> AppStoreSubscription:
    return AppStoreSubscription.objects.create(
        tenant=tenant,
        original_transaction_id=f"otx-{secrets.token_hex(6)}",
        status=AppStoreSubscription.Status.ACTIVE if entitles else AppStoreSubscription.Status.EXPIRED,
        entitles=entitles,
        expires_at=timezone.now() + (timedelta(days=30) if entitles else -timedelta(days=3)),
    )


class AiPausedTest(TestCase):
    def test_matrix(self):
        self.assertFalse(ai_paused(_make_tenant(status=Tenant.Status.ACTIVE)))
        self.assertFalse(ai_paused(_make_tenant(is_trial=True)))
        self.assertFalse(ai_paused(_make_tenant(stripe="sub_live")))
        apple_paying = _make_tenant()
        _apple(apple_paying, entitles=True)
        self.assertFalse(ai_paused(apple_paying))
        self.assertFalse(ai_paused(_make_tenant(budget_exempt=True)))
        self.assertTrue(ai_paused(_make_tenant()))
        lapsed_apple = _make_tenant()
        _apple(lapsed_apple, entitles=False)
        self.assertTrue(ai_paused(lapsed_apple))
        self.assertFalse(ai_paused(None))

    def test_active_tenant_costs_no_query(self):
        tenant = _make_tenant(status=Tenant.Status.ACTIVE)
        with self.assertNumQueries(0):
            self.assertFalse(ai_paused(tenant))


class _PaywallClientMixin:
    def client_for(self, tenant: Tenant, header: str | None = PAYWALL_2) -> APIClient:
        client = APIClient(**_header(header))
        # A fresh user row so user.tenant reflects the tenant's current DB state.
        client.force_authenticate(user=User.objects.get(pk=tenant.user_id))
        return client


@patch("apps.pii.redactor._detect_pii", return_value=[])
@patch("apps.cron.publish.publish_task")
@patch("apps.router.chat_views.enqueue_message_for_tenant")
class ChatMessagePaywallTest(_PaywallClientMixin, TestCase):
    def setUp(self):
        self.paused = _make_tenant()
        # Hibernated, so "as today" visibly publishes a wake and a refusal visibly doesn't.
        Tenant.objects.filter(pk=self.paused.pk).update(hibernated_at=timezone.now() - timedelta(hours=1))
        self.paused.refresh_from_db()

    def post(self, tenant, header=PAYWALL_2, client_msg_id="pw-1", **extra):
        return self.client_for(tenant, header).post(
            "/api/v1/chat/messages/",
            {"text": "plan my week", "client_msg_id": client_msg_id, **extra},
            format="json",
        )

    def assert_served(self, resp, tenant, enqueue, *, client_msg_id="pw-1"):
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["status"], "pending")
        self.assertTrue(AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).exists())
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["tenant"].pk, tenant.pk)

    def assert_refused(self, resp, tenant, enqueue, wake, *, client_msg_id="pw-1"):
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data, {"client_msg_id": client_msg_id, **SUBSCRIPTION_REQUIRED})
        self.assertEqual(AppChatMessage.objects.filter(tenant=tenant).count(), 0)
        enqueue.assert_not_called()
        wake.assert_not_called()
        tenant.refresh_from_db()
        self.assertIsNone(tenant.last_message_at)

    def test_flag_off_serves_paused_paywall2_as_today(self, enqueue, wake, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=""):
            resp = self.post(self.paused)
        self.assert_served(resp, self.paused, enqueue)
        wake.assert_called_once_with("wake_tenant_for_app_message", str(self.paused.id), retries=1)

    def test_flag_on_no_header_serves(self, enqueue, wake, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)):
            resp = self.post(self.paused, header=None)
        self.assert_served(resp, self.paused, enqueue)

    def test_flag_on_paywall1_serves(self, enqueue, wake, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)):
            resp = self.post(self.paused, header=PAYWALL_1)
        self.assert_served(resp, self.paused, enqueue)

    def test_flag_on_paywall2_paused_refused_and_nothing_persisted(self, enqueue, wake, _pii):
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)),
            self.assertLogs("apps.router.chat_gates", level="INFO") as logs,
        ):
            resp = self.post(self.paused, image="not-even-decoded")
        self.assert_refused(resp, self.paused, enqueue, wake)
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.endpoint, "chat_messages")
        self.assertEqual(record.tenant_id, str(self.paused.id))
        self.assertEqual(record.paywall, 2)
        self.assertIs(record.flag_allowed, True)
        self.assertIs(record.refused, True)
        self.assertNotIn("plan my week", logs.output[0])

    def test_refused_client_msg_id_is_free_for_the_local_turn_record(self, enqueue, wake, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)):
            self.post(self.paused)
            with patch("apps.router.conversation_capture.schedule_user_md_refresh"):
                turn = self.client_for(self.paused).post(
                    "/api/v1/chat/turns/",
                    {"text": "plan my week", "reply_text": "Here is a plan.", "client_msg_id": "pw-1"},
                    format="json",
                )
        self.assertEqual(turn.status_code, 201, turn.content)
        self.assertEqual(turn.data["source"], "on_device")

    def test_flag_for_a_different_tenant_serves(self, enqueue, wake, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="00000000-0000-0000-0000-000000000001"):
            resp = self.post(self.paused)
        self.assert_served(resp, self.paused, enqueue)

    def test_flag_star_refuses(self, enqueue, wake, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            resp = self.post(self.paused)
        self.assert_refused(resp, self.paused, enqueue, wake)

    def test_flag_on_paywall2_entitled_tenants_served(self, enqueue, wake, _pii):
        apple = _make_tenant()
        _apple(apple, entitles=True)
        tenants = {
            "trial": _make_tenant(is_trial=True),
            "stripe": _make_tenant(stripe="sub_live"),
            "apple": apple,
            "budget_exempt": _make_tenant(budget_exempt=True),
            "active": _make_tenant(status=Tenant.Status.ACTIVE),
        }
        for label, tenant in tenants.items():
            with self.subTest(label), override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
                enqueue.reset_mock()
                resp = self.post(tenant, client_msg_id=f"pw-{label}")
                self.assert_served(resp, tenant, enqueue, client_msg_id=f"pw-{label}")

    def test_replay_of_stored_turn_beats_refusal(self, enqueue, wake, _pii):
        AppChatMessage.objects.create(
            tenant=self.paused,
            user=self.paused.user,
            thread=_get_or_create_main_thread(self.paused, self.paused.user),
            client_msg_id="pw-old",
            user_text="from before the pause",
            reply_text="An earlier answer.",
            status=AppChatMessage.Status.READY,
        )
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            resp = self.post(self.paused, client_msg_id="pw-old")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["client_msg_id"], "pw-old")
        self.assertEqual(resp.data["status"], "ready")
        self.assertEqual(resp.data["reply_text"], "An earlier answer.")
        self.assertEqual(resp.data["error"], "")
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 1)
        enqueue.assert_not_called()

    def test_flag_off_still_logs_paused_turn_for_adoption(self, enqueue, wake, _pii):
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=""),
            self.assertLogs("apps.router.chat_gates", level="INFO") as logs,
        ):
            self.post(self.paused, header=PAYWALL_1)
        self.assertEqual(logs.records[0].paywall, 1)
        self.assertIs(logs.records[0].flag_allowed, False)
        self.assertIs(logs.records[0].refused, False)


def _completion(text: str) -> tuple[dict, str]:
    return {"choices": [{"message": {"content": text}}]}, "test-model"


@patch("apps.pii.redactor._detect_pii", return_value=[])
@patch("apps.router.chat_views.enqueue_message_for_tenant")
@patch("apps.common.openrouter.chat_completion", return_value=_completion("Two tasks are due."))
class SiriPaywallTest(_PaywallClientMixin, TestCase):
    def setUp(self):
        self.paused = _make_tenant()
        snapshot = patch.object(siri_views, "_placeholder_snapshot", return_value="STATE")
        intent = patch.object(siri_views, "_placeholder_intent", side_effect=lambda tenant, text, ingress=None: text)
        self.snapshot = snapshot.start()
        self.intent = intent.start()
        self.addCleanup(snapshot.stop)
        self.addCleanup(intent.stop)

    def ask(self, header=PAYWALL_2):
        return self.client_for(self.paused, header).post(
            "/api/v1/siri/respond/", {"intent": "what's due?"}, format="json"
        )

    def test_refused_before_placeholder_work_and_any_model(self, completion, enqueue, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)):
            resp = self.ask()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data, {"answered": False, "escalated": False, "error": "subscription_required"})
        self.snapshot.assert_not_called()
        self.intent.assert_not_called()
        completion.assert_not_called()
        enqueue.assert_not_called()
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 0)

    def test_paywall1_served_as_today(self, completion, enqueue, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)):
            resp = self.ask(header=PAYWALL_1)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["answered"])
        self.snapshot.assert_called_once()
        completion.assert_called_once()

    def test_flag_off_served_as_today(self, completion, enqueue, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=""):
            resp = self.ask()
        self.assertTrue(resp.data["answered"])
        completion.assert_called_once()


@override_settings(OPENROUTER_API_KEY="test-only", TEST_MODE=True)
class TalkAndShapePaywallTest(_PaywallClientMixin, TestCase):
    def setUp(self):
        self.paused = _make_tenant()

    def test_talk_route_degraded_without_model_call(self):
        with (
            patch.object(talk_route_views, "route_talk", wraps=talk_route_views.route_talk) as route_talk,
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*", TALK_ROUTE_TENANT_IDS="*"),
        ):
            resp = self.client_for(self.paused).post("/api/v1/chat/talk-route/", {"text": "log a run"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = dict(resp.data)
        body.pop("latency_ms")
        self.assertEqual(body, {"ack_kind": "other", "quick_read": "none", "reason": "unavailable"})
        route_talk.assert_not_called()

    def test_talk_route_paywall1_reaches_model_function(self):
        with (
            patch.object(talk_route_views, "route_talk", wraps=talk_route_views.route_talk) as route_talk,
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*", TALK_ROUTE_TENANT_IDS=""),
        ):
            resp = self.client_for(self.paused, PAYWALL_1).post(
                "/api/v1/chat/talk-route/", {"text": "log a run"}, format="json"
            )
        self.assertEqual(resp.data["reason"], "disabled")
        route_talk.assert_called_once()

    def test_chat_shape_degraded_without_model_call(self):
        with (
            patch.object(chat_shape_views, "shape_chat", wraps=chat_shape_views.shape_chat) as shape_chat,
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*", CHAT_SHAPE_TENANT_IDS="*"),
        ):
            resp = self.client_for(self.paused).post(
                "/api/v1/chat/shape/",
                {"client_msg_id": "6f1c1f0e-3d7c-4c55-9a51-1b2f7f0e9a01", "text": "how did I sleep"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["reason"], "unavailable")
        self.assertIs(resp.data["enabled"], True)
        self.assertIsNone(resp.data["panel"])
        shape_chat.assert_not_called()

    def test_chat_shape_flag_off_reaches_model_function(self):
        with (
            patch.object(chat_shape_views, "shape_chat", wraps=chat_shape_views.shape_chat) as shape_chat,
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="", CHAT_SHAPE_TENANT_IDS=""),
        ):
            resp = self.client_for(self.paused).post(
                "/api/v1/chat/shape/",
                {"client_msg_id": "6f1c1f0e-3d7c-4c55-9a51-1b2f7f0e9a01", "text": "how did I sleep"},
                format="json",
            )
        self.assertEqual(resp.data["reason"], "disabled")
        shape_chat.assert_called_once()


@override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*")
class LocalTurnPaywallTest(_PaywallClientMixin, TestCase):
    def setUp(self):
        self.paused = _make_tenant()

    @patch("apps.router.conversation_capture.schedule_user_md_refresh")
    def test_paused_paywall2_records_local_turn_and_logs_engine(self, _refresh):
        with self.assertLogs("apps.router.chat_views", level="INFO") as logs:
            resp = self.client_for(self.paused).post(
                "/api/v1/chat/turns/",
                {"text": "hi", "reply_text": "Hello.", "client_msg_id": "lt-1", "engine": "apple_cloud"},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        engine_lines = [r for r in logs.records if getattr(r, "engine", None)]
        self.assertEqual([r.engine for r in engine_lines], ["apple_cloud"])
        self.assertEqual(engine_lines[0].tenant_id, str(self.paused.id))

    @patch("apps.router.conversation_capture.schedule_user_md_refresh")
    def test_unknown_or_malformed_engine_is_ignored(self, _refresh):
        for n, engine in enumerate(["gpt", {"x": 1}, ["on_device"], 7, ""]):
            with self.subTest(engine=engine), patch("apps.router.chat_views.logger") as log:
                resp = self.client_for(self.paused).post(
                    "/api/v1/chat/turns/",
                    {"text": "hi", "reply_text": "Hello.", "client_msg_id": f"lt-x{n}", "engine": engine},
                    format="json",
                )
                self.assertEqual(resp.status_code, 201, resp.content)
                self.assertFalse(
                    any(c.args and str(c.args[0]).startswith("chat_local_turn") for c in log.info.call_args_list)
                )


class TenantMePaywallFieldTest(_PaywallClientMixin, TestCase):
    def setUp(self):
        cache.clear()  # /tenants/me/ body is tenant-cached for 60 s
        self.paused = _make_tenant()

    def paywall(self, tenant, header=PAYWALL_2):
        resp = self.client_for(tenant, header).get("/api/v1/tenants/me/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.data["subscription"]["paywall"]

    def test_true_only_in_the_refuse_case(self):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)):
            self.assertIs(self.paywall(self.paused), True)
            self.assertIs(self.paywall(self.paused, PAYWALL_1), False)
            self.assertIs(self.paywall(self.paused, None), False)  # web
            self.assertIs(self.paywall(self.paused), True)  # cached body, fresh value
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=""):
            self.assertIs(self.paywall(self.paused), False)

    def test_false_for_entitled_tenants(self):
        for tenant in (_make_tenant(stripe="sub_live"), _make_tenant(is_trial=True), _make_tenant(budget_exempt=True)):
            with self.subTest(tenant=tenant.pk), override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
                self.assertIs(self.paywall(tenant), False)

    def test_other_subscription_fields_unchanged(self):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            subscription = self.client_for(self.paused).get("/api/v1/tenants/me/").data["subscription"]
        self.assertEqual(
            set(subscription),
            {"apple_status", "active", "source", "status", "renews_at", "auto_renew", "trial_ends_at", "paywall"},
        )
        self.assertIs(subscription["active"], False)
