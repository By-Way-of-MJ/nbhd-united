"""QStash task wrappers for billing cron jobs."""


def refresh_infra_costs_task():
    from apps.billing.infra_cost_service import refresh_infra_costs

    return refresh_infra_costs()


def snapshot_donations_monthly_task():
    """Record each revenue-generating tenant's (paying subscribers AND credit
    top-up buyers) revenue-percentage donation for the just-closed month as a
    pending ``DonationLedger`` row (disbursed manually via the
    ``reconcile_donations`` command). See ``apps.billing.donation_service``."""
    from apps.billing.donation_service import snapshot_donations_for_month

    return snapshot_donations_for_month()


def reconcile_openrouter_spend_task():
    """Trues up internal usage counters against OpenRouter provider truth.

    See ``apps.billing.management.commands.reconcile_openrouter_spend`` for
    the implementation. Wrapped here so QStash can dispatch it via the
    ``trigger_task`` URL pattern in ``apps.cron.views``.
    """
    from apps.billing.management.commands.reconcile_openrouter_spend import reconcile_all

    return reconcile_all()


def model_health_check_task():
    """Probe the free-model offer (pricing + reachability) and flip it on a
    transition. See ``apps.billing.model_health.model_health_check``."""
    from apps.billing.model_health import model_health_check

    return model_health_check()


def ensure_openrouter_subkey_task(tenant_id: str) -> dict:
    """Give a reactivated tenant its own capped OpenRouter sub-key.

    Queued by ``restore_tenant_runtime``: tenants that went inactive before
    per-tenant keys existed (or whose provisioning fell back) came back on the
    shared, uncapped key. Runs the existing single-tenant backfill (mint → KV →
    MI grant → persist → container rebind; re-hibernates a sleeping tenant).

    Never raises: a QStash retry after a partial failure could mint a second
    key, so failures are logged at ERROR (Sentry) and reported, not retried.
    """
    import io
    import logging

    from django.conf import settings

    from apps.billing.management.commands.backfill_openrouter_keys import _backfill_one, _eligible_tenants
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)
    if not getattr(settings, "OPENROUTER_PER_TENANT_KEYS_ENABLED", False):
        return {"status": "flag_off"}
    eligible = [t for t in _eligible_tenants(tenant_id) if t.status == Tenant.Status.ACTIVE]
    if not eligible:
        return {"status": "not_eligible"}
    out = io.StringIO()
    try:
        ok = _backfill_one(eligible[0], False, out)
    except Exception:
        logger.exception("ensure_openrouter_subkey: crashed for tenant %s", str(tenant_id)[:8])
        return {"status": "error"}
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    if not ok:
        logger.error("ensure_openrouter_subkey: failed for tenant %s: %s", str(tenant_id)[:8], lines[-1:] or "")
    else:
        logger.info("ensure_openrouter_subkey: keyed tenant %s", str(tenant_id)[:8])
    return {"status": "ok" if ok else "failed", "lines": lines}


def sweep_orphan_openrouter_keys_task(dry_run: bool = True, min_age_hours: int = 24) -> dict:
    """Reap orphan per-tenant OpenRouter sub-keys. DRY RUN unless told otherwise.

    Only ``tenant-<8 hex>`` keys are candidates (platform keys are never
    touched). Deleting is irreversible, so a bare trigger only reports;
    deleting needs ``{"kwargs": {"dry_run": false}}``.
    """
    import io
    import logging

    from apps.billing.management.commands.sweep_orphan_openrouter_keys import sweep_orphan_keys

    logger = logging.getLogger(__name__)
    out = io.StringIO()
    try:
        totals = sweep_orphan_keys(dry_run=dry_run is not False, min_age_hours=int(min_age_hours), stdout=out)
    except Exception:
        logger.exception("sweep_orphan_openrouter_keys_task failed")
        return {"status": "error"}
    return {"status": "ok", **totals}
