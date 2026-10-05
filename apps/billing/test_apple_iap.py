"""Apple in-app subscriptions (DIRECTIVE_ios_in_app_purchase.md, phase A).

Apple is faked at the two seams `apple_iap` talks through: `_verify` (signed-payload
verification) and `_client` (App Store Server API). A "signed" payload in these tests
is JSON describing what Apple's real JWS would decode to.
"""

import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing import apple_iap, services
from apps.billing.entitlement import billing_source, is_paying, paying_q
from apps.billing.models import AppStoreNotification, AppStoreSubscription
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

DELETE_URL = "/api/v1/tenants/delete-account/"

CONFIGURED = {
    "APPLE_IAP_ISSUER_ID": "issuer",
    "APPLE_IAP_KEY_ID": "key",
    "APPLE_IAP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    "APPLE_IAP_BUNDLE_ID": "org.hoodunited.nbhd",
    "APPLE_IAP_PRODUCT_IDS": ["org.hoodunited.nbhd.standard.monthly"],
}
PRODUCT = "org.hoodunited.nbhd.standard.monthly"


def signed(**fields) -> str:
    return json.dumps(fields)


class FakeApple:
    """Holds Apple's 'truth' per original transaction id and records API calls."""

    def __init__(self):
        self.status = {}  # original id -> (code, txn fields, renewal fields)
        self.tokens_set = []
        self.api_error = None  # an exception get_all_subscription_statuses raises

    def set(self, original_id, code, *, env="Production", token="", expires_in_days=30, auto_renew=1, signed_at=None):
        txn = {
            "kind": "transaction",
            "env": env,
            "originalTransactionId": original_id,
            "productId": PRODUCT,
            "expiresDate": int((timezone.now() + timedelta(days=expires_in_days)).timestamp() * 1000),
            "appAccountToken": token,
            "signedDate": int((signed_at or timezone.now()).timestamp() * 1000),
        }
        renewal = {"kind": "renewal", "env": env, "rawAutoRenewStatus": auto_renew}
        self.status[original_id] = (code, txn, renewal)

    # ── seams ──
    def verify(self, kind, value):
        data = json.loads(value)
        if data.get("bad"):
            raise apple_iap.AppleVerificationError("bad signature")
        return SimpleNamespace(**{k: v for k, v in data.items() if k not in {"kind", "env"}}), data["env"]

    def client(self, env_name):
        fake = self

        class _Client:
            def get_all_subscription_statuses(self, original_id):
                if fake.api_error is not None:
                    raise fake.api_error
                code, txn, renewal = fake.status[original_id]
                item = SimpleNamespace(
                    rawStatus=code,
                    originalTransactionId=original_id,
                    signedTransactionInfo=json.dumps(txn),
                    signedRenewalInfo=json.dumps(renewal),
                )
                return SimpleNamespace(data=[SimpleNamespace(lastTransactions=[item])])

            def set_app_account_token(self, original_id, request):
                fake.tokens_set.append((original_id, request.appAccountToken))

        return _Client()


