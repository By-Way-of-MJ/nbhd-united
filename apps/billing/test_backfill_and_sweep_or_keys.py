"""Tests for backfill_openrouter_keys + sweep_orphan_openrouter_keys
management commands (PR #1.6 Phase 5)."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


@override_settings(OPENROUTER_PER_TENANT_KEYS_ENABLED=True)
class BackfillTest(TestCase):
    def setUp(self):
        self.t1 = create_tenant(display_name="Backfill Test 1", telegram_chat_id=901)
        self.t1.status = Tenant.Status.ACTIVE
        self.t1.container_id = "oc-test-1"
        self.t1.managed_identity_id = "/subscriptions/x/identity"
        self.t1.save()

        self.t2 = create_tenant(display_name="Backfill Test 2", telegram_chat_id=902)
        self.t2.status = Tenant.Status.ACTIVE
        self.t2.container_id = "oc-test-2"
        self.t2.managed_identity_id = "/subscriptions/x/identity-2"
        # Already has a sub-key — must be skipped.
        self.t2.openrouter_key_secret_name = "tenants-2-openrouter-key"
        self.t2.openrouter_key_hash = "existing-hash"
        self.t2.save()

        self.t3 = create_tenant(display_name="Backfill Test 3", telegram_chat_id=903)
        self.t3.status = Tenant.Status.SUSPENDED  # eligible, but no container update
        self.t3.save()

        self.t4 = create_tenant(display_name="Backfill Test 4", telegram_chat_id=904)
        self.t4.status = Tenant.Status.PENDING  # ineligible
        self.t4.save()

    def test_flag_off_refuses_to_run(self):
        with override_settings(OPENROUTER_PER_TENANT_KEYS_ENABLED=False), self.assertRaises(CommandError):
            call_command("backfill_openrouter_keys")

    @patch("apps.orchestrator.azure_client.update_container_openrouter_key_secret")
    @patch("apps.orchestrator.azure_client.assign_key_vault_role")
    @patch("apps.orchestrator.azure_client.get_identity_client")
    @patch("apps.byo_models.services._write_secret_to_kv")
    @patch("apps.billing.openrouter_admin.create_sub_key")
    def test_creates_keys_only_for_eligible_tenants(self, mock_create, mock_kv, mock_idc, mock_grant, mock_rebind):
        # Identity-client stub: principal_id read from the MI lookup feeds
        # into assign_key_vault_role. Just needs to return SOMETHING with
        # a .principal_id attr.
        mock_idc.return_value.user_assigned_identities.get.return_value.principal_id = "test-principal"
        mock_create.side_effect = [
            ("mock-or-key-t1", "hash-t1"),
            ("mock-or-key-t3", "hash-t3"),
        ]
        out = StringIO()
        call_command("backfill_openrouter_keys", stdout=out)

        # t1 and t3 got sub-keys; t2 skipped (already had one); t4 skipped (PENDING).
        self.assertEqual(mock_create.call_count, 2)
        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.t3.refresh_from_db()
        self.t4.refresh_from_db()

        self.assertEqual(self.t1.openrouter_key_hash, "hash-t1")
        self.assertEqual(self.t2.openrouter_key_hash, "existing-hash")  # untouched
        self.assertEqual(self.t3.openrouter_key_hash, "hash-t3")
        self.assertEqual(self.t4.openrouter_key_hash, "")

        # KV role granted only for t1 — t3 has no managed_identity_id in
        # setUp ("eligible, but no container update") so the grant is
        # skipped (would be a no-op anyway with no MI to grant to).
        self.assertEqual(mock_grant.call_count, 1)
        # Only ACTIVE tenants get a container rebind. t3 is SUSPENDED → skipped.
        self.assertEqual(mock_rebind.call_count, 1)

    @patch("apps.billing.openrouter_admin.create_sub_key")
    def test_dry_run_makes_no_changes(self, mock_create):
        out = StringIO()
        call_command("backfill_openrouter_keys", "--dry-run", stdout=out)
        mock_create.assert_not_called()
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.openrouter_key_secret_name, "")

    @patch("apps.orchestrator.azure_client.update_container_openrouter_key_secret")
    @patch("apps.orchestrator.azure_client.assign_key_vault_role")
    @patch("apps.orchestrator.azure_client.get_identity_client")
    @patch("apps.byo_models.services._write_secret_to_kv")
    @patch("apps.billing.openrouter_admin.create_sub_key")
    def test_missing_key_vault_prefix_fails_before_create_sub_key(
        self, mock_create, mock_kv, mock_idc, mock_grant, mock_rebind
    ):
        """Regression: tenants without a key_vault_prefix must fail BEFORE
        create_sub_key fires. 2026-05-28 fleet backfill leaked 9 orphan
        OR sub-keys because the call order was inverted."""
        self.t1.key_vault_prefix = ""  # prefix missing → secret_name lookup raises
        self.t1.save()

        out = StringIO()
        call_command("backfill_openrouter_keys", f"--tenant={self.t1.id}", stdout=out)

        # No OR sub-key call should have been made — the prefix check fails fast.
        mock_create.assert_not_called()
        # No KV/role/rebind side-effects either.
        mock_kv.assert_not_called()
        mock_grant.assert_not_called()
        mock_rebind.assert_not_called()
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.openrouter_key_hash, "")

    @patch("apps.orchestrator.azure_client.update_container_openrouter_key_secret")
    @patch("apps.orchestrator.azure_client.assign_key_vault_role")
    @patch("apps.orchestrator.azure_client.get_identity_client")
    @patch("apps.byo_models.services._write_secret_to_kv")
    @patch("apps.billing.openrouter_admin.create_sub_key")
    def test_filter_by_single_tenant(self, mock_create, mock_kv, mock_idc, mock_grant, mock_rebind):
        mock_idc.return_value.user_assigned_identities.get.return_value.principal_id = "test-principal"
        mock_create.return_value = ("mock-or-key-only", "hash-only")
        out = StringIO()
        call_command("backfill_openrouter_keys", f"--tenant={self.t1.id}", stdout=out)

        self.assertEqual(mock_create.call_count, 1)
        self.t1.refresh_from_db()
        self.t3.refresh_from_db()
        self.assertEqual(self.t1.openrouter_key_hash, "hash-only")
        self.assertEqual(self.t3.openrouter_key_hash, "")


class SweepTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sweep Test", telegram_chat_id=905)
        self.tenant.openrouter_key_hash = "known-hash"
        self.tenant.save()

    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.billing.openrouter_admin.list_sub_keys")
    def test_deletes_old_orphans_keeps_known_keys(self, mock_list, mock_delete):
        # Three OR keys: one tied to our tenant, one orphan + old, one orphan + young.
        mock_list.return_value = [
            {
                "hash": "known-hash",
                "name": "tenant-00000001",
                "label": "sk-or-v1-a...1",
                "created_at": "2026-04-01T00:00:00Z",
            },
            {
                "hash": "orphan-old",
                "name": "tenant-00000002",
                "label": "sk-or-v1-b...2",
                "created_at": "2026-04-01T00:00:00Z",
            },
            {
                "hash": "orphan-young",
                "name": "tenant-00000003",
                "label": "sk-or-v1-c...3",
                "created_at": "2099-01-01T00:00:00Z",
            },
        ]
        out = StringIO()
        call_command("sweep_orphan_openrouter_keys", stdout=out)

        # Only the old orphan should have been deleted.
        mock_delete.assert_called_once_with("orphan-old")

    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.billing.openrouter_admin.list_sub_keys")
    def test_dry_run_does_not_delete(self, mock_list, mock_delete):
        mock_list.return_value = [
            {"hash": "orphan-old", "name": "tenant-0000000a", "created_at": "2026-01-01T00:00:00Z"},
        ]
        out = StringIO()
        call_command("sweep_orphan_openrouter_keys", "--dry-run", stdout=out)
        mock_delete.assert_not_called()

    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.billing.openrouter_admin.list_sub_keys")
    def test_skips_when_no_created_at(self, mock_list, mock_delete):
        # Missing created_at → treated as young (conservative).
        mock_list.return_value = [{"hash": "orphan-no-ts", "name": "tenant-0000000b"}]
        out = StringIO()
        call_command("sweep_orphan_openrouter_keys", stdout=out)
        mock_delete.assert_not_called()


class SweepSafetyTest(TestCase):
    """list_sub_keys returns EVERY key on the account (prod 2026-11-25: 59
    tenant keys + 4 platform keys). Only ``tenant-<8 hex>`` keys may go."""

    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.billing.openrouter_admin.list_sub_keys")
    def test_platform_keys_are_never_deleted(self, mock_list, mock_delete):
        old = "2026-01-01T00:00:00Z"
        mock_list.return_value = [
            {"hash": "p1", "name": "nbhd-openrouter-platform-key", "created_at": old},
            {"hash": "p2", "name": "nbhd-functionality", "created_at": old},
            {"hash": "p3", "name": "TheClaw", "created_at": old},
            {"hash": "p4", "name": None, "label": "tenant-deadbeef", "created_at": old},
            {"hash": "p5", "name": "tenant-DEADBEEF", "created_at": old},
            {"hash": "p6", "name": "tenant-deadbeef-extra", "created_at": old},
            {"hash": "t1", "name": "tenant-deadbeef", "created_at": old},
        ]
        call_command("sweep_orphan_openrouter_keys", stdout=StringIO())
        mock_delete.assert_called_once_with("t1")

    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.billing.openrouter_admin.list_sub_keys")
    def test_task_defaults_to_dry_run(self, mock_list, mock_delete):
        from apps.billing.tasks import sweep_orphan_openrouter_keys_task

        mock_list.return_value = [{"hash": "t1", "name": "tenant-deadbeef", "created_at": "2026-01-01T00:00:00Z"}]
        result = sweep_orphan_openrouter_keys_task()
        mock_delete.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["orphans"], 1)

        sweep_orphan_openrouter_keys_task(dry_run=False)
        mock_delete.assert_called_once_with("t1")


@override_settings(OPENROUTER_PER_TENANT_KEYS_ENABLED=True)
class EnsureSubkeyOnReactivationTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Comeback", telegram_chat_id=906)
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.ACTIVE, container_id="oc-comeback")
        self.tenant.refresh_from_db()

    @patch("apps.billing.management.commands.backfill_openrouter_keys._backfill_one", return_value=True)
    def test_task_keys_an_unkeyed_active_tenant(self, mock_backfill):
        from apps.billing.tasks import ensure_openrouter_subkey_task

        self.assertEqual(ensure_openrouter_subkey_task(str(self.tenant.id))["status"], "ok")
        mock_backfill.assert_called_once()
        self.assertEqual(mock_backfill.call_args.args[0].pk, self.tenant.pk)
        self.assertFalse(mock_backfill.call_args.args[1])  # not a dry run

    @patch("apps.billing.management.commands.backfill_openrouter_keys._backfill_one")
    def test_task_skips_keyed_and_suspended_tenants(self, mock_backfill):
        from apps.billing.tasks import ensure_openrouter_subkey_task

        Tenant.objects.filter(pk=self.tenant.pk).update(openrouter_key_secret_name="already")
        self.assertEqual(ensure_openrouter_subkey_task(str(self.tenant.id))["status"], "not_eligible")
        Tenant.objects.filter(pk=self.tenant.pk).update(openrouter_key_secret_name="", status=Tenant.Status.SUSPENDED)
        self.assertEqual(ensure_openrouter_subkey_task(str(self.tenant.id))["status"], "not_eligible")
        mock_backfill.assert_not_called()

    @patch("apps.billing.management.commands.backfill_openrouter_keys._backfill_one", side_effect=RuntimeError("boom"))
    def test_task_never_raises(self, _mock_backfill):
        from apps.billing.tasks import ensure_openrouter_subkey_task

        self.assertEqual(ensure_openrouter_subkey_task(str(self.tenant.id))["status"], "error")

    @override_settings(OPENROUTER_PER_TENANT_KEYS_ENABLED=False)
    def test_task_respects_flag(self):
        from apps.billing.tasks import ensure_openrouter_subkey_task

        self.assertEqual(ensure_openrouter_subkey_task(str(self.tenant.id))["status"], "flag_off")

    @patch("apps.orchestrator.services.refresh_system_cron_rows_from_seed", return_value={})
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.scale_container_app")
    def test_restore_queues_subkey_for_unkeyed_tenant_only(self, _mock_scale, mock_publish, _mock_refresh):
        from apps.billing.services import restore_tenant_runtime

        self.assertTrue(restore_tenant_runtime(self.tenant))
        calls = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "ensure_openrouter_subkey"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], str(self.tenant.id))
        self.assertEqual(calls[0].kwargs["retries"], 0)
        self.assertGreaterEqual(calls[0].kwargs["delay_seconds"], 60)

        mock_publish.reset_mock()
        Tenant.objects.filter(pk=self.tenant.pk).update(openrouter_key_secret_name="already")
        restore_tenant_runtime(self.tenant)
        self.assertFalse([c for c in mock_publish.call_args_list if c.args and c.args[0] == "ensure_openrouter_subkey"])
