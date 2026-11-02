"""Tests for the strip_phase2_blocks management command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator.config_generator import (
    _RETIRED_FINAL_STEP_PROSE,
    PHASE2_SYNC_MARKER,
    upgrade_retired_final_step_prose,
)
from apps.tenants.models import Tenant, User


def _legacy_message() -> str:
    return "base" + "\n\n---\n**" + PHASE2_SYNC_MARKER + ":**\nold"


class StripPhase2BlocksCommandTest(TestCase):
    def setUp(self):
        self.enterContext(suppress_cronjob_reconcile())

    def _tenant(self, username: str, *, canonical: bool = True) -> Tenant:
        user = User.objects.create_user(username=username, password="x")
        return Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            postgres_cron_canonical=canonical,
        )

    def _row(self, tenant: Tenant, name: str, message: str) -> CronJob:
        return CronJob.objects.create(
            tenant=tenant,
            name=name,
            data={"payload": {"kind": "agentTurn", "message": message}},
        )

    def test_dry_run_writes_nothing(self):
        tenant = self._tenant("strip-dry-run")
        row = self._row(tenant, "Legacy", _legacy_message())
        stdout = StringIO()

        call_command("strip_phase2_blocks", "--tenant", str(tenant.id), stdout=stdout)

        row.refresh_from_db()
        self.assertEqual(row.data["payload"]["message"], _legacy_message())
        output = stdout.getvalue()
        self.assertIn("rows_matched=1", output)
        self.assertIn("rows_written=0", output)
        self.assertNotIn("old", output)

    def test_apply_cleans_marker_and_old_prose_rows(self):
        tenant = self._tenant("strip-apply")
        marker_row = self._row(tenant, "Marker", _legacy_message())
        old, new = _RETIRED_FINAL_STEP_PROSE[0]
        old_prose_row = self._row(tenant, "Old prose", "base\n" + old)
        stdout = StringIO()

        call_command(
            "strip_phase2_blocks",
            "--tenant",
            str(tenant.id),
            "--apply",
            stdout=stdout,
        )

        marker_row.refresh_from_db()
        old_prose_row.refresh_from_db()
        self.assertEqual(marker_row.data["payload"]["message"], "base")
        self.assertEqual(
            old_prose_row.data["payload"]["message"],
            upgrade_retired_final_step_prose("base\n" + old),
        )
        self.assertEqual(old_prose_row.data["payload"]["message"], "base\n" + new)
        output = stdout.getvalue()
        self.assertIn("rows_matched=2", output)
        self.assertIn("rows_written=2", output)

    def test_clean_rows_are_not_saved(self):
        tenant = self._tenant("strip-clean")
        self._row(tenant, "Clean", "already clean")

        with patch.object(CronJob, "save") as save:
            call_command(
                "strip_phase2_blocks",
                "--tenant",
                str(tenant.id),
                "--apply",
                stdout=StringIO(),
            )

        save.assert_not_called()

    def test_second_apply_writes_zero(self):
        tenant = self._tenant("strip-idempotent")
        row = self._row(tenant, "Marker", _legacy_message())

        call_command(
            "strip_phase2_blocks",
            "--tenant",
            str(tenant.id),
            "--apply",
            stdout=StringIO(),
        )
        second_stdout = StringIO()
        call_command(
            "strip_phase2_blocks",
            "--tenant",
            str(tenant.id),
            "--apply",
            stdout=second_stdout,
        )

        row.refresh_from_db()
        self.assertEqual(row.data["payload"]["message"], "base")
        self.assertIn("rows_matched=0", second_stdout.getvalue())
        self.assertIn("rows_written=0", second_stdout.getvalue())

    def test_noncanonical_tenant_is_counted_but_not_written(self):
        tenant = self._tenant("strip-noncanonical", canonical=False)
        row = self._row(tenant, "Marker", _legacy_message())
        stdout = StringIO()

        call_command(
            "strip_phase2_blocks",
            "--tenant",
            str(tenant.id),
            "--apply",
            stdout=stdout,
        )

        row.refresh_from_db()
        self.assertEqual(row.data["payload"]["message"], _legacy_message())
        output = stdout.getvalue()
        self.assertIn("rows_written=0", output)
        self.assertIn("skipped_noncanonical=1", output)

    def test_tenant_flag_limits_scope(self):
        target = self._tenant("strip-target")
        other = self._tenant("strip-other")
        target_row = self._row(target, "Target", _legacy_message())
        other_row = self._row(other, "Other", _legacy_message())

        call_command(
            "strip_phase2_blocks",
            "--tenant",
            str(target.id),
            "--apply",
            stdout=StringIO(),
        )

        target_row.refresh_from_db()
        other_row.refresh_from_db()
        self.assertEqual(target_row.data["payload"]["message"], "base")
        self.assertEqual(other_row.data["payload"]["message"], _legacy_message())

    def test_missing_scope_flag_errors(self):
        with self.assertRaises(CommandError):
            call_command("strip_phase2_blocks", stdout=StringIO())

    def test_tenant_and_all_are_mutually_exclusive(self):
        tenant = self._tenant("strip-both")
        with self.assertRaises(CommandError):
            call_command(
                "strip_phase2_blocks",
                "--tenant",
                str(tenant.id),
                "--all",
                stdout=StringIO(),
            )
