#!/usr/bin/env python3
"""Own and clean one isolated Docker stack; host-side stdlib only."""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
STATE = HERE / ".state"
LABEL = f"org.nbhd.stage={ROOT}"


def command(*args, capture=False, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=capture, **kwargs)


def docker(*args, **kwargs):
    return command("docker", *args, **kwargs)


def state():
    return json.loads((STATE / "stack.json").read_text())


def runtime(*args):
    return docker("exec", "nbhd-stage-django", "python", "/stage/runtime.py", *args, capture=True).stdout


def safe_logs(name):
    result = docker("logs", name, capture=True)
    output = result.stdout + result.stderr
    environment = json.loads(docker("inspect", "--format", "{{json .Config.Env}}", name, capture=True).stdout)
    for entry in environment:
        key, _, value = entry.partition("=")
        if any(word in key for word in ("KEY", "TOKEN", "SECRET", "SEED")) and len(value) >= 8:
            output = output.replace(value, "[secret omitted]")
    return output


def request(path, data=None, token=None, base_url=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        (base_url or state()["url"]) + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers,
    )

    # No redirects or inherited HTTP proxy for local authenticated requests.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=20) as response:
        return json.load(response)


def down():
    # Ownership label, rather than name prefix, protects other concurrent worktrees.
    for kind, listing, removal in (
        ("containers", ("ps", "-aq"), ("rm", "-f", "-v")),
        ("networks", ("network", "ls", "-q"), ("network", "rm")),
        ("volumes", ("volume", "ls", "-q"), ("volume", "rm")),
    ):
        ids = docker(*listing, "--filter", f"label={LABEL}", capture=True).stdout.split()
        if ids:
            # Shared namespace container must go before Django.
            if kind == "containers":
                for item in ("nbhd-stage-assistant", "nbhd-stage-django"):
                    owned = docker(
                        "ps", "-aq", "--filter", f"label={LABEL}", "--filter", f"name=^/{item}$", capture=True
                    ).stdout.split()
                    if owned:
                        docker(*removal, *owned, capture=True)
                ids = docker(*listing, "--filter", f"label={LABEL}", capture=True).stdout.split()
            if ids:
                docker(*removal, *ids, capture=True)
    if STATE.exists():
        # Atomic share files belong to the image's UID 1000. A root helper can
        # remove them even if Django has crashed, without broadening file modes.
        docker(
            "run",
            "--rm",
            "--label",
            LABEL,
            "--name",
            "nbhd-stage-cleanup",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={STATE},dst=/cleanup",
            "python:3.12-slim",
            "python",
            "-c",
            "from pathlib import Path; import shutil; "
            "[(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) for p in Path('/cleanup').iterdir()]",
            capture=True,
        )
    if STATE.exists():
        STATE.rmdir()
    shutil.rmtree(HERE / "__pycache__", ignore_errors=True)
    print(json.dumps({"status": "down", "owned_resources_remaining": 0}))


def wait_until(predicate, description, timeout=360):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {description}; inspect the owned container logs")


