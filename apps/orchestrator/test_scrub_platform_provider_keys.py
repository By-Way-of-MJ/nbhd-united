"""Tests for the existing-fleet platform provider-key scrub command."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.tenants.services import create_tenant


def _make_tenant(suffix: int):
    tenant = create_tenant(
        display_name=f"ProviderScrub-{suffix}",
        telegram_chat_id=980000 + suffix,
    )
    tenant.container_id = f"oc-provider-scrub-{suffix}"
    tenant.save(update_fields=["container_id"])
    return tenant


def _container_app(*, dirty: bool):
    secrets = [{"name": "nbhd-internal-api-key", "secretRef": "internal"}]
    env = [{"name": "NBHD_TENANT_ID", "value": "tenant-id"}]
    if dirty:
        secrets.extend(
            [
                {"name": "anthropic-key", "secretRef": "legacy-anthropic"},
                {"name": "openai-key", "secretRef": "legacy-openai"},
            ]
        )
        env.extend(
            [
                {"name": "ANTHROPIC_API_KEY", "secretRef": "anthropic-key"},
                {"name": "OPENAI_API_KEY", "secretRef": "openai-key"},
            ]
        )
    return SimpleNamespace(
        configuration=SimpleNamespace(secrets=secrets),
        template=SimpleNamespace(
            revision_suffix="old",
            containers=[SimpleNamespace(name="openclaw", env=env)],
        ),
    )


@override_settings(AZURE_RESOURCE_GROUP="rg-nbhd-test")
class ScrubPlatformProviderKeysTest(TestCase):
    @patch(
        "apps.orchestrator.management.commands.scrub_platform_provider_keys.is_mock",
        return_value=True,
    )
    def test_refuses_mock_mode(self, _mock_is_mock):
        with self.assertRaises(CommandError):
            call_command("scrub_platform_provider_keys")

    @patch(
        "apps.orchestrator.management.commands.scrub_platform_provider_keys.is_mock",
        return_value=False,
    )
    @patch("apps.orchestrator.management.commands.scrub_platform_provider_keys.get_container_client")
    def test_dirty_container_is_scrubbed_and_revised(self, mock_get_client, _mock_is_mock):
        tenant = _make_tenant(1)
        app = _container_app(dirty=True)
        client = MagicMock()
        client.container_apps.get.return_value = app
        mock_get_client.return_value = client
        stdout = StringIO()

        call_command("scrub_platform_provider_keys", tenant=str(tenant.id), stdout=stdout)

        self.assertEqual(stdout.getvalue().strip(), "scrubbed=1 unchanged=0 failed=0")
        self.assertEqual(
            {entry["name"] for entry in app.configuration.secrets},
            {"nbhd-internal-api-key"},
        )
        self.assertEqual(
            {entry["name"] for entry in app.template.containers[0].env},
            {"NBHD_TENANT_ID"},
        )
        self.assertRegex(app.template.revision_suffix, r"^b[0-9a-f]{6}$")
        client.container_apps.begin_create_or_update.assert_called_once_with(
            "rg-nbhd-test",
            tenant.container_id,
            app,
        )
        client.container_apps.begin_create_or_update.return_value.result.assert_called_once()

    @patch(
        "apps.orchestrator.management.commands.scrub_platform_provider_keys.is_mock",
        return_value=False,
    )
    @patch("apps.orchestrator.management.commands.scrub_platform_provider_keys.get_container_client")
    def test_clean_container_is_unchanged(self, mock_get_client, _mock_is_mock):
        tenant = _make_tenant(2)
        app = _container_app(dirty=False)
        client = MagicMock()
        client.container_apps.get.return_value = app
        mock_get_client.return_value = client
        stdout = StringIO()

        call_command("scrub_platform_provider_keys", tenant=str(tenant.id), stdout=stdout)

        self.assertEqual(stdout.getvalue().strip(), "scrubbed=0 unchanged=1 failed=0")
        self.assertEqual(app.template.revision_suffix, "old")
        client.container_apps.begin_create_or_update.assert_not_called()

    @patch(
        "apps.orchestrator.management.commands.scrub_platform_provider_keys.is_mock",
        return_value=False,
    )
    @patch("apps.orchestrator.management.commands.scrub_platform_provider_keys.get_container_client")
    def test_failure_does_not_abort_the_sweep(self, mock_get_client, _mock_is_mock):
        failing = _make_tenant(3)
        succeeding = _make_tenant(4)
        successful_app = _container_app(dirty=True)
        client = MagicMock()

        def get_app(_resource_group, container_id):
            if container_id == failing.container_id:
                raise RuntimeError("simulated Azure failure")
            return successful_app

        client.container_apps.get.side_effect = get_app
        mock_get_client.return_value = client
        stdout = StringIO()

        call_command("scrub_platform_provider_keys", stdout=stdout)

        self.assertEqual(stdout.getvalue().strip(), "scrubbed=1 unchanged=0 failed=1")
        self.assertEqual(client.container_apps.get.call_count, 2)
        client.container_apps.begin_create_or_update.assert_called_once_with(
            "rg-nbhd-test",
            succeeding.container_id,
            successful_app,
        )

    @patch(
        "apps.orchestrator.management.commands.scrub_platform_provider_keys.is_mock",
        return_value=False,
    )
    @patch("apps.orchestrator.management.commands.scrub_platform_provider_keys.get_container_client")
    def test_dry_run_counts_dirty_container_without_writing(self, mock_get_client, _mock_is_mock):
        tenant = _make_tenant(5)
        app = _container_app(dirty=True)
        client = MagicMock()
        client.container_apps.get.return_value = app
        mock_get_client.return_value = client
        stdout = StringIO()

        call_command(
            "scrub_platform_provider_keys",
            tenant=str(tenant.id),
            dry_run=True,
            stdout=stdout,
        )

        self.assertEqual(stdout.getvalue().strip(), "scrubbed=1 unchanged=0 failed=0")
        self.assertEqual(app.template.revision_suffix, "old")
        client.container_apps.begin_create_or_update.assert_not_called()
