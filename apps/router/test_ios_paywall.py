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
from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.entitlement import ai_paused
from apps.billing.models import AppStoreSubscription
from apps.router import chat_shape_views, siri_views, talk_route_views
from apps.router.chat_gates import _tenant_allowed, ios_paywall_version
from apps.router.chat_shape import ChatShapeResponse
from apps.router.chat_views import _get_or_create_main_thread
from apps.router.models import AppChatMessage
from apps.router.talk_route import TalkRouteResponse
from apps.tenants.models import Tenant, User

PAYWALL_1 = "ios/2.4.2/108; paywall=1"
PAYWALL_2 = "ios/2.4.4/110; paywall=2"
# Two copies of the header comma-joined by a proxy: must read as an old client.
DUPLICATED = "ios/2.4.2/108, ios/2.4.4/110; paywall=2"
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
            # The exact string the shipped app builds (nbhd-ios ClientIdentity.headerValue).
            ("ios/2.4.2/108; paywall=1", 1),
            ("ios/0/0; paywall=1", 1),  # its fallback when Info.plist keys are missing
            ("ios/2.5.0/120; paywall=2", 2),  # the same shape from a free-tier build
            ("ios/0/0; paywall=2", 2),
            ("ios/2.4.4/110;paywall=3", 3),
            ("  ios/2.4.4/110 ;  locale=ja ; PayWall = 2 ; extra  ", 2),
            ("\tios/2.4.4/110;\tpaywall=2\t", 2),  # ASCII tab is HTTP whitespace
            ("ios/2.4.4/110; paywall=0", 0),
            ("ios/2.4.4/110; paywall=999999", 999999),
            # More than one paywall param: ambiguous, so 0 (whichever order).
            ("ios/2.4.4/110; locale=ja; paywall=2; paywall=0", 0),
            ("ios/2.4.4/110; paywall=2; paywall=2", 0),
            ("ios/2.4.4/110; paywall; paywall=2", 0),
            ("ios/2.4.4/110; PAYWALL=2; paywall=2", 0),
            # Any comma: a proxy comma-joined two copies of the header.
            ("ios/2.4.2/108, ios/2.4.4/110; paywall=2", 0),
            ("ios/2.4.4/110; paywall=2, ios/2.4.2/108; paywall=1", 0),
            ("ios/2.4.4/110; paywall=2,", 0),
            # Leading zeros are not a contract value.
            ("ios/2.4.4/110; paywall=02", 0),
            ("ios/2.4.4/110; paywall=000002", 0),
            ("ios/2.4.4/110; paywall=00", 0),
            # Only ASCII space/tab is stripped.
            ("ios/2.4.4/110; paywall=\u00a02", 0),  # NBSP
            ("ios/2.4.4/110; paywall=2\u00a0", 0),
            ("ios/2.4.4/110;\u00a0paywall=2", 0),
            ("ios/2.4.4/110; paywall=2\x0b", 0),  # vertical tab
            ("\u00a0ios/2.4.4/110; paywall=2", 0),
            ("ios/2.4.4/110; paywall=\u20032", 0),  # em space
            # Nothing outside printable ASCII + tab anywhere in the header, so a
            # second, mangled paywall param can't hide behind a non-ASCII key.
            ("ios/2.4.4/110; paywall=2;\x0bpaywall=3", 0),
            ("ios/2.4.4/110; paywall=2; \u00a0paywall=3", 0),
            ("ios/2.4.4/110\u00e9; paywall=2", 0),
            ("ios/2.4.4/110\x01; paywall=2", 0),
            ("ios/2.4.4/110; paywall=2\r\n", 0),
            ("ios/2.4.4/110; paywall=2\x7f", 0),
            # A product token is required after ios/.
            ("ios/; paywall=2", 0),
            ("ios/ ; paywall=2", 0),
            ("ios; paywall=2", 0),
            ("ios/;=;paywall==2", 0),
            ("ios/2.4.4/110", 0),
            ("ios/2.4.4/110; paywall", 0),
            ("ios/2.4.4/110; paywall=", 0),
            ("ios/2.4.4/110; paywall=abc", 0),
            ("ios/2.4.4/110; paywall=2abc", 0),
            ("ios/2.4.4/110; paywall=2x", 0),
            ("ios/2.4.4/110; paywall=+2", 0),
            ("ios/2.4.4/110; paywall=-2", 0),
            ("ios/2.4.4/110; paywall=2.0", 0),
            ("ios/2.4.4/110; paywall=2 2", 0),
            ("ios/2.4.4/110; paywall=\u0662", 0),  # Arabic-Indic digit two
            ("ios/2.4.4/110; paywall=\uff12", 0),  # fullwidth digit two
            ("ios/2.4.4/110; paywall=\u00b2", 0),  # superscript two
            ("ios/2.4.4/110; paywall=9999999", 0),  # 7 digits: junk, not a contract
            ("ios/2.4.4/110; paywall=" + "9" * 400, 0),  # absurdly large
            ("ios/" + "1" * 242 + "; paywall=2", 0),  # 257 characters: over the limit
            ("web/1.0; paywall=2", 0),
            ("android/1.0/1; paywall=2", 0),
            ("iOS/2.4.4/110; paywall=2", 0),  # the app always sends lowercase
            ("IOS/2.4.4/110; paywall=2", 0),
            ("paywall=2", 0),
            ("garbage;;;===;", 0),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(self._version(value), expected)

    def test_header_at_the_length_limit_still_parses(self):
        tail = "; paywall=2"
        value = "ios/" + "1" * (256 - len("ios/") - len(tail)) + tail
        self.assertEqual(len(value), 256)
        self.assertEqual(self._version(value), 2)
        self.assertEqual(self._version(value[:4] + "1" + value[4:]), 0)

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
        # Only SUSPENDED is paused. In particular a normal Stripe cancellation goes
        # DEPROVISIONING -> DELETED, so a churned Stripe user is never refused (known gap).
        for status in (
            Tenant.Status.PENDING,
            Tenant.Status.PROVISIONING,
            Tenant.Status.DEPROVISIONING,
            Tenant.Status.DELETED,
        ):
            with self.subTest(status=status):
                self.assertFalse(ai_paused(_make_tenant(status=status)))
        # SUSPENDED with is_trial still true is never paused (same as Telegram/LINE).
        self.assertFalse(ai_paused(_make_tenant(is_trial=True)))

    def test_paused_tenant_costs_one_query(self):
        tenant = _make_tenant()
        with self.assertNumQueries(1):
            self.assertTrue(ai_paused(tenant))

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

    def test_flag_off_paywall2_logs_paused_turn_for_adoption(self, enqueue, wake, _pii):
        # A new build from a paused tenant is still measured while the flag is off.
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=""),
            self.assertLogs("apps.router.chat_gates", level="INFO") as logs,
        ):
            resp = self.post(self.paused)
        self.assert_served(resp, self.paused, enqueue)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].paywall, 2)
        self.assertIs(logs.records[0].flag_allowed, False)
        self.assertIs(logs.records[0].refused, False)

    def counted_post(self, tenant, header=PAYWALL_2):
        """``(response, number of queries)`` for one chat send."""
        with CaptureQueriesContext(connection) as ctx:
            resp = self.post(tenant, header=header)
        return resp, len(ctx.captured_queries)

    def assert_gate_adds_nothing(self, enqueue, *, header, status=Tenant.Status.SUSPENDED):
        """The same send with the gate stubbed out vs. real: equal query counts and
        no gate log line, from two identically-built tenants."""
        baseline_tenant, gated_tenant = _make_tenant(status=status), _make_tenant(status=status)
        with patch("apps.router.chat_views.ios_paywall_refuses", return_value=False):
            _resp, baseline = self.counted_post(baseline_tenant, header)
        enqueue.reset_mock()
        with self.assertNoLogs("apps.router.chat_gates", level="DEBUG"):
            resp, gated = self.counted_post(gated_tenant, header)
        self.assert_served(resp, gated_tenant, enqueue)
        self.assertEqual(gated, baseline)

    def test_old_clients_cost_no_query_and_no_log(self, enqueue, wake, _pii):
        # Web (no header), every shipped build (paywall=1) and a comma-joined
        # duplicate header return before the flag or ai_paused is read: no added
        # query, no log line, for a paused tenant, with the flag off (today) and on.
        self.post(_make_tenant(), header=None, client_msg_id="warm")  # warm per-process caches
        for flag in ("", "*"):
            for label, header in (("web", None), ("paywall1", PAYWALL_1), ("dup", DUPLICATED)):
                self._assert_old_client_untouched(enqueue, flag=flag, label=label, header=header)

    def _assert_old_client_untouched(self, enqueue, *, flag, label, header):
        with (
            self.subTest(flag=flag, client=label),
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=flag),
            patch("apps.router.chat_gates.ai_paused", wraps=ai_paused) as paused_check,
            patch("apps.router.chat_gates._tenant_allowed", wraps=_tenant_allowed) as flag_check,
        ):
            enqueue.reset_mock()
            self.assert_gate_adds_nothing(enqueue, header=header)
            paused_check.assert_not_called()
            paywall_flag_reads = [c for c in flag_check.call_args_list if c.args[1] == "IOS_PAYWALL_ENFORCE_TENANT_IDS"]
            self.assertEqual(paywall_flag_reads, [])

    def test_active_tenant_on_paywall2_costs_no_query(self, enqueue, wake, _pii):
        self.post(_make_tenant(status=Tenant.Status.ACTIVE), client_msg_id="warm")
        enqueue.reset_mock()
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            self.assert_gate_adds_nothing(enqueue, header=PAYWALL_2, status=Tenant.Status.ACTIVE)

    def test_refused_without_client_msg_id(self, enqueue, wake, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            resp = self.client_for(self.paused).post("/api/v1/chat/messages/", {"text": "hi"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(set(resp.data), {"client_msg_id", "status", "error"})
        self.assertEqual({k: resp.data[k] for k in ("status", "error")}, SUBSCRIPTION_REQUIRED)
        self.assertRegex(resp.data["client_msg_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 0)
        enqueue.assert_not_called()
        wake.assert_not_called()

    def test_non_suspended_statuses_served_as_today(self, enqueue, wake, _pii):
        # Known gap pinned: only SUSPENDED is refused. A churned Stripe user goes
        # DEPROVISIONING -> DELETED and keeps today's response, whatever that is.
        for tenant_status in (
            Tenant.Status.PENDING,
            Tenant.Status.PROVISIONING,
            Tenant.Status.DEPROVISIONING,
            Tenant.Status.DELETED,
        ):
            with self.subTest(status=tenant_status), override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
                baseline_tenant, gated_tenant = _make_tenant(status=tenant_status), _make_tenant(status=tenant_status)
                with patch("apps.router.chat_views.ios_paywall_refuses", return_value=False):
                    baseline = self.post(baseline_tenant, client_msg_id=f"base-{tenant_status}")
                with self.assertNoLogs("apps.router.chat_gates", level="DEBUG"):
                    gated = self.post(gated_tenant, client_msg_id=f"gated-{tenant_status}")
                self.assertEqual(gated.status_code, baseline.status_code, gated.content)
                self.assertEqual(gated.data.get("status"), baseline.data.get("status"))
                self.assertEqual(gated.data.get("error"), baseline.data.get("error"))
                self.assertNotEqual(gated.data.get("error"), "subscription_required")
                self.assertEqual(
                    AppChatMessage.objects.filter(tenant=gated_tenant).count(),
                    AppChatMessage.objects.filter(tenant=baseline_tenant).count(),
                )

    def test_duplicated_header_served_as_today(self, enqueue, wake, _pii):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            resp = self.post(self.paused, header=DUPLICATED)
        self.assert_served(resp, self.paused, enqueue)


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

    def ask(self, header=PAYWALL_2, **extra):
        return self.client_for(self.paused, header).post(
            "/api/v1/siri/respond/", {"intent": "what's due?", **extra}, format="json"
        )

    def test_refused_before_placeholder_work_and_any_model(self, completion, enqueue, _pii):
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)),
            self.assertLogs("apps.router.chat_gates", level="INFO") as logs,
        ):
            resp = self.ask()
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.endpoint, "siri_respond")
        self.assertEqual(record.tenant_id, str(self.paused.id))
        self.assertEqual(record.paywall, 2)
        self.assertIs(record.flag_allowed, True)
        self.assertIs(record.refused, True)
        self.assertNotIn("what's due", logs.output[0])
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

    def _stored_turn(self, client_msg_id="siri-old"):
        return AppChatMessage.objects.create(
            tenant=self.paused,
            user=self.paused.user,
            thread=_get_or_create_main_thread(self.paused, self.paused.user),
            client_msg_id=client_msg_id,
            user_text="from before the pause",
            reply_text="An earlier answer.",
            status=AppChatMessage.Status.READY,
        )

    def test_known_client_msg_id_replays_instead_of_refusal(self, completion, enqueue, _pii):
        # Same order as POST /chat/messages/: replay beats the refusal, and still
        # nothing runs — no placeholder work, no model, no enqueue, no new row.
        self._stored_turn()
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            resp = self.ask(client_msg_id="siri-old")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            (resp.data["answered"], resp.data["escalated"], resp.data["client_msg_id"]), (False, True, "siri-old")
        )
        self.assertEqual(resp.data["turn"]["status"], "ready")
        self.assertEqual(resp.data["turn"]["reply_text"], "An earlier answer.")
        self.assertNotIn("error", resp.data)
        self.assertIn("no-store", resp["Cache-Control"])
        self.snapshot.assert_not_called()
        self.intent.assert_not_called()
        completion.assert_not_called()
        enqueue.assert_not_called()
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 1)

    def test_unknown_or_invalid_client_msg_id_is_refused(self, completion, enqueue, _pii):
        self._stored_turn()
        for client_msg_id in ("siri-new", "x" * 65, "", "   "):
            with self.subTest(client_msg_id=client_msg_id), override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
                resp = self.ask(client_msg_id=client_msg_id)
                self.assertEqual(resp.data, {"answered": False, "escalated": False, "error": "subscription_required"})
        completion.assert_not_called()
        enqueue.assert_not_called()
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 1)

    def test_served_ask_does_no_replay_read(self, completion, enqueue, _pii):
        # The replay read lives in the refusal branch only: a served paywall=1
        # ask with a known id pays no extra query (it answers fast, as today).
        self._stored_turn()
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"),
            patch.object(siri_views.SiriRespondView, "_replay", wraps=siri_views.SiriRespondView._replay) as replay,
        ):
            resp = self.ask(header=PAYWALL_1, client_msg_id="siri-old")
        self.assertTrue(resp.data["answered"])
        replay.assert_not_called()

    def ask_as(self, tenant, header=PAYWALL_2, **extra):
        return self.client_for(tenant, header).post(
            "/api/v1/siri/respond/", {"intent": "what's due?", **extra}, format="json"
        )

    def test_replay_never_serves_another_tenants_turn(self, completion, enqueue, _pii):
        # client_msg_id is unique per tenant, not globally: tenant A's stored turn
        # under an id is invisible to tenant B refused with the same id.
        self._stored_turn("siri-shared")
        other = _make_tenant()
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            resp = self.ask_as(other, client_msg_id="siri-shared")
        self.assertEqual(resp.data, {"answered": False, "escalated": False, "error": "subscription_required"})
        self.assertNotIn(b"An earlier answer.", resp.content)
        self.assertEqual(AppChatMessage.objects.filter(tenant=other).count(), 0)
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 1)
        completion.assert_not_called()
        enqueue.assert_not_called()

    def test_refusal_and_replay_stay_with_their_tenant(self, completion, enqueue, _pii):
        # A refusal persists nothing under its id, so an active tenant escalating
        # with the same id gets a fresh turn of its own; and a stored turn of the
        # refused tenant never comes back to the active one either.
        completion.return_value = _completion(siri_views._ESCALATE_SENTINEL)
        self._stored_turn("siri-stored")
        active = _make_tenant(status=Tenant.Status.ACTIVE)
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            refused = self.ask(client_msg_id="siri-fresh")
            served = {cid: self.ask_as(active, client_msg_id=cid) for cid in ("siri-fresh", "siri-stored")}
        self.assertEqual(refused.data, {"answered": False, "escalated": False, "error": "subscription_required"})
        self.assertFalse(AppChatMessage.objects.filter(tenant=self.paused, client_msg_id="siri-fresh").exists())
        for cid, resp in served.items():
            with self.subTest(client_msg_id=cid):
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual((resp.data["answered"], resp.data["escalated"]), (False, True))
                self.assertEqual(resp.data["client_msg_id"], cid)
                self.assertNotIn("error", resp.data)
                self.assertEqual(resp.data["turn"]["reply_text"], "")
                self.assertTrue(AppChatMessage.objects.filter(tenant=active, client_msg_id=cid).exists())
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 1)

    def test_active_tenant_on_paywall2_with_flag_on_gets_a_normal_reply(self, completion, enqueue, _pii):
        active = _make_tenant(status=Tenant.Status.ACTIVE)
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"),
            self.assertNoLogs("apps.router.chat_gates", level="DEBUG"),
        ):
            resp = self.ask_as(active)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data, {"answered": True, "escalated": False, "text": "Two tasks are due."})
        self.snapshot.assert_called_once()
        completion.assert_called_once()
        enqueue.assert_not_called()


