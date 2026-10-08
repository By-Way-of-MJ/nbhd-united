"""Static guards for model-provider egress and embedding caller scope."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOYABLE_ROOTS = ("apps", "config", "runtime", "scripts")
_SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".mjs",
    ".cjs",
    ".sh",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
}
_DEPENDENCY_LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
_VENDORED_RUNTIME_DIRS = {"pinned-runtime", "vendor", "vendored", "third-party", "third_party"}

_MODEL_HOSTNAME = re.compile(
    r"api\.openai\.com|openai\.com/v1|api\.anthropic\.com|"
    r"generativelanguage\.googleapis\.com|aiplatform\.googleapis\.com|"
    r"api\.mistral\.ai|api\.deepseek\.com|api\.groq\.com|deepgram|elevenlabs",
    re.IGNORECASE,
)
_SDK_USAGE = re.compile(
    r"from\s+openai\s+import|\bOpenAI\s*\(|\bAnthropic\s*\(|@anthropic-ai/sdk|"
    r"(?:from|require\s*\()\s*['\"]openai['\"]|['\"]openai['\"]\s*:",
)
_PROVIDER_KEY_READ = re.compile(
    r"settings\.(?:OPENAI|ANTHROPIC)_API_KEY|"
    r"getattr\(settings,\s*['\"](?:OPENAI|ANTHROPIC)_API_KEY['\"]|"
    r"os\.(?:getenv|environ\.get)\(\s*['\"](?:OPENAI|ANTHROPIC)_API_KEY['\"]|"
    r"process\.env\.(?:OPENAI|ANTHROPIC)_API_KEY|"
    r"env\(\s*['\"](?:OPENAI|ANTHROPIC)_API_KEY['\"]",
)

# PR-B2 removes the azure_client/image-gen runtime seams at integration, where
# these entries become tolerated no-ops. On PR-A alone they remain real, so
# keeping exact path+rule pairs makes any new use fail. Django's settings
# declaration is configuration, not a provider call.
_ALLOWED_MATCHES = {
    ("config/settings/base.py", "provider_key_read"),
    ("apps/orchestrator/azure_client.py", "provider_key_read"),
    ("runtime/openclaw/plugins/nbhd-image-gen/index.js", "model_hostname"),
    ("runtime/openclaw/plugins/nbhd-image-gen/index.js", "provider_key_read"),
    # Named exception (MJ 2026-10-08): a profile photo is sent to OpenAI's moderation
    # model before it is stored, so an unsafe photo never reaches neighbors. It is the
    # one direct provider call kept on purpose; replace it with a local safety model
    # rather than widening this list. The photo is never given to the assistant.
    ("apps/friends/photos.py", "provider_key_read"),
    ("apps/friends/photos.py", "sdk_usage"),
}

_EGRESS_PATTERNS = (
    ("model_hostname", _MODEL_HOSTNAME),
    ("sdk_usage", _SDK_USAGE),
    ("provider_key_read", _PROVIDER_KEY_READ),
)


def _is_vendored_dependency(relative: Path) -> bool:
    vendored_runtime_manifest = (
        relative.name in {"package.json", "package-lock.json"}
        and bool(relative.parts)
        and relative.parts[0] == "runtime"
        and any(part in _VENDORED_RUNTIME_DIRS for part in relative.parts[1:-1])
    )
    # Vendored third-party dependencies and their manifests are not our egress surface.
    return (
        "node_modules" in relative.parts
        or relative.parts[:3] == ("runtime", "openclaw", "pinned-runtime")
        or relative.name in _DEPENDENCY_LOCKFILES
        or vendored_runtime_manifest
    )


def _is_deployable(relative: Path) -> bool:
    if _is_vendored_dependency(relative):
        return False

    root_special = len(relative.parts) == 1 and (
        relative.name.startswith("Dockerfile") or "entrypoint" in relative.name
    )
    deployable_source = (
        bool(relative.parts)
        and relative.parts[0] in _DEPLOYABLE_ROOTS
        and relative.suffix in _SOURCE_SUFFIXES
        and "tests" not in relative.parts
        and not relative.name.startswith("test")
        and ".test." not in relative.name
    )
    return root_special or deployable_source


_UNTRACKED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".claude", ".dd"}


def _candidate_relatives():
    """Tracked files when git can answer; otherwise every file on disk.

    The docker gate and CI test a snapshot with no ``.git``, where ``git ls-files``
    exits 128. Walking the tree there scans a superset of the tracked files, so the
    guard still runs (and can only be stricter) instead of erroring out.
    """
    try:
        tracked = subprocess.run(
            ["git", "-C", str(_ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        for directory, subdirectories, filenames in os.walk(_ROOT):
            subdirectories[:] = [name for name in subdirectories if name not in _UNTRACKED_DIRS]
            for filename in filenames:
                yield (Path(directory) / filename).relative_to(_ROOT)
        return
    for encoded_relative in tracked.split(b"\0"):
        if encoded_relative:
            yield Path(encoded_relative.decode("utf-8"))


def _deployable_files():
    for relative in _candidate_relatives():
        path = _ROOT / relative
        if not path.is_file():
            continue
        if _is_deployable(relative):
            yield path


def _find_offenders():
    offenders = []
    for path in _deployable_files():
        relative = path.relative_to(_ROOT).as_posix()
        for line_number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue
            for rule, pattern in _EGRESS_PATTERNS:
                if pattern.search(line) and (relative, rule) not in _ALLOWED_MATCHES:
                    offenders.append((relative, line_number, rule))
    return offenders


class ModelEgressStaticGuard(SimpleTestCase):
    def test_no_unapproved_provider_egress(self):
        offenders = _find_offenders()
        self.assertEqual(
            offenders,
            [],
            "Direct model-provider egress, SDK usage, or provider-key reads must "
            f"use an explicitly reviewed seam. Offenders: {offenders}",
        )

    def test_tracked_offender_is_scanned_and_untracked_artifact_is_ignored(self):
        runtime_dir = _ROOT / "runtime/openclaw"
        with tempfile.TemporaryDirectory(dir=runtime_dir, prefix="egress-guard-") as directory:
            tracked_offender = Path(directory) / "tracked-offender.js"
            untracked_artifact = Path(directory) / "untracked-artifact.js"
            tracked_offender.write_text('const OpenAI = require("openai");\n')
            untracked_artifact.write_text('const OpenAI = require("openai");\n')
            tracked_relative = tracked_offender.relative_to(_ROOT).as_posix()
            untracked_relative = untracked_artifact.relative_to(_ROOT).as_posix()
            git_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{tracked_relative}\0".encode())

            with patch.object(subprocess, "run", return_value=git_result):
                deployable = set(_deployable_files())
                offenders = _find_offenders()

            self.assertIn(tracked_offender, deployable)
            self.assertNotIn(untracked_artifact, deployable)
            self.assertIn((tracked_relative, 1, "sdk_usage"), offenders)
            self.assertFalse(any(path == untracked_relative for path, _line, _rule in offenders))

    def test_vendored_dependency_manifests_are_excluded(self):
        excluded = (
            "runtime/openclaw/pinned-runtime/package-lock.json",
            "runtime/openclaw/pinned-runtime/package.json",
            "runtime/openclaw/node_modules/openai/index.js",
            "apps/example/package-lock.json",
            "runtime/vendor/example/package.json",
            "scripts/pnpm-lock.yaml",
            "config/yarn.lock",
        )
        for relative in excluded:
            with self.subTest(relative=relative):
                self.assertFalse(_is_deployable(Path(relative)))

        self.assertTrue(_is_deployable(Path("runtime/openclaw/nbhd-transcribe.js")))
        self.assertTrue(_is_deployable(Path("runtime/openclaw/plugins/nbhd-example/index.mjs")))

    def test_six_embedding_callers_all_pass_tenant(self):
        expected = {
            "apps/lessons/services.py": 2,
            "apps/journal/extraction.py": 1,
            "apps/journal/embedding.py": 1,
            "apps/journal/workspace_services.py": 1,
            "apps/router/poller.py": 1,
        }
        found: dict[str, int] = {}
        missing_tenant = []
        for relative, count in expected.items():
            tree = ast.parse((_ROOT / relative).read_text())
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "generate_embedding"
            ]
            found[relative] = len(calls)
            missing_tenant.extend(
                (relative, node.lineno)
                for node in calls
                if not any(keyword.arg == "tenant" for keyword in node.keywords)
            )

        self.assertEqual(found, expected)
        self.assertEqual(missing_tenant, [])

    def test_all_openrouter_http_callers_use_mandatory_builder(self):
        request_modules = {
            "apps/common/openrouter.py",
            "apps/lessons/services.py",
            "apps/router/transcription.py",
        }
        for relative in request_modules:
            source = (_ROOT / relative).read_text()
            self.assertIn("build_openrouter_body(", source, relative)

        migrated_chat_clients = {
            "apps/lessons/cluster_naming.py",
            "apps/lessons/copilot.py",
            "apps/lessons/tutoring.py",
            "apps/lessons/management/commands/rewrite_lessons_actionable.py",
        }
        for relative in migrated_chat_clients:
            source = (_ROOT / relative).read_text()
            self.assertIn("chat_completion(", source, relative)
            self.assertNotIn("requests.post(", source, relative)
