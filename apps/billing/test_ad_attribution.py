"""Apple Ads attribution: the app reports a token, we record where it came from.

Apple is faked at the one seam the client uses — ``httpx.post`` — so no test
touches the network.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework.test import APIClient

from apps.billing.models import AdAttribution
from apps.cron.views import TASK_MAP
from apps.tenants.services import create_tenant

URL = "/api/v1/billing/ad-attribution/"

ATTRIBUTED = {
    "attribution": True,
    "orgId": 123456,
    "campaignId": 111,
    "adGroupId": 222,
    "keywordId": 333,
    "adId": 444,
    "countryOrRegion": "US",
    "clickDate": "2026-01-02T03:04:05Z",
}


def apple_response(status_code: int, payload=None) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = {} if payload is None else payload
    return response


class AdAttributionTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Aya", telegram_chat_id=990101)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def post(self, token="token-1"):
        return self.client.post(URL, {"token": token}, format="json")

    def row(self):
        return AdAttribution.objects.get(tenant=self.tenant)

    def test_attributed_token_records_the_campaign_and_never_echoes_the_token(self):
        token = "secret-apple-token"
        with patch("httpx.post", return_value=apple_response(200, ATTRIBUTED)) as post:
            response = self.post(token)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"status": "attributed"})
        self.assertNotIn(token, response.content.decode())

        row = self.row()
        self.assertEqual(row.status, AdAttribution.Status.ATTRIBUTED)
        self.assertEqual(
            (row.org_id, row.campaign_id, row.ad_group_id, row.keyword_id, row.ad_id),
            (123456, 111, 222, 333, 444),
        )
        self.assertEqual(row.country_or_region, "US")
        self.assertEqual(row.raw, ATTRIBUTED)
        self.assertIsNotNone(row.click_date)
        self.assertIsNotNone(row.resolved_at)
        self.assertEqual(row.token, "")

        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api-adservices.apple.com/api/v1/")
        self.assertEqual(kwargs["content"], token)
        self.assertEqual(kwargs["headers"], {"Content-Type": "text/plain"})
        self.assertEqual(kwargs["timeout"], 2)

    def test_organic_install_records_no_campaign(self):
        with patch("httpx.post", return_value=apple_response(200, {"attribution": False})):
            response = self.post()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"status": "organic"})
        row = self.row()
        self.assertEqual(row.status, AdAttribution.Status.ORGANIC)
        self.assertIsNone(row.campaign_id)
        self.assertEqual(row.raw, {"attribution": False})
        self.assertIsNotNone(row.resolved_at)
        self.assertEqual(row.token, "")

    def test_not_ready_yet_stays_pending_and_the_retry_command_resolves_it(self):
        with patch(
            "httpx.post",
            side_effect=[apple_response(404), apple_response(200, ATTRIBUTED)],
        ) as post:
            first = self.post()
            self.assertEqual(first.json(), {"status": "pending"})
            row = self.row()
            self.assertEqual(row.attempts, 1)
            self.assertEqual(row.token, "token-1")

            call_command("resolve_ad_attributions", stdout=StringIO())

        self.assertEqual(post.call_count, 2)
        row = self.row()
        self.assertEqual(row.status, AdAttribution.Status.ATTRIBUTED)
        self.assertEqual(row.campaign_id, 111)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.token, "")

    def test_invalid_token_becomes_error_and_is_not_retried(self):
        with patch("httpx.post", return_value=apple_response(400, {"error": "bad token"})):
            response = self.post("bad-secret-token")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"status": "error"})
        self.assertNotIn("bad-secret-token", response.content.decode())
        row = self.row()
        self.assertEqual(row.status, AdAttribution.Status.ERROR)
        self.assertEqual(row.raw, {"reason": "token_invalid"})
        self.assertEqual(row.token, "")
        self.assertEqual(row.attempts, 0)

    def test_apple_5xx_leaves_the_row_pending_for_retry(self):
        with patch("httpx.post", return_value=apple_response(503)):
            response = self.post()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"status": "pending"})
        row = self.row()
        self.assertEqual(row.status, AdAttribution.Status.PENDING)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.token, "token-1")

    def test_second_post_is_a_no_op_and_the_first_token_wins(self):
        different = dict(ATTRIBUTED, campaignId=999)
        with patch(
            "httpx.post",
            side_effect=[apple_response(200, ATTRIBUTED), apple_response(200, different)],
        ) as post:
            first = self.post("first-token")
            second = self.post("second-token")

        self.assertEqual(first.json(), {"status": "attributed"})
        self.assertEqual(second.json(), {"status": "attributed"})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(AdAttribution.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(self.row().campaign_id, 111)

    def test_unauthenticated_post_is_rejected(self):
        response = APIClient().post(URL, {"token": "token-1"}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(AdAttribution.objects.exists())

    def test_invalid_body_is_rejected_without_creating_a_row(self):
        for body in ({}, {"token": ""}, {"token": 123}, {"token": "x" * 4097}):
            response = self.client.post(URL, body, format="json")
            self.assertEqual(response.status_code, 400, body)
        self.assertFalse(AdAttribution.objects.exists())

    def test_retry_command_expires_rows_older_than_24_hours(self):
        row = AdAttribution.objects.create(tenant=self.tenant, token="old-token")
        AdAttribution.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(hours=25))

        with patch("httpx.post") as post:
            call_command("resolve_ad_attributions", stdout=StringIO())

        post.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, AdAttribution.Status.ERROR)
        self.assertEqual(row.raw, {"reason": "expired"})
        self.assertEqual(row.token, "")

    def test_resolve_failure_returns_pending_and_never_echoes_the_token(self):
        token = "secret-apple-token"
        with patch("apps.billing.adservices.resolve", side_effect=RuntimeError("boom")):
            response = self.post(token)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"status": "pending"})
        self.assertNotIn(token, response.content.decode())
        self.assertEqual(self.row().status, AdAttribution.Status.PENDING)

    def test_retry_task_is_registered_and_calls_the_command(self):
        task = import_string(TASK_MAP["resolve_ad_attributions"])
        self.assertTrue(callable(task))

        with patch("django.core.management.call_command") as call_command:
            result = task()

        call_command.assert_called_once_with("resolve_ad_attributions")
        self.assertEqual(result, {"status": "completed"})

    def test_report_counts_accounts_and_paying_now(self):
        self.tenant.stripe_subscription_id = "sub_paying"
        self.tenant.save(update_fields=["stripe_subscription_id"])
        second = create_tenant(display_name="Ben", telegram_chat_id=990102)
        organic = create_tenant(display_name="Cora", telegram_chat_id=990103)
        pending = create_tenant(display_name="Dee", telegram_chat_id=990104)
        errored_invalid = create_tenant(display_name="Eli", telegram_chat_id=990105)
        errored_expired = create_tenant(display_name="Fay", telegram_chat_id=990106)

        for tenant in (self.tenant, second):
            AdAttribution.objects.create(
                tenant=tenant,
                status=AdAttribution.Status.ATTRIBUTED,
                campaign_id=111,
                ad_group_id=222,
                keyword_id=333,
                org_id=123456,
            )
        AdAttribution.objects.create(tenant=organic, status=AdAttribution.Status.ORGANIC)
        AdAttribution.objects.create(tenant=pending, status=AdAttribution.Status.PENDING, token="retry")
        AdAttribution.objects.create(
            tenant=errored_invalid,
            status=AdAttribution.Status.ERROR,
            raw={"reason": "token_invalid"},
        )
        AdAttribution.objects.create(
            tenant=errored_expired,
            status=AdAttribution.Status.ERROR,
            raw={"reason": "expired"},
        )

        out = StringIO()
        call_command("ad_attribution_report", "--days", "1", stdout=out)
        output = out.getvalue()

        self.assertIn("campaign_id=111 ad_group_id=222 keyword_id=333 accounts=2 paying_now=1", output)
        self.assertIn("totals organic=1 pending=1 error_expired=1 error_token_invalid=1", output)
