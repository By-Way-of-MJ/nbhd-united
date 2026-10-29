"""Integration scheduled task tests."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .models import Integration
from .tasks import refresh_expiring_integrations_task


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="google-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
)
class RefreshExpiringIntegrationsTaskTest(TestCase):
    def setUp(self):
        tenant = create_tenant(display_name="Refresh User", telegram_chat_id=616161)
        self.integration = Integration.objects.create(
            tenant=tenant,
            provider=Integration.Provider.GOOGLE,
            status=Integration.Status.ACTIVE,
            token_expires_at=timezone.now() + timedelta(minutes=5),
        )

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_refreshes_expiring_integration(self, mock_load_tokens, mock_refresh):
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["errored"], 0)
        mock_refresh.assert_called_once()

    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_marks_expired_when_refresh_token_missing(self, mock_load_tokens):
        mock_load_tokens.return_value = {"access_token": "access-only"}

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.integration.status, Integration.Status.EXPIRED)

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="",
        GOOGLE_OAUTH_CLIENT_SECRET="",
    )
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_marks_error_when_oauth_credentials_missing(self, mock_load_tokens):
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["errored"], 1)
        self.assertEqual(self.integration.status, Integration.Status.ERROR)

    @patch("apps.integrations.tasks.get_provider_client_credentials", return_value=("", ""))
    def test_skips_m2m_provider_without_oauth_credentials(self, mock_get_credentials):
        self.integration.token_expires_at = timezone.now() + timedelta(hours=1)
        self.integration.save(update_fields=["token_expires_at", "updated_at"])
        sautai = Integration.objects.create(
            tenant=self.integration.tenant,
            provider=Integration.Provider.SAUTAI,
            status=Integration.Status.ACTIVE,
        )
        original_updated_at = sautai.updated_at

        result = refresh_expiring_integrations_task()
        sautai.refresh_from_db()

        self.assertEqual(
            result,
            {"checked": 0, "refreshed": 0, "expired": 0, "errored": 0, "retried_errors": 0},
        )
        self.assertEqual(sautai.status, Integration.Status.ACTIVE)
        self.assertEqual(sautai.updated_at, original_updated_at)
        mock_get_credentials.assert_not_called()

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_marks_expired_on_http_400(self, mock_load_tokens, mock_refresh):
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}
        req = httpx.Request("POST", "https://oauth2.googleapis.com/token")
        resp = httpx.Response(400, request=req)
        mock_refresh.side_effect = httpx.HTTPStatusError(
            "bad request",
            request=req,
            response=resp,
        )

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.integration.status, Integration.Status.EXPIRED)

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_refreshes_when_token_expiry_is_unknown(self, mock_load_tokens, mock_refresh):
        self.integration.token_expires_at = None
        self.integration.save(update_fields=["token_expires_at", "updated_at"])
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["refreshed"], 1)
        mock_refresh.assert_called_once()

    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_handles_malformed_token_payload_without_crashing(self, mock_load_tokens):
        mock_load_tokens.return_value = ["not-a-dict"]

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.integration.status, Integration.Status.EXPIRED)

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_retries_error_integration_and_heals(self, mock_load_tokens, mock_refresh):
        self.integration.status = Integration.Status.ERROR
        self.integration.token_expires_at = timezone.now() + timedelta(days=30)
        self.integration.save(update_fields=["status", "token_expires_at", "updated_at"])
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}
        initial_config_version = self.integration.tenant.pending_config_version

        def heal(**kwargs):
            healed = Integration.objects.get(pk=self.integration.pk)
            healed.status = Integration.Status.ACTIVE
            healed.save(update_fields=["status", "updated_at"])

        mock_refresh.side_effect = heal

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()
        self.integration.tenant.refresh_from_db()

        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["retried_errors"], 1)
        self.assertEqual(self.integration.status, Integration.Status.ACTIVE)
        self.assertEqual(
            self.integration.tenant.pending_config_version,
            initial_config_version + 1,
        )
        mock_refresh.assert_called_once()

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_heal_bump_is_atomic_against_stale_instance(self, mock_load_tokens, mock_refresh):
        self.integration.status = Integration.Status.ERROR
        self.integration.token_expires_at = timezone.now() + timedelta(days=30)
        self.integration.save(update_fields=["status", "token_expires_at", "updated_at"])
        applied_version = 7
        Tenant.objects.filter(pk=self.integration.tenant_id).update(
            pending_config_version=applied_version,
            config_version=applied_version,
        )
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        def heal(**kwargs):
            healed = Integration.objects.get(pk=self.integration.pk)
            healed.status = Integration.Status.ACTIVE
            healed.save(update_fields=["status", "updated_at"])

        mock_refresh.side_effect = heal

        result = refresh_expiring_integrations_task()
        tenant = Tenant.objects.get(pk=self.integration.tenant_id)

        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["retried_errors"], 1)
        self.assertEqual(tenant.pending_config_version, applied_version + 1)
        self.assertGreater(tenant.pending_config_version, tenant.config_version)
        mock_refresh.assert_called_once()

    @patch("apps.integrations.tasks.Tenant.objects")
    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_bump_failure_leaves_row_retryable(self, mock_load_tokens, mock_refresh, mock_tenant_objects):
        self.integration.status = Integration.Status.ERROR
        self.integration.token_expires_at = timezone.now() + timedelta(days=30)
        self.integration.save(update_fields=["status", "token_expires_at", "updated_at"])
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}
        mock_tenant_objects.filter.side_effect = RuntimeError("database unavailable")

        def heal(**kwargs):
            healed = Integration.objects.get(pk=self.integration.pk)
            healed.status = Integration.Status.ACTIVE
            healed.save(update_fields=["status", "updated_at"])

        mock_refresh.side_effect = heal

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["retried_errors"], 0)
        self.assertEqual(self.integration.status, Integration.Status.ERROR)
        mock_refresh.assert_called_once()

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_active_refresh_does_not_bump_config(self, mock_load_tokens, mock_refresh):
        initial_config_version = self.integration.tenant.pending_config_version
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()
        self.integration.tenant.refresh_from_db()

        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(self.integration.tenant.pending_config_version, initial_config_version)

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_unreadable_tokens_mark_error_not_expired(self, mock_load_tokens, mock_refresh):
        mock_load_tokens.return_value = None

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["errored"], 1)
        self.assertEqual(result["expired"], 0)
        self.assertEqual(self.integration.status, Integration.Status.ERROR)
        mock_refresh.assert_not_called()

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_error_row_with_unreadable_tokens_stays_error(self, mock_load_tokens, mock_refresh):
        self.integration.status = Integration.Status.ERROR
        self.integration.token_expires_at = timezone.now() + timedelta(days=30)
        self.integration.save(update_fields=["status", "token_expires_at", "updated_at"])
        mock_load_tokens.return_value = None

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["errored"], 1)
        self.assertEqual(self.integration.status, Integration.Status.ERROR)
        mock_refresh.assert_not_called()

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_error_integration_with_http_401_becomes_expired(self, mock_load_tokens, mock_refresh):
        self.integration.status = Integration.Status.ERROR
        self.integration.token_expires_at = timezone.now() + timedelta(days=30)
        self.integration.save(update_fields=["status", "token_expires_at", "updated_at"])
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}
        req = httpx.Request("POST", "https://oauth2.googleapis.com/token")
        resp = httpx.Response(401, request=req)
        mock_refresh.side_effect = httpx.HTTPStatusError(
            "unauthorized",
            request=req,
            response=resp,
        )

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["retried_errors"], 0)
        self.assertEqual(self.integration.status, Integration.Status.EXPIRED)

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_expired_integration_is_not_retried(self, mock_load_tokens, mock_refresh):
        self.integration.status = Integration.Status.EXPIRED
        self.integration.token_expires_at = timezone.now() + timedelta(days=30)
        self.integration.save(update_fields=["status", "token_expires_at", "updated_at"])
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()

        self.assertEqual(result["checked"], 0)
        mock_refresh.assert_not_called()

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_active_not_expiring_is_still_skipped(self, mock_load_tokens, mock_refresh):
        self.integration.token_expires_at = timezone.now() + timedelta(days=30)
        self.integration.save(update_fields=["token_expires_at", "updated_at"])
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()

        self.assertEqual(result["checked"], 0)
        mock_refresh.assert_not_called()