_SHAPE_BODY = {"client_msg_id": "6f1c1f0e-3d7c-4c55-9a51-1b2f7f0e9a01", "text": "how did I sleep"}


@override_settings(OPENROUTER_API_KEY="test-only", TEST_MODE=True)
class TalkAndShapePaywallTest(_PaywallClientMixin, TestCase):
    """The gate only applies where the feature is enabled for the tenant; a tenant
    outside the feature's allowlist keeps today's ``disabled`` answer either way."""

    def setUp(self):
        self.paused = _make_tenant()

    def talk(self, header=PAYWALL_2, **flags):
        ok = TalkRouteResponse(ack_kind="tasks", reason="ok")
        with (
            patch.object(talk_route_views, "route_talk", wraps=talk_route_views.route_talk) as route_talk,
            patch("apps.router.talk_route._route_talk", return_value=ok),
            override_settings(**flags),
        ):
            resp = self.client_for(self.paused, header).post(
                "/api/v1/chat/talk-route/", {"text": "log a run"}, format="json"
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = dict(resp.data)
        self.assertGreaterEqual(body.pop("latency_ms"), 0)
        return body, route_talk

    def shape(self, header=PAYWALL_2, **flags):
        served = ChatShapeResponse(reason="no_panel")
        with (
            patch.object(chat_shape_views, "shape_chat", return_value=served) as shape_chat,
            override_settings(**flags),
        ):
            resp = self.client_for(self.paused, header).post("/api/v1/chat/shape/", _SHAPE_BODY, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.data, shape_chat

    # -- talk-route --------------------------------------------------------

    def test_talk_route_degraded_without_model_call(self):
        body, route_talk = self.talk(IOS_PAYWALL_ENFORCE_TENANT_IDS="*", TALK_ROUTE_TENANT_IDS="*")
        self.assertEqual(body, {"ack_kind": "other", "quick_read": "none", "reason": "unavailable"})
        route_talk.assert_not_called()

    def test_talk_route_feature_off_keeps_disabled_answer(self):
        # Paywall flag on, feature off: the gate is never consulted (no log line).
        with self.assertNoLogs("apps.router.chat_gates", level="DEBUG"):
            body, route_talk = self.talk(IOS_PAYWALL_ENFORCE_TENANT_IDS="*", TALK_ROUTE_TENANT_IDS="")
        self.assertEqual(body, {"ack_kind": "other", "quick_read": "none", "reason": "disabled"})
        route_talk.assert_called_once()

    def test_talk_route_both_flags_off_keeps_disabled_answer(self):
        body, route_talk = self.talk(IOS_PAYWALL_ENFORCE_TENANT_IDS="", TALK_ROUTE_TENANT_IDS="")
        self.assertEqual(body, {"ack_kind": "other", "quick_read": "none", "reason": "disabled"})
        route_talk.assert_called_once()

    def test_talk_route_paywall1_served(self):
        body, route_talk = self.talk(PAYWALL_1, IOS_PAYWALL_ENFORCE_TENANT_IDS="*", TALK_ROUTE_TENANT_IDS="*")
        self.assertEqual(body, {"ack_kind": "tasks", "quick_read": "none", "reason": "ok"})
        route_talk.assert_called_once()

    def test_talk_route_paywall_flag_off_served(self):
        body, route_talk = self.talk(IOS_PAYWALL_ENFORCE_TENANT_IDS="", TALK_ROUTE_TENANT_IDS="*")
        self.assertEqual(body, {"ack_kind": "tasks", "quick_read": "none", "reason": "ok"})
        route_talk.assert_called_once()

    # -- chat shape --------------------------------------------------------

    def test_chat_shape_degraded_without_model_call(self):
        data, shape_chat = self.shape(IOS_PAYWALL_ENFORCE_TENANT_IDS="*", CHAT_SHAPE_TENANT_IDS="*")
        self.assertEqual(data["reason"], "unavailable")
        self.assertIs(data["enabled"], True)  # not latched off: the app asks again after payment
        self.assertIsNone(data["panel"])
        shape_chat.assert_not_called()

    def test_chat_shape_feature_off_keeps_disabled_answer(self):
        with (
            patch.object(chat_shape_views, "shape_chat", wraps=chat_shape_views.shape_chat) as shape_chat,
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*", CHAT_SHAPE_TENANT_IDS=""),
            self.assertNoLogs("apps.router.chat_gates", level="DEBUG"),
        ):
            resp = self.client_for(self.paused).post("/api/v1/chat/shape/", _SHAPE_BODY, format="json")
        self.assertEqual(resp.data["reason"], "disabled")
        self.assertIs(resp.data["enabled"], False)
        shape_chat.assert_called_once()

    def test_chat_shape_paywall1_served(self):
        data, shape_chat = self.shape(PAYWALL_1, IOS_PAYWALL_ENFORCE_TENANT_IDS="*", CHAT_SHAPE_TENANT_IDS="*")
        self.assertEqual((data["enabled"], data["reason"]), (True, "no_panel"))
        shape_chat.assert_called_once()

    def test_chat_shape_paywall_flag_off_served(self):
        data, shape_chat = self.shape(IOS_PAYWALL_ENFORCE_TENANT_IDS="", CHAT_SHAPE_TENANT_IDS="*")
        self.assertEqual((data["enabled"], data["reason"]), (True, "no_panel"))
        shape_chat.assert_called_once()

    def test_chat_shape_flag_off_feature_off_reaches_model_function(self):
        with (
            patch.object(chat_shape_views, "shape_chat", wraps=chat_shape_views.shape_chat) as shape_chat,
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="", CHAT_SHAPE_TENANT_IDS=""),
        ):
            resp = self.client_for(self.paused).post("/api/v1/chat/shape/", _SHAPE_BODY, format="json")
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

    def me(self, tenant, header=PAYWALL_2):
        resp = self.client_for(tenant, header).get("/api/v1/tenants/me/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp

    def paywall(self, tenant, header=PAYWALL_2):
        return self.me(tenant, header).data["subscription"]["paywall"]

    def test_true_only_in_the_refuse_case(self):
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)):
            first = self.me(self.paused)
            self.assertEqual(first["X-Cache"], "MISS")
            self.assertIs(first.data["subscription"]["paywall"], True)
            for header, expected in ((PAYWALL_1, False), (None, False), (DUPLICATED, False), (PAYWALL_2, True)):
                with self.subTest(header=header):
                    resp = self.me(self.paused, header)
                    self.assertEqual(resp["X-Cache"], "HIT")  # cached body, fresh value
                    self.assertIs(resp.data["subscription"]["paywall"], expected)
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

    def test_gate_failure_never_breaks_the_profile(self):
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"),
            patch("apps.router.chat_gates.ios_paywall_refuses", side_effect=RuntimeError("boom")),
            self.assertLogs("apps.tenants.views", level="ERROR") as logs,
        ):
            resp = self.me(self.paused)
        self.assertIs(resp.data["subscription"]["paywall"], False)
        self.assertEqual(resp.data["id"], str(self.paused.id))
        self.assertIn("paywall computation failed", logs.output[0])

    def test_paywall_flips_on_payment_while_body_is_still_cached(self):
        # The Apple row below is created directly (no refresh(), so no tag bump):
        # the cached body still says active=False, but paywall is computed fresh.
        # This is the documented up-to-60 s disagreement; the client treats
        # paywall=false + active=false as "unknown", never as "unpaid".
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"):
            before = self.me(self.paused)
            self.assertEqual(before["X-Cache"], "MISS")
            self.assertIs(before.data["subscription"]["paywall"], True)
            _apple(self.paused, entitles=True)
            after = self.me(self.paused)
        self.assertEqual(after["X-Cache"], "HIT")
        self.assertIs(after.data["subscription"]["paywall"], False)
        self.assertIs(after.data["subscription"]["active"], False)  # stale until the TTL or a bump

    def test_cached_body_never_carries_paywall_in_either_order(self):
        # The cache key (view, tenant, tag version, path, query) cannot see the
        # header, so the body is shared by every client of the tenant. paywall is
        # added after the cached read and never written into the cache: a web-built
        # body serves a paywall=2 client its true value, and the paywall=2 request
        # does not leave a true value behind for the next web/paywall=1 read.
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*"),
            patch("apps.common.cache.cache.set", wraps=cache.set) as cache_set,
        ):
            reads = [self.me(self.paused, header) for header in (None, PAYWALL_2, PAYWALL_1, PAYWALL_2)]
        self.assertEqual([r["X-Cache"] for r in reads], ["MISS", "HIT", "HIT", "HIT"])
        self.assertEqual([r.data["subscription"]["paywall"] for r in reads], [False, True, False, True])
        view_writes = [c for c in cache_set.call_args_list if str(c.args[0]).startswith("nbhd:view:")]
        self.assertEqual(len(view_writes), 1)
        self.assertNotIn("paywall", view_writes[0].args[1]["data"]["subscription"])

    def test_flag_off_paused_paywall2_is_false_with_no_added_query_or_log(self):
        self.me(_make_tenant(), header=None)  # warm per-process caches
        baseline_tenant, gated_tenant = _make_tenant(), _make_tenant()
        with (
            override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=""),
            patch("apps.router.chat_gates.ai_paused", wraps=ai_paused) as paused_check,
        ):
            with CaptureQueriesContext(connection) as baseline_queries:
                baseline = self.me(baseline_tenant, header=None)
            with (
                CaptureQueriesContext(connection) as gated_queries,
                self.assertNoLogs("apps.router.chat_gates", level="DEBUG"),
                self.assertNoLogs("apps.tenants.views", level="DEBUG"),
            ):
                gated = self.me(gated_tenant)
        self.assertEqual((baseline["X-Cache"], gated["X-Cache"]), ("MISS", "MISS"))
        self.assertIs(gated.data["subscription"]["paywall"], False)
        self.assertEqual(gated.data["subscription"], baseline.data["subscription"])
        self.assertEqual(len(gated_queries.captured_queries), len(baseline_queries.captured_queries))
        paused_check.assert_not_called()