def up(checkout, flags):
    checkout = Path(checkout).resolve()
    if not checkout.is_relative_to(ROOT):
        raise ValueError("Checkout must be inside this clone")
    sha = command("git", "-C", str(checkout), "rev-parse", "HEAD", capture=True).stdout.strip()
    if command("git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no", capture=True).stdout:
        raise ValueError("Use a clean tracked checkout so image SHA identifies its contents")
    overrides = {}
    settings_source = (checkout / "config/settings/base.py").read_text()
    for flag in flags:
        name, value = flag.split("=", 1)
        # Feature rollout flags only: never permit credential, URL, model or safety overrides.
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*_TENANT_IDS", name) or f'"{name}"' not in settings_source:
            raise ValueError("--flag accepts known *_TENANT_IDS rollout settings only")
        if any(char in value for char in "\r\n"):
            raise ValueError("Invalid flag value")
        overrides[name] = value
    if STATE.exists():
        raise ValueError("Run scripts/stage/down.sh before starting another stack")
    if docker("ps", "-aq", "--filter", "name=^/nbhd-stage-", capture=True).stdout.strip():
        raise ValueError("An nbhd-stage stack already exists; its owner must stop it first")
    STATE.mkdir(mode=0o700)
    (STATE / "share").mkdir(mode=0o755)
    keyfile = STATE / "runtime.env"
    short = sha[:8]
    images = {kind: f"nbhd-stage-{kind}:{short}" for kind in ("django", "openclaw")}
    metadata = {"sha": sha, "checkout": str(checkout), "images": images, "flags": overrides}
    (STATE / "stack.json").write_text(json.dumps(metadata))
    try:
        for kind, image in images.items():
            exists = (
                subprocess.run(
                    ["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                ).returncode
                == 0
            )
            if not exists:
                print(f"Building {image}", flush=True)
                docker(
                    "build",
                    "--force-rm",
                    "-f",
                    str(HERE / "Dockerfile.django" if kind == "django" else checkout / "Dockerfile.openclaw"),
                    "-t",
                    image,
                    str(checkout),
                )
        docker("network", "create", "--internal", "--label", LABEL, "nbhd-stage-private", capture=True)
        docker("network", "create", "--label", LABEL, "nbhd-stage-egress", capture=True)
        docker("volume", "create", "--label", LABEL, "nbhd-stage-postgres", capture=True)
        docker("volume", "create", "--label", LABEL, "nbhd-stage-redis", capture=True)
        common = ("run", "-d", "--label", LABEL, "--network", "nbhd-stage-private")
        docker(
            *common,
            "--name",
            "nbhd-stage-postgres",
            "--network-alias",
            "postgres",
            "-e",
            "POSTGRES_USER=stage",
            "-e",
            "POSTGRES_PASSWORD=stage-local-only",
            "-e",
            "POSTGRES_DB=stage",
            "-v",
            "nbhd-stage-postgres:/var/lib/postgresql/data",
            "pgvector/pgvector:pg16",
            capture=True,
        )
        docker(
            *common,
            "--name",
            "nbhd-stage-redis",
            "--network-alias",
            "redis",
            "-v",
            "nbhd-stage-redis:/data",
            "redis:7-alpine",
            capture=True,
        )
        docker(
            "run",
            "-d",
            "--label",
            LABEL,
            "--network",
            "nbhd-stage-egress",
            "--name",
            "nbhd-stage-proxy",
            "-p",
            "127.0.0.1::8000",
            "-p",
            "127.0.0.1::8080",
            "--mount",
            f"type=bind,src={HERE},dst=/stage,readonly",
            "python:3.12-slim",
            "python",
            "/stage/egress.py",
            capture=True,
        )
        docker("network", "connect", "--alias", "egress", "nbhd-stage-private", "nbhd-stage-proxy", capture=True)
        wait_until(
            lambda: docker("exec", "nbhd-stage-postgres", "pg_isready", "-U", "stage", capture=True).returncode == 0,
            "Postgres",
            60,
        )
        values = {
            "NBHD_STAGE": "1",
            "DJANGO_SETTINGS_MODULE": "settings",
            "PYTHONPATH": "/stage:/app",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SECRET_KEY": secrets.token_urlsafe(48),
            "NBHD_INTERNAL_API_KEY": secrets.token_urlsafe(48),
            "LOCAL_TEST_KEK_SEED": secrets.token_urlsafe(48),
            "NBHD_TENANT_ID": str(uuid.uuid4()),
            "AZURE_MOCK": "true",
            "DEBUG": "true",
            "DATABASE_URL": "postgres://stage:stage-local-only@postgres:5432/stage",
            "REDIS_URL": "redis://redis:6379/0",
            "API_BASE_URL": "http://127.0.0.1:8000",
            "NBHD_API_BASE_URL": "http://127.0.0.1:8000",
            "OPENROUTER_API_KEY": (Path.home() / ".config/openrouter/key").read_text().strip(),
            "OPENROUTER_PER_TENANT_KEYS_ENABLED": "false",
            "QSTASH_TOKEN": "",
            "HF_HUB_OFFLINE": "1",
            "PII_MODEL_PATH": "/nonexistent-stage-detector",
            "HTTP_PROXY": "http://egress:8888",
            "HTTPS_PROXY": "http://egress:8888",
            "NO_PROXY": "127.0.0.1,localhost,postgres,redis",
            "NODE_USE_ENV_PROXY": "1",
            "STAGE_IMAGE_TAG": "2026.9.4-" + short,
            "OPENCLAW_JOURNAL_PLUGIN_ID": "nbhd-journal-tools",
            "OPENCLAW_USAGE_PLUGIN_ID": "nbhd-usage-reporter",
            **overrides,
        }
        if not values["OPENROUTER_API_KEY"] or any("\n" in value or "\r" in value for value in values.values()):
            raise ValueError("Missing key or invalid environment value")
        with open(keyfile, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
        docker(
            *common,
            "--name",
            "nbhd-stage-django",
            "--network-alias",
            "django",
            "--env-file",
            str(keyfile),
            "--mount",
            f"type=bind,src={HERE},dst=/stage,readonly",
            "--mount",
            f"type=bind,src={STATE},dst=/stage-state",
            "--mount",
            f"type=bind,src={STATE / 'share'},dst=/home/node/.openclaw",
            images["django"],
            "python",
            "/stage/runtime.py",
            "serve",
            capture=True,
        )
        ports = json.loads(
            docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", "nbhd-stage-proxy", capture=True).stdout
        )
        metadata.update(
            url="http://127.0.0.1:" + ports["8000/tcp"][0]["HostPort"],
            assistant_url="http://127.0.0.1:" + ports["8080/tcp"][0]["HostPort"],
        )
        (STATE / "stack.json").write_text(json.dumps(metadata))
        wait_until(lambda: (STATE / "seed.json").exists(), "migration, seed and real config render")
        # runtime.py has handed initial share files to the image's node UID.
        docker(
            "run",
            "-d",
            "--label",
            LABEL,
            "--name",
            "nbhd-stage-assistant",
            "--network",
            "container:nbhd-stage-django",
            "--env-file",
            str(keyfile),
            "--mount",
            f"type=bind,src={STATE / 'share'},dst=/home/node/.openclaw",
            images["openclaw"],
            capture=True,
        )
        keyfile.unlink()
        wait_until(
            lambda: "http server listening" in docker("logs", "nbhd-stage-assistant", capture=True).stdout,
            "OpenClaw HTTP listener",
        )
        wait_until(lambda: request("/health/").get("status") in ("ok", "healthy"), "Django health")
        # Proxy readiness includes the underlying gateway, rather than TCP alone.
        wait_until(
            lambda: request("/proxy-health", base_url=metadata["assistant_url"]).get("gateway") is True,
            "underlying OpenClaw gateway health",
        )
        wait_until(
            lambda: "nbhd-doc-taint-guard" in docker("logs", "nbhd-stage-assistant", capture=True).stdout,
            "doc taint guard plugin",
        )
        print(json.dumps({"status": "healthy", **metadata, "seed": json.loads((STATE / "seed.json").read_text())}))
    except BaseException:
        if keyfile.exists():
            keyfile.unlink()
        # Leave failed runtime for inspection; down owns every resource and file.
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("up")
    start.add_argument("checkout")
    start.add_argument("--flag", action="append", default=[])
    commands.add_parser("down")
    logs = commands.add_parser("logs")
    logs.add_argument("pattern", nargs="?")
    args = parser.parse_args()
    if args.action == "up":
        up(args.checkout, args.flag)
    elif args.action == "down":
        down()
    else:
        for line in safe_logs("nbhd-stage-assistant").splitlines():
            if not args.pattern or args.pattern in line:
                print(line)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        # Never stringify a subprocess exception: a future caller may add a token.
        print(f"Stage subprocess failed (exit {exc.returncode}); inspect local logs", file=sys.stderr)
        raise SystemExit(1) from None