@override_settings(**CONFIGURED, NBHD_DISABLE_BACKGROUND_THREADS=True)
class AppleIAPTests(TestCase):
    def setUp(self):
        self.apple = FakeApple()
        self.tenant = create_tenant(display_name="Aya", telegram_chat_id=990001)
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.container_id = "oc-aya"
        self.tenant.is_trial = False
        self.tenant.save()
        patches = [
            patch.object(apple_iap, "_verify", side_effect=self.apple.verify),
            patch.object(apple_iap, "_client", side_effect=self.apple.client),
            patch.object(services, "restore_tenant_runtime", return_value=True),
            patch("apps.cron.publish.publish_task"),
            patch("apps.orchestrator.azure_client.scale_container_app"),
            patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0}),
            patch("apps.cron.views._send_alert_via_pushover", return_value="delivered"),
        ]
        self.mocks = [p.start() for p in patches]
        self.publish_task, self.alert = self.mocks[3], self.mocks[6]
        for p in patches:
            self.addCleanup(p.stop)

    def txn(self, original_id, *, env="Production", token=None):
        token = str(self.tenant.user_id) if token is None else token
        return signed(kind="transaction", env=env, originalTransactionId=original_id, appAccountToken=token)

    def fresh(self):
        return Tenant.objects.get(id=self.tenant.id)

    def client_for(self, tenant):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(tenant.user).access_token}")
        return client

    # ── buying ────────────────────────────────────────────────────────────

    def test_a_purchase_from_the_app_makes_a_paused_account_pay_and_wakes_it(self):
        self.apple.set("1000", 1, token=str(self.tenant.user_id))
        response = self.client_for(self.tenant).post(
            "/api/v1/billing/apple/sync/", {"signed_transaction": self.txn("1000")}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["subscription"]
        self.assertEqual((body["active"], body["source"], body["status"]), (True, "apple", "active"))
        tenant = self.fresh()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertTrue(is_paying(tenant))
        self.assertTrue(tenant.has_entitlement)
        self.assertEqual(billing_source(tenant), "apple")
        self.assertIn(tenant, Tenant.objects.filter(paying_q()))

    def test_a_purchase_without_a_token_is_bound_to_the_signed_in_user_and_told_to_apple(self):
        self.apple.set("1001", 1, token="")
        apple_iap.sync_from_app(self.tenant, self.txn("1001", token=""))
        sub = AppStoreSubscription.objects.get(original_transaction_id="1001")
        self.assertEqual(sub.tenant_id, self.tenant.id)
        self.assertEqual(self.apple.tokens_set, [("1001", str(self.tenant.user_id))])

    def test_a_purchase_bound_to_another_account_is_refused_politely(self):
        other = create_tenant(display_name="Ben", telegram_chat_id=990002)
        self.apple.set("1002", 1, token=str(other.user_id))
        response = self.client_for(self.tenant).post(
            "/api/v1/billing/apple/sync/",
            {"signed_transaction": self.txn("1002", token=str(other.user_id))},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "other_account")
        self.assertFalse(is_paying(self.fresh()))

    def test_a_returning_user_whose_assistant_was_deleted_gets_a_new_one(self):
        self.tenant.status = Tenant.Status.DELETED
        self.tenant.container_id = ""
        self.tenant.save()
        self.apple.set("1003", 1, token=str(self.tenant.user_id))
        apple_iap.sync_from_app(self.tenant, self.txn("1003"))
        self.assertEqual(self.fresh().status, Tenant.Status.PROVISIONING)
        self.assertEqual(self.publish_task.call_args[0][:2], ("provision_tenant", str(self.tenant.id)))

    # ── Apple's status is the truth ───────────────────────────────────────

    def test_expired_pauses_grace_keeps_and_billing_retry_does_not_entitle(self):
        self.apple.set("1004", 1, token=str(self.tenant.user_id))
        apple_iap.sync_from_app(self.tenant, self.txn("1004"))
        self.apple.set("1004", 4, token=str(self.tenant.user_id))  # grace
        apple_iap.refresh("1004", "Production")
        self.assertEqual(self.fresh().status, Tenant.Status.ACTIVE)
        self.apple.set("1004", 3, token=str(self.tenant.user_id))  # billing retry, no grace
        apple_iap.refresh("1004", "Production")
        tenant = self.fresh()
        self.assertEqual(tenant.status, Tenant.Status.SUSPENDED)
        self.assertIsNone(tenant.hibernated_at)  # billing pause: the queue can't wake it
        self.apple.set("1004", 1, token=str(self.tenant.user_id))  # renewal recovered
        apple_iap.refresh("1004", "Production")
        self.assertEqual(self.fresh().status, Tenant.Status.ACTIVE)

    def test_a_late_failure_notification_cannot_pause_someone_apple_says_is_paying(self):
        self.apple.set("1005", 1, token=str(self.tenant.user_id))
        apple_iap.sync_from_app(self.tenant, self.txn("1005"))
        note = signed(
            kind="notification",
            env="Production",
            notificationUUID="n-late",
            rawNotificationType="DID_FAIL_TO_RENEW",
            rawSubtype="",
            signedDate=1,
            data=None,
        )
        with patch.object(apple_iap, "_verify", side_effect=self._notification_verify("1005")):
            apple_iap.handle_notification(note)
        self.assertEqual(self.fresh().status, Tenant.Status.ACTIVE)

    def _notification_verify(self, original_id, env="Production"):
        def verify(kind, value):
            data = json.loads(value)
            if kind == "notification":
                payload = SimpleNamespace(
                    notificationUUID=data["notificationUUID"],
                    rawNotificationType=data["rawNotificationType"],
                    rawSubtype=data.get("rawSubtype", ""),
                    signedDate=data.get("signedDate"),
                    data=SimpleNamespace(signedTransactionInfo=self.txn(original_id, env=env)) if original_id else None,
                )
                return payload, env
            return self.apple.verify(kind, value)

        return verify

    def test_notifications_are_answered_once_including_test_and_unknown_types(self):
        client = APIClient()
        for uuid_, kind, original in [("n-test", "TEST", None), ("n-new", "SOMETHING_NEW", "1006")]:
            if original:
                self.apple.set(original, 1, token=str(self.tenant.user_id))
            body = signed(kind="notification", notificationUUID=uuid_, rawNotificationType=kind)
            with patch.object(apple_iap, "_verify", side_effect=self._notification_verify(original)):
                first = client.post("/api/v1/billing/apple/notifications/", {"signedPayload": body}, format="json")
                again = client.post("/api/v1/billing/apple/notifications/", {"signedPayload": body}, format="json")
            self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual(AppStoreNotification.objects.get(notification_uuid="n-test").outcome, "test")
        self.assertEqual(AppStoreNotification.objects.get(notification_uuid="n-new").outcome, "applied")
        self.assertEqual(AppStoreNotification.objects.count(), 2)

    def test_a_bad_signature_is_refused_and_unconfigured_answers_503(self):
        client = APIClient()
        bad = client.post("/api/v1/billing/apple/notifications/", {"signedPayload": signed(bad=True)}, format="json")
        self.assertEqual(bad.status_code, 400)
        with override_settings(APPLE_IAP_ISSUER_ID=""):
            off = client.post("/api/v1/billing/apple/notifications/", {"signedPayload": "x"}, format="json")
        self.assertEqual(off.status_code, 503)

    def test_a_notification_for_a_deleted_account_is_answered_and_kept_unbound(self):
        self.apple.set("1007", 1, token="")  # nobody we know
        body = signed(kind="notification", notificationUUID="n-orphan", rawNotificationType="DID_RENEW")
        with patch.object(apple_iap, "_verify", side_effect=self._notification_verify("1007")):
            # the app-side token in the transaction is empty too
            self.apple.status["1007"][1]["appAccountToken"] = ""
            response = APIClient().post("/api/v1/billing/apple/notifications/", {"signedPayload": body}, format="json")
        self.assertEqual(response.status_code, 200)
        sub = AppStoreSubscription.objects.get(original_transaction_id="1007")
        self.assertIsNone(sub.tenant_id)
        self.assertFalse(sub.entitles)

    # ── sandbox ───────────────────────────────────────────────────────────

    def test_sandbox_purchases_entitle_only_allow_listed_people(self):
        self.apple.set("2000", 1, env="Sandbox", token=str(self.tenant.user_id))
        apple_iap.sync_from_app(self.tenant, self.txn("2000", env="Sandbox"))
        sub = AppStoreSubscription.objects.get(original_transaction_id="2000")
        self.assertEqual(sub.environment, "Sandbox")
        self.assertFalse(sub.entitles)
        self.assertFalse(is_paying(self.fresh()))
        with override_settings(APPLE_IAP_SANDBOX_USER_IDS=[str(self.tenant.user_id)]):
            apple_iap.refresh("2000", "Sandbox")
            self.assertTrue(is_paying(self.fresh()))

    # ── Stripe and Apple together ─────────────────────────────────────────

    def test_switching_from_stripe_to_apple_never_deletes_the_assistant(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.stripe_subscription_id = "sub_old"
        self.tenant.save()
        self.apple.set("3000", 1, token=str(self.tenant.user_id))
        live = {"status": "active", "cancel_at_period_end": False}
        with patch("stripe.Subscription.retrieve", return_value=live), patch("stripe.Subscription.modify"):
            apple_iap.sync_from_app(self.tenant, self.txn("3000"))
        services.handle_subscription_deleted(
            {"id": "sub_old", "customer": "", "metadata": {"user_id": str(self.tenant.user_id)}}
        )
        tenant = self.fresh()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)  # no DEPROVISIONING
        self.assertEqual(tenant.stripe_subscription_id, "")
        self.assertIsNotNone(tenant.stripe_subscription_ended_at)
        self.assertNotIn("deprovision_tenant", [c[0][0] for c in self.publish_task.call_args_list])

    def test_double_billing_keeps_the_apple_purchase_and_ends_stripe(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.stripe_subscription_id = "sub_live"
        self.tenant.save()
        self.apple.set("3001", 1, token=str(self.tenant.user_id))
        live = {"status": "active", "cancel_at_period_end": False}
        with patch("stripe.Subscription.retrieve", return_value=live), patch("stripe.Subscription.modify") as modify:
            apple_iap.sync_from_app(self.tenant, self.txn("3001"))
        modify.assert_called_once_with("sub_live", cancel_at_period_end=True)
        self.alert.assert_called_once()
        self.assertTrue(AppStoreSubscription.objects.get(original_transaction_id="3001").entitles)

    def test_stripe_giving_up_never_pauses_an_apple_payer(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save()
        self.apple.set("3002", 1, token=str(self.tenant.user_id))
        apple_iap.sync_from_app(self.tenant, self.txn("3002"))
        Tenant.objects.filter(id=self.tenant.id).update(stripe_subscription_id="sub_dying")
        services.handle_invoice_payment_failed(
            {
                "id": "in_1",
                "subscription": "sub_dying",
                "customer": "",
                "next_payment_attempt": None,
                "metadata": {"user_id": str(self.tenant.user_id)},
            }
        )
        tenant = self.fresh()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertEqual(tenant.stripe_subscription_id, "")

    # ── the daily sweep ───────────────────────────────────────────────────

    def test_the_daily_sweep_asks_apple_before_pausing(self):
        from apps.cron.views import _suspend_unentitled_tenant, _unentitled_active_tenants

        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save()
        self.apple.set("4000", 1, token=str(self.tenant.user_id))
        apple_iap.sync_from_app(self.tenant, self.txn("4000"))
        # our stored copy went stale (a renewal notification is late) …
        AppStoreSubscription.objects.filter(original_transaction_id="4000").update(entitles=False, status="expired")
        self.assertIn(self.fresh(), list(_unentitled_active_tenants()))
        # … but Apple says paying, so the sweep leaves them alone.
        result = _suspend_unentitled_tenant(self.fresh())
        self.assertEqual(result.get("skipped"), "apple")
        self.assertEqual(self.fresh().status, Tenant.Status.ACTIVE)

    # ── deletion ──────────────────────────────────────────────────────────

    def test_deleting_an_account_that_pays_through_apple_says_billing_continues(self):
        self.apple.set("5000", 1, token=str(self.tenant.user_id))
        apple_iap.sync_from_app(self.tenant, self.txn("5000"))
        with patch("apps.tenants.views._do_hard_delete"):
            response = self.client_for(self.tenant).post(
                "/api/v1/tenants/delete-account/", {"confirm": "DELETE"}, format="json"
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["apple_subscription_active"])
        self.assertIn("App Store subscription keeps billing", response.json()["detail"])

    # ── review round 1 ────────────────────────────────────────────────────

    def _active(self, original_id):
        self.apple.set(original_id, 1, token=str(self.tenant.user_id))
        return apple_iap.sync_from_app(self.tenant, self.txn(original_id))

    def test_an_older_answer_never_overwrites_a_newer_one(self):
        self._active("6000")
        older = timezone.now() - timedelta(hours=1)
        self.apple.set("6000", 2, token=str(self.tenant.user_id), signed_at=older)  # stale "expired"
        apple_iap.refresh("6000", "Production")
        self.assertTrue(AppStoreSubscription.objects.get(original_transaction_id="6000").entitles)
        self.assertEqual(self.fresh().status, Tenant.Status.ACTIVE)

    def test_expired_and_revoked_stop_the_plan(self):
        for code, label in [(2, "expired"), (5, "revoked")]:
            original = f"6{code}00"
            self._active(original)
            self.apple.set(original, code, token=str(self.tenant.user_id))
            apple_iap.refresh(original, "Production")
            sub = AppStoreSubscription.objects.get(original_transaction_id=original)
            self.assertEqual((sub.status, sub.entitles), (label, False))
        self.assertEqual(self.fresh().status, Tenant.Status.SUSPENDED)

    def test_a_key_problem_is_retried_not_swallowed(self):
        from appstoreserverlibrary.api_client import APIException

        self._active("6100")
        self.apple.api_error = APIException(401)
        with self.assertRaises(apple_iap.TransientAppleError):
            apple_iap.refresh("6100", "Production")
        body = signed(kind="notification", notificationUUID="n-401", rawNotificationType="EXPIRED")
        with patch.object(apple_iap, "_verify", side_effect=self._notification_verify("6100")):
            response = APIClient().post("/api/v1/billing/apple/notifications/", {"signedPayload": body}, format="json")
        self.assertEqual(response.status_code, 503)  # Apple will send it again
        self.assertEqual(AppStoreNotification.objects.get(notification_uuid="n-401").outcome, "retry")

    def test_restore_after_deleting_and_signing_up_again_moves_the_purchase(self):
        old = create_tenant(display_name="Old me", telegram_chat_id=990010)
        self.apple.set("6200", 1, token=str(old.user_id))
        apple_iap.sync_from_app(old, self.txn("6200", token=str(old.user_id)))
        old.user.delete()  # account deleted; Apple keeps billing
        sub = AppStoreSubscription.objects.get(original_transaction_id="6200")
        self.assertIsNone(sub.tenant_id)
        apple_iap.sync_from_app(self.tenant, self.txn("6200", token=str(old.user_id)))
        sub.refresh_from_db()
        self.assertEqual(sub.tenant_id, self.tenant.id)
        self.assertTrue(sub.entitles)
        self.assertEqual(self.fresh().status, Tenant.Status.ACTIVE)
        self.assertIn(("6200", str(self.tenant.user_id)), self.apple.tokens_set)

    def test_an_activation_that_fails_is_redone_on_the_next_try(self):
        self.tenant.status = Tenant.Status.DELETED
        self.tenant.container_id = ""
        self.tenant.save()
        self.apple.set("6300", 1, token=str(self.tenant.user_id))
        self.publish_task.side_effect = RuntimeError("QStash down")
        response = self.client_for(self.tenant).post(
            "/api/v1/billing/apple/sync/", {"signed_transaction": self.txn("6300")}, format="json"
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.fresh().status, Tenant.Status.DELETED)  # put back, not stuck in PROVISIONING
        self.publish_task.side_effect = None
        apple_iap.sync_from_app(self.tenant, self.txn("6300"))
        self.assertEqual(self.fresh().status, Tenant.Status.PROVISIONING)

    def test_a_wake_that_fails_leaves_the_assistant_wakeable(self):
        self.mocks[2].return_value = False  # restore_tenant_runtime
        self._active("6400")
        tenant = self.fresh()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertIsNotNone(tenant.hibernated_at)

    def test_a_tenant_being_torn_down_is_activated_later_not_now(self):
        self.tenant.status = Tenant.Status.DEPROVISIONING
        self.tenant.save()
        self.apple.set("6500", 1, token=str(self.tenant.user_id))
        with self.assertRaises(apple_iap.TransientAppleError):
            apple_iap.sync_from_app(self.tenant, self.txn("6500"))
        self.assertEqual(self.fresh().status, Tenant.Status.DEPROVISIONING)

    def test_several_app_store_rows_never_duplicate_a_tenant(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.is_budget_exempt = True
        self.tenant.save()
        self._active("6600")
        with override_settings(APPLE_IAP_SANDBOX_USER_IDS=[str(self.tenant.user_id)]):
            self.apple.set("6601", 1, env="Sandbox", token=str(self.tenant.user_id))
            apple_iap.sync_from_app(self.tenant, self.txn("6601", env="Sandbox"))
        ids = list(Tenant.entitled_active().filter(id=self.tenant.id).values_list("id", flat=True))
        self.assertEqual(len(ids), 1)

    def test_paying_on_the_iphone_cancels_a_scheduled_deletion_and_stripe_end_never_hard_deletes(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.pending_deletion = True
        self.tenant.stripe_subscription_id = "sub_leaving"
        self.tenant.save()
        ending = {"status": "active", "cancel_at_period_end": True}
        with patch("stripe.Subscription.retrieve", return_value=ending):
            self._active("6700")
        self.assertFalse(self.fresh().pending_deletion)
        Tenant.objects.filter(id=self.tenant.id).update(pending_deletion=True)  # e.g. set again by a race
        with patch("apps.tenants.views._do_hard_delete") as hard_delete:
            services.handle_subscription_deleted(
                {"id": "sub_leaving", "customer": "", "metadata": {"user_id": str(self.tenant.user_id)}}
            )
        hard_delete.assert_not_called()

    def test_a_dead_stripe_id_is_cleared_quietly_not_alerted(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.stripe_subscription_id = "sub_dead"
        self.tenant.save()
        with patch("stripe.Subscription.retrieve", return_value={"status": "canceled"}):
            self._active("6800")
        self.assertEqual(self.fresh().stripe_subscription_id, "")
        self.alert.assert_not_called()

    def test_the_daily_recheck_catches_a_lost_expiry(self):
        self._active("6900")
        AppStoreSubscription.objects.filter(original_transaction_id="6900").update(
            expires_at=timezone.now() - timedelta(days=3)
        )
        self.apple.set("6900", 2, token=str(self.tenant.user_id))
        result = apple_iap.recheck_lapsed()
        self.assertEqual(result["checked"], 1)
        self.assertFalse(AppStoreSubscription.objects.get(original_transaction_id="6900").entitles)
        self.assertEqual(self.fresh().status, Tenant.Status.SUSPENDED)

    def test_a_purchase_bound_elsewhere_is_still_recorded(self):
        other = create_tenant(display_name="Ben", telegram_chat_id=990011)
        self.apple.set("6950", 1, token=str(other.user_id))
        with self.assertRaises(apple_iap.AccountMismatch):
            apple_iap.sync_from_app(self.tenant, self.txn("6950", token=str(other.user_id)))
        self.assertTrue(AppStoreSubscription.objects.filter(original_transaction_id="6950").exists())

    def test_the_library_parses_apples_real_shapes(self):
        """Guards the attribute names the code reads against library drift."""
        from appstoreserverlibrary.models.LibraryUtility import _get_cattrs_converter
        from appstoreserverlibrary.models.StatusResponse import StatusResponse

        raw = {
            "environment": "Sandbox",
            "data": [
                {
                    "subscriptionGroupIdentifier": "g",
                    "lastTransactions": [
                        {
                            "status": 4,
                            "originalTransactionId": "x1",
                            "signedTransactionInfo": "a",
                            "signedRenewalInfo": "b",
                        }
                    ],
                }
            ],
        }
        parsed = _get_cattrs_converter(StatusResponse).structure(raw, StatusResponse)
        item = parsed.data[0].lastTransactions[0]
        self.assertEqual((item.rawStatus, item.originalTransactionId, item.signedTransactionInfo), (4, "x1", "a"))

    # ── review round 2 ────────────────────────────────────────────────────

    def test_a_running_trial_user_who_buys_stops_being_a_trial(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.is_trial = True
        self.tenant.trial_ends_at = timezone.now() + timedelta(days=10)
        self.tenant.save()
        self._active("7000")
        self.assertFalse(self.fresh().is_trial)
        self.assertEqual(self.fresh().status, Tenant.Status.ACTIVE)

    def test_clean_up_reads_real_stripe_objects(self):
        import stripe
        from django.core.management import call_command

        self.tenant.stripe_subscription_id = "sub_gone"
        self.tenant.save()
        real = stripe.Subscription.construct_from({"id": "sub_gone", "status": "canceled"}, "sk_test")
        with patch("stripe.Subscription.retrieve", return_value=real):
            call_command("clear_ended_stripe_subscriptions", "--apply", stdout=io.StringIO())
        self.assertEqual(self.fresh().stripe_subscription_id, "")

    def test_double_billing_reads_real_stripe_objects(self):
        import stripe

        self.tenant.stripe_subscription_id = "sub_live"
        self.tenant.save()
        real = stripe.Subscription.construct_from(
            {"id": "sub_live", "status": "active", "cancel_at_period_end": False}, "sk_test"
        )
        with patch("stripe.Subscription.retrieve", return_value=real), patch("stripe.Subscription.modify") as modify:
            self._active("7100")
        modify.assert_called_once_with("sub_live", cancel_at_period_end=True)

    def test_deleting_an_account_that_pays_apple_and_stripe_is_immediate(self):
        self._active("7200")
        self.tenant.refresh_from_db()
        Tenant.objects.filter(id=self.tenant.id).update(stripe_subscription_id="sub_both")
        client = self.client_for(self.tenant)
        with (
            patch("stripe.Subscription.cancel") as cancel,
            patch("stripe.Subscription.modify") as modify,
            patch("apps.tenants.views._do_hard_delete") as hard_delete,  # needs no outer transaction
        ):
            response = client.post(DELETE_URL, {"confirm": "DELETE"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["scheduled"])
        self.assertTrue(response.json()["apple_subscription_active"])
        cancel.assert_called_once_with("sub_both")
        modify.assert_not_called()
        hard_delete.assert_called_once()
        self.assertFalse(self.fresh().pending_deletion)

    def test_a_skipped_scheduled_deletion_tells_mj(self):
        self._active("7300")
        Tenant.objects.filter(id=self.tenant.id).update(pending_deletion=True, stripe_subscription_id="sub_x")
        services.handle_subscription_deleted(
            {"id": "sub_x", "customer": "", "metadata": {"user_id": str(self.tenant.user_id)}}
        )
        self.assertTrue(any("skipped scheduled deletion" in c.args[0] for c in self.alert.call_args_list))

    def test_a_trial_user_whose_assistant_is_still_being_built_stops_being_a_trial(self):
        self.tenant.status = Tenant.Status.PROVISIONING
        self.tenant.container_id = ""
        self.tenant.is_trial = True
        self.tenant.save()
        self._active("7400")
        self.assertFalse(self.fresh().is_trial)
        self.assertEqual(self.fresh().status, Tenant.Status.PROVISIONING)
        self.publish_task.assert_not_called()

    # ── plan screen (iOS phase C review) ─────────────────────────────────────

    def test_the_plan_screen_learns_a_card_problem_and_a_lapse(self):
        from apps.common.cache import get_tag_version

        self._active("7500")
        before = get_tag_version(self.tenant.id, "tenant")
        self.apple.set("7500", 3, token=str(self.tenant.user_id))  # billing retry
        apple_iap.refresh("7500", "Production")
        summary = apple_iap.subscription_summary(self.fresh())
        self.assertEqual((summary["active"], summary["source"], summary["apple_status"]), (False, "", "billing_retry"))
        self.assertGreater(get_tag_version(self.tenant.id, "tenant"), before)
        self.apple.set("7500", 2, token=str(self.tenant.user_id))
        apple_iap.refresh("7500", "Production")
        self.assertEqual(apple_iap.subscription_summary(self.fresh())["apple_status"], "expired")
        self.assertEqual(apple_iap.subscription_summary(None)["apple_status"], "")