# Headers that must never be refused, however the tenant and flag are set: no
# header (web), shipped builds, and anything that is not exactly a paywall>=2 iOS
# contract (wrong product, wrong case, zero, leading zero, repeated param, joined copies).
_NEVER_REFUSED = (
    None,
    PAYWALL_1,
    DUPLICATED,
    "ios/2.4.4/110; paywall=0",
    "ios/2.4.4/110; paywall=02",
    "ios/2.4.4/110; paywall=2; paywall=2",
    "iOS/2.4.4/110; paywall=2",
    "android/2.4.4/110; paywall=2",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
)


@override_settings(OPENROUTER_API_KEY="test-only", TEST_MODE=True, TALK_ROUTE_TENANT_IDS="*", CHAT_SHAPE_TENANT_IDS="*")
@patch("apps.pii.redactor._detect_pii", return_value=[])
@patch("apps.cron.publish.publish_task")
@patch("apps.router.chat_views.enqueue_message_for_tenant")
@patch("apps.common.openrouter.chat_completion", return_value=_completion("Two tasks are due."))
class HeaderFirstPerViewTest(_PaywallClientMixin, TestCase):
    """Each gated view, for a paused tenant the flag opens: a request that does not
    declare paywall>=2 is served, and the gate neither reads the flag nor runs
    ``ai_paused`` nor logs for it."""

    def setUp(self):
        cache.clear()  # throttles
        self.paused = _make_tenant()
        for target, attr, kwargs in (
            (siri_views, "_placeholder_snapshot", {"return_value": "STATE"}),
            (siri_views, "_placeholder_intent", {"side_effect": lambda tenant, text, ingress=None: text}),
            (chat_shape_views, "shape_chat", {"return_value": ChatShapeResponse(reason="no_panel")}),
        ):
            patcher = patch.object(target, attr, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "apps.router.talk_route._route_talk", return_value=TalkRouteResponse(ack_kind="tasks", reason="ok")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_served(self, endpoint: str, header: str | None, n: int) -> None:
        client = self.client_for(self.paused, header)
        if endpoint == "chat_messages":
            resp = client.post(
                "/api/v1/chat/messages/", {"text": "plan my week", "client_msg_id": f"hf-{n}"}, format="json"
            )
            self.assertEqual((resp.status_code, resp.data["status"]), (201, "pending"), resp.content)
        elif endpoint == "siri_respond":
            resp = client.post("/api/v1/siri/respond/", {"intent": "what's due?"}, format="json")
            self.assertEqual(resp.data, {"answered": True, "escalated": False, "text": "Two tasks are due."})
        elif endpoint == "talk_route":
            resp = client.post("/api/v1/chat/talk-route/", {"text": "log a run"}, format="json")
            self.assertEqual((resp.data["ack_kind"], resp.data["reason"]), ("tasks", "ok"))
        else:
            resp = client.post("/api/v1/chat/shape/", _SHAPE_BODY, format="json")
            self.assertEqual((resp.data["enabled"], resp.data["reason"]), (True, "no_panel"))
        self.assertNotIn(b"subscription_required", resp.content)

    def test_non_paywall2_requests_never_refused_on_any_gated_view(self, completion, enqueue, wake, _pii):
        n = 0
        for endpoint in ("chat_messages", "siri_respond", "talk_route", "chat_shape"):
            for header in _NEVER_REFUSED:
                n += 1
                with (
                    self.subTest(endpoint=endpoint, header=header),
                    override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)),
                    patch("apps.router.chat_gates.ai_paused", wraps=ai_paused) as paused_check,
                    patch("apps.router.chat_gates._tenant_allowed", wraps=_tenant_allowed) as flag_check,
                    self.assertNoLogs("apps.router.chat_gates", level="DEBUG"),
                ):
                    self.assert_served(endpoint, header, n)
                    paused_check.assert_not_called()
                    paywall_flag_reads = [
                        c for c in flag_check.call_args_list if c.args[1] == "IOS_PAYWALL_ENFORCE_TENANT_IDS"
                    ]
                    self.assertEqual(paywall_flag_reads, [])
        # The same tenant and flag with a paywall=2 header IS refused (the control).
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=str(self.paused.id)):
            resp = self.client_for(self.paused).post("/api/v1/siri/respond/", {"intent": "what's due?"}, format="json")
        self.assertEqual(resp.data, {"answered": False, "escalated": False, "error": "subscription_required"})


