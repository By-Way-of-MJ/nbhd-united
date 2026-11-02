"""Flag convergence and the real built-in cron transport's tool wall."""

import json
import os
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import TestCase, override_settings

from apps.cron.models import CronCreationPath, CronJob, CronPattern
from apps.cron.services import create_typed_cron
from apps.cron.share_cron_sync import build_signed_crons_doc
from apps.friends.test_pr6 import _tenant
from apps.orchestrator.config_generator import HEARTBEAT_MODEL, build_cron_seed_jobs
from apps.orchestrator.services import refresh_system_cron_rows_from_seed
from apps.orchestrator.tool_policy import STEP_PREP_TOOLS, generate_tool_config


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True, STEP_PREP_TENANT_IDS="")
class StepPrepJobTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("prep_jobs")

    def jobs(self):
        return {j["name"]: j for j in build_cron_seed_jobs(self.tenant) if j["name"].startswith("Step Prep")}

    @skipUnless(
        os.environ.get("OPENCLAW_CONTRACT_ROOT"), "Set OPENCLAW_CONTRACT_ROOT to the unpacked 2026.9.4 npm package"
    )
    @override_settings(STEP_PREP_TENANT_IDS="*")
    def test_real_runtime_matcher_enforces_seeded_builtin_caps(self):
        script = Path(__file__).with_name("runtime-policy-check.mjs")
        result = subprocess.run(
            ["node", str(script), os.environ["OPENCLAW_CONTRACT_ROOT"]],
            input=json.dumps(list(self.jobs().values())),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        print(result.stdout, end="")

    def test_flagged_only_and_independent_of_scheduled_message_opt_in(self):
        self.assertEqual(self.jobs(), {})
        with override_settings(STEP_PREP_TENANT_IDS=str(self.tenant.id)):
            self.tenant.scheduled_messages_opt_in = True
            self.assertEqual(set(self.jobs()), {"Step Prep", "Step Prep Update"})
        with override_settings(STEP_PREP_TENANT_IDS="00000000-0000-0000-0000-000000000000"):
            self.assertEqual(self.jobs(), {})

    @override_settings(STEP_PREP_TENANT_IDS="*")
    def test_exact_tool_walls_silent_delivery_and_worker_model(self):
        jobs = self.jobs()
        for name, job in jobs.items():
            expected = {
                "Step Prep": ["nbhd_step_prep_next", "nbhd_step_prep_save", "nbhd_step_prep_report", "web_search"],
                "Step Prep Update": ["nbhd_step_prep_update_next", "nbhd_step_prep_save", "nbhd_step_prep_report"],
            }[name]
            self.assertEqual(list(STEP_PREP_TOOLS[name]), expected)
            self.assertEqual(job["payload"]["toolsAllow"], expected)
            self.assertEqual(job["delivery"], {"mode": "none"})
            self.assertEqual(job["model"], HEARTBEAT_MODEL)
            self.assertEqual(job["sessionTarget"], "isolated")
            self.assertTrue(job["payload"]["lightContext"])
            self.assertIn("If job is null, end immediately", job["payload"]["message"])
            self.assertNotIn("nbhd_send_to_user", expected)
            self.assertNotIn("nbhd_document_", job["payload"]["message"])
            self.assertNotIn("mode=", job["payload"]["message"])
            self.assertFalse(any(tool.startswith("nbhd_document_") for tool in expected))
            self.assertNotIn("nbhd_step_prep_update_next" if name == "Step Prep" else "nbhd_step_prep_next", expected)
            for tool in expected:
                self.assertFalse(any(word in tool for word in ["task", "memory", "cron", "project"]))
        self.assertNotIn("web_search", jobs["Step Prep Update"]["payload"]["toolsAllow"])
        checkin = next(j for j in build_cron_seed_jobs(self.tenant) if j["name"] == "Project Check-in")
        hour = int(checkin["schedule"]["expr"].split()[1])
        for offset, name in [(1, "Step Prep"), (2, "Step Prep Update")]:
            self.assertEqual(jobs[name]["schedule"]["expr"], f"0 {(hour + offset) % 24} * * *")
        policy = generate_tool_config()
        self.assertIn("group:plugins", policy["allow"])
        self.assertTrue({tool for tools in STEP_PREP_TOOLS.values() for tool in tools}.isdisjoint(policy["deny"]))

    def test_existing_tenant_converges_flag_on_refresh_and_flag_off_reaps(self):
        refresh_system_cron_rows_from_seed(self.tenant)
        rows = CronJob.objects.filter(tenant=self.tenant, name__startswith="Step Prep")
        self.assertEqual(rows.count(), 0)
        with override_settings(STEP_PREP_TENANT_IDS=str(self.tenant.id)):
            refresh_system_cron_rows_from_seed(self.tenant)
            self.assertEqual(rows.count(), 2)
            before = list(rows.order_by("id").values_list("id", "data"))
            refresh_system_cron_rows_from_seed(self.tenant)
            self.assertEqual(list(rows.order_by("id").values_list("id", "data")), before)
            # The platform must recognize our prompt as managed on later
            # applies, rather than preserving an obsolete script as custom.
            row = rows.get(name="Step Prep")
            row.data["payload"]["message"] = "Background maintenance run. Obsolete prep script."
            row.save(update_fields=["data"])
            refresh_system_cron_rows_from_seed(self.tenant)
            row.refresh_from_db()
            self.assertEqual(row.data["payload"]["message"], self.jobs()["Step Prep"]["payload"]["message"])
        refresh_system_cron_rows_from_seed(self.tenant)
        self.assertEqual(rows.count(), 0)

    @override_settings(STEP_PREP_TENANT_IDS="*")
    def test_builtin_and_typed_tools_allow_survive_real_row_render_and_signing(self):
        refresh_system_cron_rows_from_seed(self.tenant)
        control = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Test reminder"},
            name="Typed reminder control",
            schedule={"kind": "cron", "expr": "0 10 * * *", "tz": "UTC"},
        )
        self.tenant.internal_api_key = "test-signing-key"
        body, _ = build_signed_crons_doc(self.tenant)
        rendered = {job["name"]: job for job in json.loads(json.loads(body)["signed"])}
        for name, seed in {**self.jobs(), "Typed reminder control": control.data}.items():
            row = CronJob.objects.get(tenant=self.tenant, name=name)
            self.assertEqual(
                row.creation_path, CronCreationPath.TYPED if row.pk == control.pk else CronCreationPath.LEGACY
            )
            self.assertEqual(rendered[name]["payload"]["toolsAllow"], seed["payload"]["toolsAllow"])
        self.assertEqual(rendered["Step Prep"]["payload"]["model"], HEARTBEAT_MODEL)
