"""Real DB/config/storage contracts for the opt-in local adapter; no LLM calls."""

import json
import os
import runpy
import socket
import subprocess
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from apps.tenants.models import Tenant, User

from . import azure_client
from .config_generator import generate_openclaw_config
from .local_test import local_root, share_path


@override_settings(DEBUG=True)
class LocalStackTests(TestCase):
    def setUp(self):
        (settings.BASE_DIR / "deploy/local-test/.state/tmp").mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=settings.BASE_DIR / "deploy/local-test/.state/tmp")
        self.addCleanup(self.directory.cleanup)
        self.user = User.objects.create(email="local-stack-fixture@example.invalid")
        self.tenant = Tenant.objects.create(
            user=self.user, is_synthetic=True, is_eval_sink=False, openclaw_version="2026.9.1", sautai_enabled=True
        )
        self.overrides = override_settings(LOCAL_TEST_ROOT=self.directory.name)
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)
        self.env = patch.dict(
            os.environ,
            AZURE_MOCK="true",
            NBHD_TENANT_ID=str(self.tenant.id),
            LOCAL_TEST_MODEL="qwen3.8:27b-obliterated-q8",
            LOCAL_TEST_KEK_SEED="fixture-only",
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_storage_sanitizes_and_refuses_escape(self):
        azure_client.upload_workspace_file(str(self.tenant.id), "workspace/USER.md", "fixture\x00text")
        self.assertEqual(azure_client.download_workspace_file(str(self.tenant.id), "workspace/USER.md"), "fixturetext")
        with self.assertRaises(ValueError):
            share_path(self.tenant.id, "../outside")
        self.tenant.is_synthetic = False
        self.tenant.save(update_fields=["is_synthetic"])
        with self.assertRaises(ValueError):
            local_root(self.tenant.id)
        with override_settings(DEBUG=False), self.assertRaises(RuntimeError):
            local_root(self.tenant.id)

    def test_handoff_survives_disconnected_probe_and_rejects_invalid_host(self):
        import shutil

        # macOS caps AF_UNIX paths at 103 bytes; keep the socket shallow so any checkout path fits.
        sockets = settings.BASE_DIR / "deploy/local-test/.state/hs"
        sockets.mkdir(mode=0o700, exist_ok=True)
        self.addCleanup(shutil.rmtree, sockets, ignore_errors=True)
        handoff = runpy.run_path(str(settings.BASE_DIR / "deploy/local-test/handoff.py"))
        listener = handoff["start_listener"](sockets)
        self.addCleanup(listener.close)
        path = str(sockets / "sautai-handoff.sock")
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(path)
        # A second request must receive a rejection even after the first client
        # disconnected without a payload. No token or DB link is manufactured.
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(path)
            client.sendall(b'{"base_url":"https://invalid.example"}\n')
            self.assertEqual(json.loads(client.recv(1024)), {"accepted": False})

    @override_settings(SAUTAI_PLATFORM_SECRET="")
    def test_handoff_secret_only_leaves_integration_untouched(self):
        from apps.integrations.models import Integration

        accept = runpy.run_path(str(settings.BASE_DIR / "deploy/local-test/handoff.py"))["accept_payload"]
        payload = {"base_url": "http://127.0.0.1:8000", "platform_secret": "fixture-secret"}
        accept(payload)
        self.assertEqual(settings.SAUTAI_PLATFORM_SECRET, "fixture-secret")
        self.assertFalse(Integration.objects.filter(tenant=self.tenant).exists())
        accept({**payload, "sautai_user_id": 42})
        row = Integration.objects.get(tenant=self.tenant)
        before = (row.sautai_user_id, row.linked_at, row.updated_at)
        accept({**payload, "sautai_user_id": None})
        row.refresh_from_db()
        self.assertEqual((row.sautai_user_id, row.linked_at, row.updated_at), before)

    @override_settings(SAUTAI_PLATFORM_SECRET="unchanged")
    def test_handoff_rejects_invalid_shapes_without_writes(self):
        from apps.integrations.models import Integration

        accept = runpy.run_path(str(settings.BASE_DIR / "deploy/local-test/handoff.py"))["accept_payload"]
        payload = {"base_url": "http://127.0.0.1:8000", "platform_secret": "fixture-secret"}
        invalid = [None, [], {}, {**payload, "base_url": "https://invalid.example"}]
        invalid += [{**payload, "sautai_user_id": value} for value in (True, False, 0, -1, 1.5, "1", [], {})]
        invalid += [{**payload, "platform_secret": value} for value in (None, "", 42)]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                accept(value)
        self.assertEqual(settings.SAUTAI_PLATFORM_SECRET, "unchanged")
        self.assertFalse(Integration.objects.filter(tenant=self.tenant).exists())

    def test_persona_import_accepts_harness_manifest_and_refuses_wrong_version(self):
        from django.core.management.base import CommandError

        from .management.commands.prepare_local_test_tenant import persona_facts

        manifest = {
            "persona": "yuki",
            "persona_version": "3",
            "facts": [{"id": "Y-001", "citation": "fixture:1", "text": "Synthetic fixture."}],
        }
        self.assertEqual(persona_facts(manifest), ["Y-001: Synthetic fixture. (source: fixture:1)"])
        manifest["persona_version"] = "2"
        with self.assertRaises(CommandError):
            persona_facts(manifest)

    def test_mock_key_survives_process_registry_reset(self):
        tid = str(self.tenant.id)
        azure_client.create_tenant_kek(tid)
        wrapped, _ = azure_client.wrap_dek(tid, b"fixture-data")
        azure_client._MOCK_KEK_REGISTRY.pop(tid)
        self.assertEqual(azure_client.unwrap_dek(tid, wrapped), b"fixture-data")
        self.assertEqual(azure_client.kek_liveness(tid), "live")

    def test_generated_config_uses_same_validated_share_apply(self):
        config = generate_openclaw_config(self.tenant)
        self.assertEqual(config["gateway"]["bind"], "loopback")
        self.assertEqual(config["gateway"]["port"], 19443)
        provider = config["models"]["providers"]["ollama"]
        self.assertEqual(provider["baseUrl"], "http://127.0.0.1:11434/v1")
        self.assertEqual(provider["api"], "openai-completions")
        self.assertIs(provider["injectNumCtxForOpenAICompat"], False)
        self.assertNotIn("num_ctx", json.dumps(config))
        self.assertIn("nbhd-sautai-tools", config["plugins"]["entries"])
        self.assertEqual(config["agents"]["defaults"]["model"]["fallbacks"], [])
        for path in config["plugins"]["load"]["paths"]:
            self.assertTrue(Path(path).is_dir(), path)
        azure_client.upload_config_to_file_share(str(self.tenant.id), json.dumps(config))
        path = share_path(self.tenant.id, "openclaw.json")
        self.assertEqual(json.loads(path.read_text()), config)
        # Real installed 2026.9.1 schema check, isolated from both gateways.
        env = {
            **os.environ,
            "HOME": self.directory.name,
            "OPENCLAW_HOME": self.directory.name,
            "OPENCLAW_STATE_DIR": self.directory.name + "/state",
            "OPENCLAW_CONFIG_PATH": str(path),
            "OPENCLAW_DEBUG": "1",
        }
        if not Path("/Users/mjjones/.local/bin/openclaw").exists():
            return  # The portable config-validator gate above still runs in Linux CI.
        result = subprocess.run(
            ["/Users/mjjones/.local/bin/openclaw", "config", "validate"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        if Path("/usr/bin/sandbox-exec").exists():
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 19443)) == 0:
                    self.skipTest("Designated test gateway port already occupied; never replace it")
            profile = settings.BASE_DIR / "deploy/local-test/gateway.sb"
            # Boot only: no chat, no inference, no cron jobs, no account signup.
            with tempfile.TemporaryFile() as logs:
                process = subprocess.Popen(
                    [
                        "/usr/bin/sandbox-exec",
                        "-D",
                        f"LOCAL_TEST_ROOT_DIR={Path(settings.BASE_DIR).resolve()}",
                        "-f",
                        str(profile),
                        "/Users/mjjones/.local/bin/openclaw",
                        "gateway",
                        "run",
                    ],
                    env=env,
                    stdout=logs,
                    stderr=logs,
                )
                try:
                    ready = False
                    for _ in range(40):
                        if process.poll() is not None:
                            break
                        try:
                            with httpx.Client(trust_env=False, follow_redirects=False, timeout=1) as client:
                                response = client.get("http://127.0.0.1:19443/health")
                            ready = response.status_code == 200
                        except httpx.HTTPError:
                            pass
                        if ready:
                            break
                        time.sleep(0.5)
                    if not ready:
                        logs.seek(0)
                        diagnostics = logs.read().decode(errors="replace")
                        for value in env.values():
                            if len(value) > 20:
                                diagnostics = diagnostics.replace(value, "[env]")
                        self.fail("Isolated gateway failed boot: " + diagnostics[-4000:])
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    def test_local_qstash_fallback_waits_for_commit_and_honors_delay(self):
        from apps.cron.publish import publish_task
        from apps.cron.views import TASK_MAP

        called = threading.Event()
        with (
            override_settings(QSTASH_TOKEN=""),
            patch("apps.cron.views.execute_task_sync", side_effect=lambda *a, **k: called.set()) as execute,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                publish_task("generate_sautai_meal_plan", "fixture-job", delay_seconds=0.1)
                self.assertFalse(called.is_set())
            self.assertFalse(called.wait(0.02))
            self.assertTrue(called.wait(2))
            execute.assert_called_once_with(TASK_MAP["generate_sautai_meal_plan"], "fixture-job")

    def test_normal_mock_provision_path_creates_real_local_config_and_key(self):
        from apps.crypto.keys import unwrap_dek_for
        from apps.orchestrator.services import provision_tenant

        provision_tenant(str(self.tenant.id), send_first_session_welcome=False)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        self.assertEqual(self.tenant.internal_api_key, settings.NBHD_INTERNAL_API_KEY)
        self.assertEqual(len(unwrap_dek_for(self.tenant)), 32)
        config = json.loads(share_path(self.tenant.id, "openclaw.json").read_text())
        self.assertEqual(config["gateway"]["port"], 19443)
        self.assertIn("nbhd-sautai-tools", config["plugins"]["entries"])


class GpuGuardTests(SimpleTestCase):
    def test_gpu_job_running_matches_process_names_only(self):
        running = runpy.run_path(str(settings.BASE_DIR / "deploy/local-test/run.py"), run_name="local_test_run")[
            "gpu_job_running"
        ]
        for line in ("python3 /x/qwen_match_analysis.py", "/opt/bin/run_bench --n 3", "bash /y/run_bench.sh"):
            self.assertTrue(running(f"/sbin/launchd\n{line}\n"), line)
        for line in (
            "/Users/mjjones/Projects/loanarmy/.loan/bin/python src/workers/vision_worker.py",
            "sleep 60 qwen_match_analysis",
            'claude -p "... qwen_match_analysis|run_bench ..."',
            'python3 -c "import run_bench"',
            "",
        ):
            self.assertFalse(running(line), line)


class CheckoutPortabilityTests(SimpleTestCase):
    def test_gateway_sandbox_is_scoped_to_the_launcher_checkout(self):
        import sys

        directory = settings.BASE_DIR / "deploy/local-test"
        profile = (directory / "gateway.sb").read_text()
        self.assertNotIn("/worktrees/", profile)
        self.assertIn('(subpath (param "LOCAL_TEST_ROOT_DIR"))', profile)
        adapter = (directory / "openclaw-paths.mjs").read_text()
        self.assertNotIn("/worktrees/", adapter)
        self.assertIn("new URL('./.state/', import.meta.url)", adapter)
        # Seatbelt blocks setuid /bin/ps; the adapter answers OpenClaw's own start identity so cron can start.
        self.assertIn("pid-alive-", adapter)
        self.assertIn("if (pid === process.pid) return", adapter)

        launcher = runpy.run_path(str(directory / "run.py"), run_name="local_test_run")["main"]
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "openclaw.json").write_text("{}")
            with (
                patch.dict(
                    launcher.__globals__,
                    environment=dict,
                    gpu_job_running=lambda _: False,
                    TEST_HOME=Path(home),
                ),
                patch.dict(os.environ, LOCAL_TEST_CLEAN_PROCESS="1"),
                patch.object(sys, "argv", ["run.py", "gateway"]),
                patch("subprocess.run"),
                patch("os.umask"),
                patch("os.chdir"),
                patch("os.execve") as execve,
            ):
                launcher()
        root = launcher.__globals__["ROOT"]
        self.assertEqual(root, Path(settings.BASE_DIR).resolve())
        self.assertEqual(
            execve.call_args.args[1][:5],
            ["sandbox-exec", "-D", f"LOCAL_TEST_ROOT_DIR={root}", "-f", str(root / "deploy/local-test/gateway.sb")],
        )


@override_settings(DEBUG=True)
class YukiLocalTests(TestCase):
    def setUp(self):
        import io
        from unittest.mock import MagicMock

        from .management.commands import yuki_local

        self.helper = yuki_local
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.user = User.objects.create(email="yuki@example.com")
        self.tenant = Tenant.objects.create(user=self.user, is_synthetic=True, is_eval_sink=False)
        for context in (
            override_settings(LOCAL_TEST_ROOT=self.directory.name),
            patch.dict(os.environ, AZURE_MOCK="true", NBHD_TENANT_ID=str(self.tenant.id)),
            patch.object(yuki_local, "STATE", self.state),
            patch.object(yuki_local.time, "sleep"),
            patch("socket.socket", side_effect=AssertionError("No real sockets in helper tests")),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.client = MagicMock()
        factory = patch.object(yuki_local.httpx, "Client")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.factory.return_value.__enter__.return_value = self.client
        self.client.post.side_effect = self.post
        self.client.get.side_effect = self.get
        self.messages = []
        self.replies = ["Your plan is ready."]
        self.on_poll = lambda: None
        self.stdout = io.StringIO()
        self.stdin = io.StringIO()

    @staticmethod
    def response(body, code=200):
        import httpx

        return httpx.Response(code, json=body)

    def post(self, url, **kwargs):
        if url == "/api/v1/auth/login/":
            return self.response({"access": "fixture-access-token"})
        if url == "/api/v1/auth/signup/":
            return self.response({}, 201)
        if url == "/api/v1/integrations/sautai/link/":
            return self.response({"status": "connected"})
        if url == "/api/v1/chat/threads/":
            return self.response({"id": "fixture-thread", "is_main": False}, 201)
        if url == "/api/v1/chat/messages/":
            self.messages.append(kwargs["json"])
            return self.response({}, 202)
        raise AssertionError("Unexpected POST")

    def get(self, url, **kwargs):
        if url == "/api/v1/tenants/me/":
            return self.response({"id": str(self.tenant.id), "is_synthetic": True, "is_eval_sink": False})
        if url.startswith("/api/v1/chat/messages/"):
            self.on_poll()
            return self.response({"status": "ready", "source": "tenant", "reply_text": self.replies.pop(0)})
        if url == "/api/v1/integrations/sautai/link/":
            return self.response({"linked": True})
        if url == "/api/v1/fuel/meals/today/":
            return self.response({"linked": True, "meals": [{"name": "Soba"}], "week_start": "2026-09-21"})
        raise AssertionError("Unexpected GET")

    def credentials(self):
        self.helper.private_json(
            self.state / "yuki-account.json", {"email": self.user.email, "password": "fixture-password"}
        )

    def run_helper(self, action, text="", success=True, **options):
        import io

        from django.core.management import call_command

        self.stdout = io.StringIO()
        with patch.object(self.helper.sys, "stdin", io.StringIO(text)):
            if success:
                call_command("yuki_local", action, stdout=self.stdout, **options)
            else:
                with self.assertRaises(SystemExit) as error:
                    call_command("yuki_local", action, stdout=self.stdout, **options)
                self.assertEqual(error.exception.code, 1)
        lines = self.stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        for secret in ("fixture-password", "fixture-access-token", "fixture-connect-key"):
            self.assertNotIn(secret, lines[0])
        return json.loads(lines[0])

    def job(self, week="2026-09-21", **kwargs):
        from apps.integrations.models import SautaiMealPlanJob

        return SautaiMealPlanJob.objects.create(
            tenant=self.tenant,
            week_start=week,
            **{
                "status": "ready",
                "addressed_by": "linked_id",
                "result": {"week_start": week, "days": [{"meals": [{"name": "Soba"}]}]},
                **kwargs,
            },
        )

    def test_signup_creates_private_file_and_is_idempotent(self):
        self.assertEqual(self.run_helper("signup"), {"account": "created"})
        path = self.state / "yuki-account.json"
        original = path.read_bytes()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(json.loads(original)["password"], self.stdout.getvalue())
        self.assertEqual(self.run_helper("signup"), {"account": "exists"})
        self.assertEqual(path.read_bytes(), original)
        signup_calls = [c for c in self.client.post.call_args_list if c.args[0].endswith("signup/")]
        self.assertEqual(len(signup_calls), 1)
        self.factory.assert_called_with(base_url=self.helper.BASE, trust_env=False, follow_redirects=False, timeout=30)

    def test_signup_bootstraps_without_a_tenant(self):
        self.tenant.delete()
        self.assertFalse(Tenant.objects.exists())
        self.assertEqual(self.run_helper("signup"), {"account": "created"})
        self.assertEqual((self.state / "yuki-account.json").stat().st_mode & 0o777, 0o600)

    def test_signup_refuses_a_foreign_synthetic_tenant(self):
        other_user = User.objects.create(username="other-local-fixture", email="other-local-fixture@example.com")
        Tenant.objects.create(user=other_user, is_synthetic=True, is_eval_sink=False)
        self.assertEqual(
            self.run_helper("signup", success=False),
            {"account": "failed", "reason": "local_stack_required"},
        )
        self.factory.assert_not_called()
        self.assertFalse((self.state / "yuki-account.json").exists())

    def test_signup_refuses_a_malformed_tenant_id(self):
        with patch.dict(os.environ, NBHD_TENANT_ID="not-a-uuid"):
            self.assertEqual(
                self.run_helper("signup", success=False),
                {"account": "failed", "reason": "local_stack_required"},
            )
        self.factory.assert_not_called()

    def test_link_still_requires_a_tenant(self):
        self.tenant.delete()
        self.assertEqual(
            self.run_helper("link", "fixture-connect-key", success=False),
            {"linked": False, "reason": "local_stack_required"},
        )
        self.factory.assert_not_called()

    def test_signup_preserves_existing_file_on_failed_login_and_signup(self):
        self.credentials()
        path = self.state / "yuki-account.json"
        original = path.read_bytes()
        self.client.post.return_value = self.response({"detail": "fixture-password"}, 409)
        self.client.post.side_effect = None
        result = self.run_helper("signup", success=False)
        self.assertEqual(result, {"account": "failed", "reason": "signup_failed"})
        self.assertEqual(path.read_bytes(), original)

    def test_all_subcommands_refuse_missing_local_root(self):
        with override_settings(LOCAL_TEST_ROOT=""):
            for action in ("signup", "link", "chat-plan", "tonight"):
                self.assertEqual(self.run_helper(action, success=False)["reason"], "local_stack_required")
        self.factory.assert_not_called()

    def routine_cli(self, run_status="ok", existing=True):
        """Fake operator CLI: list (with an old copy of the job), rm, add, run, runs."""
        import subprocess

        calls = []

        def fake(argv, env, **kwargs):
            calls.append((argv, env))
            action = argv[2]
            body = {
                "list": {"jobs": [{"id": "old", "declarationKey": "yuki-local:morning"}] if existing else []},
                "rm": {"ok": True},
                "add": {"id": "job-1"},
                "run": {"ok": True, "enqueued": True, "runId": "run-1"},
                "runs": {
                    "entries": [
                        {"runId": "run-0", "action": "finished", "status": "ok", "durationMs": 1},
                        {
                            "runId": "run-1",
                            "action": "finished",
                            "status": run_status,
                            "errorReason": "timeout" if run_status != "ok" else "",
                            "completionStatus": "succeeded" if run_status == "ok" else "failed",
                            "durationMs": 650000,
                        },
                    ]
                },
            }[action]
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

        seed = [
            {
                "name": "Morning Briefing",
                "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "Asia/Tokyo"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "Current date and time: fixture. Morning briefing."},
                "delivery": {"mode": "none"},
                "model": "openrouter/deepseek/deepseek-v4-flash-0731",
            }
        ]
        for context in (
            patch.object(self.helper.subprocess, "run", side_effect=fake),
            patch("apps.orchestrator.config_generator.build_cron_seed_jobs", return_value=seed),
            patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="fixture-gateway-token"),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        return calls

    def run_routine(self, success=True):
        import io

        from django.core.management import call_command

        self.stdout = io.StringIO()
        if success:
            call_command("yuki_local", "routine", "morning", stdout=self.stdout)
        else:
            with self.assertRaises(SystemExit):
                call_command("yuki_local", "routine", "morning", stdout=self.stdout)
        line = self.stdout.getvalue().strip()
        self.assertNotIn("fixture-gateway-token", line)
        return json.loads(line)

    def test_routine_runs_the_real_job_on_the_local_model_and_waits_for_its_run(self):
        calls = self.routine_cli()
        result = self.run_routine()
        self.assertEqual(
            result,
            {
                "proof": "routine",
                "routine": "morning",
                "job": "Morning Briefing",
                "status": "done",
                "detail": "succeeded",
                "seconds": 650,
            },
        )
        actions = [argv[2] for argv, _ in calls]
        self.assertEqual(actions, ["list", "rm", "add", "run", "runs"])
        add = calls[2][0]
        self.assertIn("Current date and time: fixture. Morning briefing.", add)
        self.assertEqual(add[add.index("--declaration-key") + 1], "yuki-local:morning")
        self.assertEqual(add[add.index("--cron") + 1], "0 7 * * *")
        self.assertNotIn("--model", add)  # inherits the local Ollama default; the cloud pin would be rejected
        self.assertEqual(calls[3][0][3], "job-1")
        for argv, env in calls:
            self.assertEqual(env["OPENCLAW_GATEWAY_TOKEN"], "fixture-gateway-token")
            self.assertNotIn("fixture-gateway-token", argv)
            self.assertEqual(argv[argv.index("--port") + 1], "19443")
        self.factory.assert_called()  # same local-stack gate as every other subcommand

    def test_routine_reports_a_failed_run(self):
        self.routine_cli(run_status="error", existing=False)
        result = self.run_routine()
        self.assertEqual((result["status"], result["detail"]), ("failed", "timeout"))

    def test_routine_refuses_missing_local_root(self):
        calls = self.routine_cli()
        with override_settings(LOCAL_TEST_ROOT=""):
            self.assertEqual(self.run_routine(success=False)["reason"], "local_stack_required")
        self.assertEqual(calls, [])

    def test_link_checks_real_persisted_row(self):
        from django.utils import timezone

        from apps.integrations.models import Integration

        self.credentials()
        row = Integration.objects.create(
            tenant=self.tenant, provider="sautai", sautai_user_id=42, linked_at=timezone.now()
        )
        result = self.run_helper("link", "fixture-connect-key\n")
        self.assertEqual(result, {"linked": True, "sautai_user_id": 42, "linked_at": row.linked_at.isoformat()})
        self.client.post.assert_any_call(
            "/api/v1/integrations/sautai/link/", json={"connect_key": "fixture-connect-key"}
        )

    def test_link_rejects_http_status_body_and_missing_row(self):
        self.credentials()
        for body, code, reason in (
            ({}, 400, "link_failed"),
            ({}, 200, "link_not_connected"),
            ([], 200, "invalid_response"),
            ({"status": "connected"}, 200, "link_not_persisted"),
        ):

            def post(url, body=body, code=code, **kwargs):
                return self.response(body, code) if url.endswith("sautai/link/") else self.post(url, **kwargs)

            self.client.post.side_effect = post
            self.assertEqual(
                self.run_helper("link", "fixture-connect-key", success=False), {"linked": False, "reason": reason}
            )

    def test_chat_confirms_twice_and_keeps_ordered_private_evidence(self):
        self.credentials()
        self.replies = ["Please confirm the week.", "Shall I proceed?", "Generation started."]
        self.on_poll = lambda: self.job() if len(self.messages) == 3 else None
        result = self.run_helper("chat-plan", "Plan the week of 2026-09-21.\n", week="2026-09-21")
        self.assertEqual((result["turns"], result["confirm_turns"], result["meal_count"]), (3, 2, 1))
        self.assertEqual((result["correction_turns"], result["must_mention"]), (0, []))
        self.assertEqual(
            [m["text"] for m in self.messages],
            ["Plan the week of 2026-09-21.", self.helper.CONFIRM, self.helper.CONFIRM],
        )
        self.assertEqual({m["thread_id"] for m in self.messages}, {"fixture-thread"})
        path = Path(result["transcript"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        transcript = json.loads(path.read_text())["messages"]
        self.assertEqual([m["role"] for m in transcript], ["yuki", "assistant"] * 3)
        self.assertTrue(all(m["timestamp"] for m in transcript))
        self.client.delete.assert_not_called()

    def test_chat_checks_all_phrases_with_case_and_whitespace_normalized(self):
        self.credentials()
        self.replies = ["PESCATARIAN dinners, THREE\n  COOK\tNIGHTS. Confirm?", "Generation started."]
        self.on_poll = lambda: (
            self.job(user_prompt="pescatarian; three\t cook\n nights") if len(self.messages) == 2 else None
        )
        result = self.run_helper(
            "chat-plan",
            "Pescatarian dinners, three cook nights.",
            week="2026-09-21",
            must_mention=[" Pescatarian ", "three  cook nights"],
        )
        self.assertEqual((result["confirm_turns"], result["correction_turns"], result["turns"]), (1, 0, 2))
        self.assertEqual(result["must_mention"], ["Pescatarian", "three  cook nights"])
        self.assertEqual(self.messages[1]["text"], self.helper.CONFIRM)

    def test_chat_corrects_preview_before_confirming(self):
        self.credentials()
        original = "Plan dinners for 2026-09-21: pescatarian, three cook nights, use my pantry."
        self.replies = ["Prompt: pantry. Confirm?", "Pescatarian, three cook nights. Confirm?", "Started."]
        self.on_poll = lambda: self.job(user_prompt=original) if len(self.messages) == 3 else None
        result = self.run_helper(
            "chat-plan", original, week="2026-09-21", must_mention=["pescatarian", "three cook nights"]
        )
        correction = f"That is not quite what I asked. Please use my request exactly: {original}"
        self.assertEqual([m["text"] for m in self.messages], [original, correction, self.helper.CONFIRM])
        self.assertEqual((result["confirm_turns"], result["correction_turns"], result["turns"]), (1, 1, 3))
        transcript = json.loads(Path(result["transcript"]).read_text())["messages"]
        self.assertEqual(
            [m["text"] for m in transcript if m["role"] == "yuki"], [original, correction, self.helper.CONFIRM]
        )

    def test_chat_three_bad_previews_never_confirms_and_truncates_corrections(self):
        self.credentials()
        original = "Pescatarian dinners " + "x" * 380
        self.replies = ["Prompt: pantry. Confirm?", "I can help with pantry.", "Prompt: pantry. Confirm?"]
        result = self.run_helper("chat-plan", original, week="2026-09-21", must_mention=["pescatarian"], success=False)
        self.assertEqual(result["reason"], "preview_lost_request")
        self.assertEqual((result["proof"], result["status"], result["week"]), ("chat-plan", "failed", "2026-09-21"))
        correction = f"That is not quite what I asked. Please use my request exactly: {original}"[:400]
        self.assertEqual(len(correction), 400)
        self.assertEqual([m["text"] for m in self.messages], [original, correction, correction])
        transcript = json.loads(Path(result["transcript"]).read_text())["messages"]
        self.assertEqual([m["text"] for m in transcript if m["role"] == "yuki"], [original, correction, correction])
        self.helper.time.sleep.assert_not_called()

    def test_chat_two_corrections_leave_both_confirmation_turns_available(self):
        self.credentials()
        self.replies = ["Pantry?", "Pantry?", "Pescatarian. Confirm?", "Pescatarian. Confirm?", "Started."]
        self.on_poll = lambda: self.job(user_prompt="pescatarian") if len(self.messages) == 5 else None
        result = self.run_helper("chat-plan", "Pescatarian", week="2026-09-21", must_mention=["pescatarian"])
        correction = "That is not quite what I asked. Please use my request exactly: Pescatarian"
        self.assertEqual(
            [m["text"] for m in self.messages],
            ["Pescatarian", correction, correction, self.helper.CONFIRM, self.helper.CONFIRM],
        )
        self.assertEqual((result["confirm_turns"], result["correction_turns"], result["turns"]), (2, 2, 5))

    def test_chat_still_checks_preview_after_second_confirmation(self):
        self.credentials()
        self.replies = ["Pescatarian. Confirm?", "Pescatarian. Confirm?", "Pantry?", "Pantry?", "Pantry?"]
        result = self.run_helper(
            "chat-plan", "Pescatarian", week="2026-09-21", must_mention=["pescatarian"], success=False
        )
        self.assertEqual(result["reason"], "preview_lost_request")
        correction = "That is not quite what I asked. Please use my request exactly: Pescatarian"
        self.assertEqual(
            [m["text"] for m in self.messages],
            ["Pescatarian", self.helper.CONFIRM, self.helper.CONFIRM, correction, correction],
        )
        self.helper.time.sleep.assert_not_called()

    def test_chat_rejects_lost_forwarded_prompt_before_waiting(self):
        self.credentials()
        job = self.job(user_prompt="pantry", status="pending")
        self.replies = ["Pescatarian dinners. Confirm?"]
        result = self.run_helper(
            "chat-plan", "Pescatarian dinners", week="2026-09-21", must_mention=["pescatarian"], success=False
        )
        self.assertEqual((result["reason"], result["job_id"]), ("prompt_not_forwarded", str(job.id)))
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(Path(result["transcript"]).is_file())
        self.helper.time.sleep.assert_not_called()

    def test_chat_checks_forwarding_after_confirmation_in_stored_placeholder_form(self):
        self.credentials()
        self.replies = ["Dinners for Yuki. Confirm?", "Generation started."]
        self.on_poll = lambda: self.job(user_prompt="Dinners for [PERSON_1]") if len(self.messages) == 2 else None
        result = self.run_helper(
            "chat-plan", "Dinners for Yuki", week="2026-09-21", must_mention=["Yuki"], success=False
        )
        self.assertEqual(result["reason"], "prompt_not_forwarded")
        self.assertIn("job_id", result)
        self.assertEqual([m["text"] for m in self.messages], ["Dinners for Yuki", self.helper.CONFIRM])
        self.helper.time.sleep.assert_not_called()

    def test_chat_invalid_must_mention_fails_before_http(self):
        for phrases in (["x"] * 6, [""], [" \t "], ["x" * 61], ["cook\nnights"], ["cook\rnights"], ["a\u2028b"]):
            with self.subTest(phrases=phrases):
                result = self.run_helper("chat-plan", week="2026-09-21", must_mention=phrases, success=False)
                self.assertEqual(result["reason"], "invalid_must_mention")
        self.factory.assert_not_called()
        self.client.post.assert_not_called()
        self.client.get.assert_not_called()

    def test_chat_must_mention_repeatable_flag_and_valid_boundaries(self):
        from django.core.management import call_command

        self.credentials()
        phrases = ["x", "y" * 60, "pantry", "dinners", "pescatarian"]
        self.replies = [" ".join(phrases)]
        self.on_poll = lambda: self.job(user_prompt=" ".join(phrases))
        with patch.object(self.helper.sys, "stdin", self.stdin):
            self.stdin.write("Plan my dinners")
            self.stdin.seek(0)
            call_command(
                "yuki_local",
                "chat-plan",
                "--week",
                "2026-09-21",
                *[arg for phrase in phrases for arg in ("--must-mention", f" {phrase} ")],
                stdout=self.stdout,
            )
        result = json.loads(self.stdout.getvalue())
        self.assertEqual(result["must_mention"], phrases)
        self.assertEqual(result["confirm_turns"], 0)

    def test_chat_no_job_prompts_confirmation_without_question(self):
        self.credentials()
        self.replies = ["I can help with that.", "Generation started."]
        self.on_poll = lambda: self.job() if len(self.messages) == 2 else None
        result = self.run_helper("chat-plan", "Plan this week", week="2026-09-21")
        self.assertEqual(result["confirm_turns"], 1)

    def test_chat_job_ready_needs_no_confirmation(self):
        self.credentials()
        self.on_poll = self.job
        self.assertEqual(self.run_helper("chat-plan", "Plan this week", week="2026-09-21")["confirm_turns"], 0)

    def test_chat_wrong_week_fails_with_both_dates_and_evidence(self):
        self.credentials()
        self.on_poll = lambda: self.job("2026-09-28")
        result = self.run_helper("chat-plan", "Plan this week", week="2026-09-21", success=False)
        self.assertEqual(
            (result["reason"], result["week"], result["job_week"]), ("week_mismatch", "2026-09-21", "2026-09-28")
        )
        self.assertTrue(Path(result["transcript"]).is_file())
        self.assertEqual(len(self.messages), 1)

    def test_chat_rejects_failed_and_invalid_ready_jobs(self):
        self.credentials()
        for changes, reason in (
            ({"status": "failed"}, "job_failed"),
            ({"addressed_by": "email"}, "job_result_invalid"),
            ({"result": {}}, "job_result_invalid"),
            ({"error": "private error"}, "job_result_invalid"),
        ):
            self.replies = ["Generation started."]
            self.on_poll = lambda changes=changes: self.job(**changes)
            self.assertEqual(self.run_helper("chat-plan", "Plan", week="2026-09-21", success=False)["reason"], reason)

    def test_chat_main_thread_and_tenant_gate_are_enforced(self):
        self.credentials()
        self.client.get.side_effect = lambda *a, **k: self.response(
            {"id": str(self.tenant.id), "is_synthetic": False, "is_eval_sink": False}
        )
        self.assertEqual(
            self.run_helper("chat-plan", "Plan", week="2026-09-21", success=False)["reason"], "tenant_gate_failed"
        )
        self.client.get.side_effect = self.get
        self.client.post.side_effect = lambda url, **kw: (
            self.response({"id": "main", "is_main": True}, 201) if url.endswith("threads/") else self.post(url, **kw)
        )
        self.assertEqual(
            self.run_helper("chat-plan", "Plan", week="2026-09-21", success=False)["reason"], "refuse_main_thread"
        )
        self.assertFalse(self.messages)

    def test_tonight_reports_names_and_view_week(self):
        self.credentials()
        self.assertEqual(
            self.run_helper("tonight"),
            {"linked": True, "meals_today": 1, "meal_names": ["Soba"], "week_start": "2026-09-21"},
        )

    def test_tonight_empty_day_is_not_a_link_failure(self):
        self.credentials()
        self.client.get.side_effect = lambda url, **kw: (
            self.response({"linked": True, "meals": [], "week_start": "2026-09-21", "empty_reason": "no_meal_today"})
            if url.endswith("meals/today/")
            else self.get(url, **kw)
        )
        result = self.run_helper("tonight")
        self.assertEqual((result["linked"], result["meals_today"], result["empty_reason"]), (True, 0, "no_meal_today"))


@override_settings(
    DEBUG=True,
    LOCAL_TEST_ROOT="/nonexistent/local-test-root",
    SAUTAI_M2M_BASE_URL="http://127.0.0.1:8000",
    SAUTAI_PLATFORM_SECRET="fixture-secret",
)
class LocalOverridesTests(TestCase):
    def setUp(self):
        from apps.integrations import sautai_client

        self.client_module = sautai_client
        self.original = sautai_client.REQUEST_TIMEOUT_SECONDS
        self.addCleanup(setattr, sautai_client, "REQUEST_TIMEOUT_SECONDS", self.original)
        self.apply = runpy.run_path(str(settings.BASE_DIR / "deploy/local-test/local_overrides.py"))["apply"]
        self.env = patch.dict(os.environ, AZURE_MOCK="true")
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_apply_extends_legacy_generate_timeout(self):
        from apps.integrations.models import Integration, SautaiMealPlanJob
        from apps.integrations.sautai_client import (
            ASYNC_CONTRACT_REQUEST_DECISION_KEY,
            RetryableSautaiError,
            call_sautai_generate_plan,
        )

        self.apply()
        self.assertEqual(self.client_module.REQUEST_TIMEOUT_SECONDS, 1700.0)
        user = User.objects.create(email="local-overrides-fixture@example.invalid")
        tenant = Tenant.objects.create(user=user, is_synthetic=True, is_eval_sink=False, sautai_enabled=True)
        Integration.objects.create(
            tenant=tenant, provider=Integration.Provider.SAUTAI, status=Integration.Status.ACTIVE, sautai_user_id=501
        )
        job = SautaiMealPlanJob.objects.create(
            tenant=tenant, week_start=date(2026, 9, 28), funnel={ASYNC_CONTRACT_REQUEST_DECISION_KEY: False}
        )
        with (
            patch("apps.integrations.sautai_client.httpx.post", side_effect=httpx.ReadTimeout("fixture")) as post,
            self.assertRaises(RetryableSautaiError),
        ):
            call_sautai_generate_plan(job)
        self.assertEqual(post.call_args.kwargs["timeout"], 1700.0)

    def test_apply_refuses_outside_local_stack(self):
        cases = [
            override_settings(LOCAL_TEST_ROOT=""),
            override_settings(DEBUG=False),
            patch.dict(os.environ, AZURE_MOCK="false"),
            patch.object(self.client_module, "REQUEST_TIMEOUT_SECONDS", new=None),
        ]
        for index, context in enumerate(cases):
            with self.subTest(case=index), context:
                before = getattr(self.client_module, "REQUEST_TIMEOUT_SECONDS", None)
                if index == 3:
                    del self.client_module.REQUEST_TIMEOUT_SECONDS
                    before = "missing"
                with self.assertRaises(RuntimeError):
                    self.apply()
                after = getattr(self.client_module, "REQUEST_TIMEOUT_SECONDS", "missing")
                self.assertEqual(after, before)
        self.assertEqual(self.client_module.REQUEST_TIMEOUT_SECONDS, self.original)