class PaywallFlagValueTest(TestCase):
    """How ``_tenant_allowed`` (shared by every rollout flag) reads odd values."""

    def test_malformed_flag_values(self):
        tenant = _make_tenant()
        tid = str(tenant.id)
        other = "00000000-0000-0000-0000-000000000001"
        cases = [
            ("", False),
            ("garbage", False),
            ("*x", False),  # only a lone "*" token means everyone
            ("**", False),
            ("x*", False),
            (" * ", True),  # tokens are trimmed
            ("garbage,*", True),  # any lone "*" token opens every tenant
            (f"{tid},", True),
            (f",{tid},,", True),
            (f" {tid} ", True),
            (tid.upper(), True),  # uuid.UUID is case-insensitive
            (tid.replace("-", ""), True),  # and accepts the 32-hex form
            (f"{{{tid}}}", True),  # and braces
            (f"urn:uuid:{tid}", True),  # and the URN form
            (f"{other},garbage", False),
            (f"{tid}x", False),
            (tid[:-1], False),
        ]
        for value, expected in cases:
            with self.subTest(value=value), override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=value):
                self.assertIs(_tenant_allowed(tenant, "IOS_PAYWALL_ENFORCE_TENANT_IDS"), expected)


@override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*")
@patch("apps.pii.redactor._detect_pii", return_value=[])
@patch("apps.cron.publish.publish_task")
@patch("apps.router.chat_views.enqueue_message_for_tenant")
@patch("apps.router.conversation_capture.schedule_user_md_refresh")
class FreeTierSyncTest(_PaywallClientMixin, TestCase):
    """The free tier's lifecycle for one turn: the chat POST is refused, the client
    answers on the phone and records the pair under the SAME client_msg_id via
    ``/chat/turns/``. The refusal persists nothing, so the record is one fresh pair,
    a replayed record is idempotent, and nothing after acceptance ever reports
    ``subscription_required``."""

    def setUp(self):
        cache.clear()  # throttles
        self.paused = _make_tenant()

    def chat(self, client_msg_id="ft-1"):
        return self.client_for(self.paused).post(
            "/api/v1/chat/messages/", {"text": "plan my week", "client_msg_id": client_msg_id}, format="json"
        )

    def sync(self, client_msg_id="ft-1"):
        return self.client_for(self.paused).post(
            "/api/v1/chat/turns/",
            {
                "text": "plan my week",
                "reply_text": "Here is a plan.",
                "client_msg_id": client_msg_id,
                "engine": "apple_on_device",
            },
            format="json",
        )

    def test_refused_then_synced_is_one_pair_and_a_replayed_sync_is_idempotent(self, refresh, enqueue, wake, _pii):
        refused = self.chat()
        self.assertEqual(refused.data, {"client_msg_id": "ft-1", **SUBSCRIPTION_REQUIRED})
        self.assertFalse(AppChatMessage.objects.filter(tenant=self.paused).exists())

        first = self.sync()
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(
            (first.data["status"], first.data["source"], first.data["user_text"], first.data["reply_text"]),
            ("ready", "on_device", "plan my week", "Here is a plan."),
        )
        rows = AppChatMessage.objects.filter(tenant=self.paused)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual((row.client_msg_id, row.source), ("ft-1", AppChatMessage.Source.ON_DEVICE))

        replay = self.sync()
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 1)
        refresh.assert_called_once()  # the replay schedules no second USER.md push

        # A late retry of the original chat POST replays the recorded pair; it is
        # neither refused again nor enqueued.
        retry = self.chat()
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual((retry.data["source"], retry.data["reply_text"]), ("on_device", "Here is a plan."))
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 1)
        enqueue.assert_not_called()
        wake.assert_not_called()

    def test_poll_of_an_accepted_turn_never_reports_subscription_required(self, refresh, enqueue, wake, _pii):
        # Accepted while the flag was off; the flag then opens the tenant.
        with override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS=""):
            accepted = self.chat("ft-acc")
        self.assertEqual((accepted.status_code, accepted.data["status"]), (201, "pending"), accepted.content)

        poll = self.client_for(self.paused).get("/api/v1/chat/messages/ft-acc/")
        self.assertEqual(poll.status_code, 200, poll.content)
        self.assertEqual((poll.data["status"], poll.data["error"]), ("pending", ""))
        self.assertNotIn(b"subscription_required", poll.content)

        retry = self.chat("ft-acc")
        self.assertEqual((retry.status_code, retry.data["status"]), (200, "pending"), retry.content)
        self.assertNotIn(b"subscription_required", retry.content)
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.paused).count(), 1)


