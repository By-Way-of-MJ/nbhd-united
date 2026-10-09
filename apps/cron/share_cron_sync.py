"""Signed cron file for the OpenClaw 2026.9.4 in-container cron sync.

2026.9.4 gates the agent-tool gateway cron.* path (``POST /tools/invoke`` with
``tool=cron`` requires an admitted operational run instance), so Django can no
longer push crons into the container over HTTP. For 9.4 tenants we instead write
the desired managed-cron set to a **signed** ``nbhd-crons.json`` on the tenant's
share; the in-container helper (``runtime/openclaw/nbhd-cron-sync.mjs``) applies
it via the ungated operator CLI. Once a job is in the container's SQLite it fires
as an admitted run, so delivery works. See CONTINUITY_openclaw_9_4_cron_sync.md.

SECURITY: the file is HMAC-SHA256 signed with ``NBHD_INTERNAL_API_KEY`` (the same
secret the gateway authenticates Django with). The container refuses any
unsigned/forged file, and independently refuses any non-message payload
(command/script) — so this transport cannot be used to schedule shell.
"""

from __future__ import annotations

import hmac
import json
import logging
from hashlib import sha256

logger = logging.getLogger(__name__)

_CRONS_FILE = "nbhd-crons.json"
_STATE_FILE = "nbhd-cron-state.json"
# The helper rewrites an unchanged state file every 60s and polls every ~25s, so
# three minutes means it has missed at least two heartbeats. Older than this is
# "cron state unknown" — the same answer a failed live cron.list gave.
CRON_STATE_MAX_AGE_SECONDS = 180


def tenant_uses_file_cron_sync(tenant) -> bool:
    """True when the tenant's OpenClaw image is >= 2026.9.4 (gated cron RPC), so
    crons must be delivered via the signed share file instead of gateway RPC."""
    from apps.orchestrator.tool_policy import _parse_version

    version = str(getattr(tenant, "openclaw_version", "") or "")
    return _parse_version(version) >= (2026, 9, 4)


def _desired_jobs(tenant) -> list[dict]:
    """The crons this tenant's container should be running, in the gateway job
    shape, each stamped with a stable ``declarationKey`` the container upserts by.

    Includes: (a) reconciler-owned recurring managed crons (skipping agent-owned
    and system self-cleaning ones), and (b) enabled one-shot ``at`` crons whose
    fire time is still in the future — a fired/stale one-shot is excluded so the
    container removes it and never re-adds a past reminder.
    """
    import time

    from apps.cron.models import CronJob
    from apps.cron.pending_at_views import _at_fires_at_ms
    from apps.orchestrator.cron_reconcile import _is_unmanaged_cron, _row_to_cron_dict

    now_ms = int(time.time() * 1000)
    jobs: list[dict] = []
    for row in CronJob.objects.filter(tenant=tenant, enabled=True).order_by("id"):
        job = _row_to_cron_dict(row)
        schedule = job.get("schedule") or {}
        if schedule.get("kind") == "at":
            fire_ms = _at_fires_at_ms(job)
            if fire_ms is None or fire_ms <= now_ms:
                continue  # fired / stale / unparseable one-shot — do not (re)add
        else:
            if not getattr(row, "managed", False) or _is_unmanaged_cron(row.name):
                continue  # leave agent-owned and system self-cleaning crons alone
        job["declarationKey"] = f"nbhd:{row.id}"
        jobs.append(_payload_model(job))
    return jobs + _fuel_jobs(tenant)


def _fuel_jobs(tenant) -> list[dict]:
    """The CURRENT ``_fuel:*`` set, computed by the Fuel reconciler.

    ``_fuel:*`` CronJob rows are a stale mirror (old plans linger there); the
    desired set comes from the Fuel models. 9.4 gates the gateway ``cron.add``
    the Fuel reconciler used, so the signed file is the only way these reach a
    9.4 container. The key is stable per name, so an edited prep cron replaces
    its predecessor and a dropped plan's cron is removed by the writer.
    """
    from apps.orchestrator.cron_drift import strip_date_line
    from apps.orchestrator.fuel_cron import _desired_fuel_crons

    jobs = []
    for job in _desired_fuel_crons(tenant):
        job = dict(job)
        # Fuel messages are rebuilt on every call with a minute-stamped
        # "Current date and time: ... SNAPSHOT" first paragraph (the prompt
        # says never to use it; the runtime appends the live clock at fire
        # time). Left in, every file rewrite changes the job, so the 9.4
        # writer re-adds it each pass and verify never matches (eval-behavior
        # canary 2026-09-26). The 5.28 reconciler strips it the same way.
        payload = job.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            job["payload"] = {**payload, "message": strip_date_line(payload["message"])}
        job["declarationKey"] = "nbhd:fuel:" + sha256(job["name"].encode("utf-8")).hexdigest()[:16]
        jobs.append(_payload_model(job))
    return jobs