@override_settings(IOS_PAYWALL_ENFORCE_TENANT_IDS="*")
@patch("apps.pii.redactor._detect_pii", return_value=[])
@patch("apps.cron.publish.publish_task")
class FreeTierAccountEndpointsTest(_PaywallClientMixin, TestCase):
    """Every account endpoint the free tier's on-device tools call (nbhd-ios
    ``OnDeviceToolProfile.privateConnected``) plus the on-device digest read, for a
    paused tenant the flag opens on a paywall=2 build. Product rule: only AI is
    gated, so every one of these is served."""

    def setUp(self):
        cache.clear()  # throttles
        self.paused = _make_tenant()
        self.client = self.client_for(self.paused)
        self.today = timezone.localdate().isoformat()

    def ok(self, method: str, path: str, body: dict | None = None, expected: int = 200):
        resp = getattr(self.client, method)(path, body or {}, format="json")
        self.assertEqual(resp.status_code, expected, f"{method.upper()} {path}: {resp.content[:300]!r}")
        self.assertNotIn(b"subscription_required", resp.content)
        return resp

    def test_free_tier_tool_endpoints_are_served(self, wake, _pii):
        self.ok("get", "/api/v1/journal/")
        self.ok("post", "/api/v1/journal/", {"date": self.today, "mood": "good", "energy": "medium"}, 201)
        self.ok("get", "/api/v1/journal/documents/")
        self.ok("get", "/api/v1/journal/documents/memory/memory/")

        self.ok("get", "/api/v1/finance/accounts/")
        account = self.ok(
            "post",
            "/api/v1/finance/accounts/",
            {"nickname": "Visa", "account_type": "credit_card", "current_balance": "100.00"},
            201,
        )
        self.ok(
            "post",
            "/api/v1/finance/transactions/",
            {"account": account.data["id"], "transaction_type": "payment", "amount": "10.00", "date": self.today},
            201,
        )

        self.ok("get", "/api/v1/fuel/workouts/")
        # A done workout also schedules the agent-authored congrats cron (an AI turn
        # when it fires). For a billing-paused tenant ``hibernated_at`` is None, so
        # congrats' hibernation gate does not stop it; only the scaled-to-zero
        # container makes the gateway push fail and roll back. Pinned as-is (an open
        # finding on PR #1832), with the slow gateway push stubbed out.
        with patch("apps.fuel.congrats._dispatch_schedule") as congrats:
            self.ok(
                "post",
                "/api/v1/fuel/workouts/",
                {"activity": "Run", "category": "cardio", "date": self.today, "status": "done"},
                201,
            )
        congrats.assert_called_once()
        # No profile yet: the view's own 404, not a gate.
        profile = self.client.get("/api/v1/fuel/profile/")
        self.assertIn(profile.status_code, (200, 404), profile.content)
        self.assertNotIn(b"subscription_required", profile.content)

        self.ok("get", "/api/v1/insights/insights/")
        self.ok(
            "post",
            "/api/v1/insights/insights/record/",
            {"pillar": "gravity", "topic": "saving", "statement": "Saves on payday."},
            201,
        )

        self.ok("get", "/api/v1/journal/tasks/")
        task = self.ok("post", "/api/v1/journal/tasks/", {"title": "Call the bank"}, 201)
        self.ok("post", f"/api/v1/journal/tasks/{task.data['id']}/complete/")

        self.ok("get", "/api/v1/journal/goals/")
        goal = self.ok("post", "/api/v1/journal/goals/", {"title": "Run a 10k"}, 201)
        self.ok("post", f"/api/v1/journal/goals/{goal.data['id']}/achieve/")

        context = self.ok("get", "/api/v1/chat/context/")
        self.assertIsInstance(context.data["context_md"], str)

        # None of it wakes the paused tenant's container.
        wake.assert_not_called()

    def test_prewarm_is_a_no_op_for_a_paused_tenant(self, wake, _pii):
        Tenant.objects.filter(pk=self.paused.pk).update(hibernated_at=timezone.now() - timedelta(hours=1))
        resp = self.client_for(self.paused).post("/api/v1/chat/prewarm/")
        self.assertEqual((resp.status_code, resp.data), (200, {"state": "paused"}))
        wake.assert_not_called()