def _payload_model(job: dict) -> dict:
    """Carry a top-level ``model`` pin as ``payload.model``.

    System crons stamp their tier/user-preference model at the top level
    (``config_generator``); 5.28's gateway ``cron.add`` folded it into
    ``payload.model``. The 9.4 writer only emits ``--model`` from
    ``payload.model``, so without this the pin is silently dropped and the
    job runs on the chat primary. An explicit ``payload.model`` wins.
    """
    model = job.pop("model", None)
    if model is None:
        return job
    payload = job.get("payload")
    if (
        isinstance(model, str)
        and model
        and isinstance(payload, dict)
        and payload.get("kind") == "agentTurn"
        and payload.get("model") in (None, model)
    ):
        job["payload"] = {**payload, "model": model}
    else:
        logger.warning("signed crons: dropped unmappable top-level model pin on %r", job.get("name"))
    return job


def build_signed_crons_doc(tenant) -> tuple[bytes, int]:
    """Return the ``(bytes, job_count)`` of the signed ``nbhd-crons.json`` body.

    The signed envelope is ``{"signed": <exact JSON string of the jobs array>,
    "sig": HMAC-SHA256(<tenant gateway key>, signed)}``. The key is the tenant's
    own gateway token (``get_gateway_token_for_tenant`` — the per-tenant
    ``internal_api_key``, i.e. the container's ``NBHD_INTERNAL_API_KEY``), NOT the
    shared platform setting: post-2026-05-12 each container binds a per-tenant key
    (``tenant-<uuid>-internal-key``), so signing with the shared value would fail
    verification in the container. The container verifies the HMAC over ``signed``
    and then parses ``signed`` — it never re-serializes, so no cross-language JSON
    canonicalization is required.
    """
    from apps.cron.gateway_client import get_gateway_token_for_tenant

    key = get_gateway_token_for_tenant(tenant)
    if not key:
        raise RuntimeError(f"no gateway token for tenant {tenant.id} to sign the crons file")
    jobs = _desired_jobs(tenant)
    signed = json.dumps(jobs, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    sig = hmac.new(key.encode("utf-8"), signed.encode("utf-8"), sha256).hexdigest()
    doc = json.dumps({"signed": signed, "sig": sig}, separators=(",", ":"), ensure_ascii=False)
    return doc.encode("utf-8"), len(jobs)


def write_tenant_crons_file(tenant) -> int:
    """Write the signed ``nbhd-crons.json`` to the tenant's share. Returns the
    number of managed crons written. Uploaded as ``data=`` (not ``text=``) so the
    signed bytes are never mutated by share-text sanitization (which would break
    the signature)."""
    from apps.orchestrator.azure_client import _put_share_file

    data, count = build_signed_crons_doc(tenant)
    _put_share_file(str(tenant.id), _CRONS_FILE, data=data, ensure_dirs=False)
    logger.info("write_tenant_crons_file: wrote %d managed cron(s) for tenant %s", count, tenant.id)
    return count


def read_container_cron_jobs(tenant) -> list[dict]:
    """The container's ENABLED cron jobs, from the helper's state readback.

    9.4 gates Django's ``cron.list`` along with every other cron.* call, so the
    in-container helper reports each pass to ``nbhd-cron-state.json`` (metadata
    only: id, name, enabled, schedule, state timing — never payloads). Raises
    ``GatewayError`` when the file is absent, unreadable, or stale, so callers
    keep their existing "cron state unknown" handling.
    """
    import time

    from apps.cron.gateway_client import GatewayError
    from apps.orchestrator.azure_client import download_workspace_file_binary

    try:
        raw = download_workspace_file_binary(str(tenant.id), _STATE_FILE)
    except Exception as exc:
        raise GatewayError(f"cron state file unreadable for tenant {tenant.id}") from exc
    if raw is None:
        raise GatewayError(f"no cron state file for tenant {tenant.id}", unavailable=True)
    try:
        doc = json.loads(raw)
        written_at_ms = int(doc["writtenAtMs"])
        jobs = doc["jobs"]
    except (ValueError, TypeError, KeyError) as exc:
        raise GatewayError(f"cron state file malformed for tenant {tenant.id}") from exc
    if not isinstance(jobs, list):
        raise GatewayError(f"cron state file malformed for tenant {tenant.id}")
    age_seconds = time.time() - written_at_ms / 1000
    if age_seconds > CRON_STATE_MAX_AGE_SECONDS:
        raise GatewayError(f"cron state file stale for tenant {tenant.id} ({int(age_seconds)}s old)")
    return jobs
