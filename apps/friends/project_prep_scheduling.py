"""Best-effort, bounded prep dispatch after a person's Start or choice commits."""

import logging
import threading
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.billing.services import check_budget
from apps.cron.models import CronCreationPath, CronJob, CronJobSource
from apps.cron.pending_at_views import _at_fires_at_ms
from apps.cron.services import _push_at_cron_immediately
from apps.cron.signals import suppress_cronjob_reconcile
from apps.cron.system_dispatch import STEP_PREP_NOW_PREFIX
from apps.orchestrator.config_generator import step_prep_job
from apps.orchestrator.hibernation import wake_for_cron_task
from apps.router.chat_gates import _tenant_allowed
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _schedule(tenant_id):
    # This durable row reserves the slot before any slow wake/share call. Keep
    # disabled and spent rows for the rolling cap, even if cron.list misses them.
    with transaction.atomic(), suppress_cronjob_reconcile():
        tenant = Tenant.objects.select_for_update().get(pk=tenant_id)
        if not _tenant_allowed(tenant, "STEP_PREP_TENANT_IDS") or check_budget(tenant):
            return
        now = timezone.now()
        rows = CronJob.objects.filter(
            tenant=tenant,
            source=CronJobSource.SYSTEM,
            creation_path=CronCreationPath.INTERNAL,
            name__startswith=STEP_PREP_NOW_PREFIX,
        )
        if any((_at_fires_at_ms(row.data) or 0) > now.timestamp() * 1000 for row in rows.filter(enabled=True)):
            return
        if rows.filter(created_at__gte=now - timedelta(hours=24)).count() >= 6:
            return
        hibernated = bool(tenant.hibernated_at)
        job = step_prep_job()
        job.update(
            name=f"{STEP_PREP_NOW_PREFIX}{uuid4()}",
            schedule={"kind": "at", "at": (now + timedelta(seconds=270 if hibernated else 90)).isoformat()},
            deleteAfterRun=True,
        )
        cron = CronJob.objects.create(
            tenant=tenant,
            name=job["name"],
            data=job,
            enabled=True,
            managed=False,
            source=CronJobSource.SYSTEM,
            creation_path=CronCreationPath.INTERNAL,
        )

    if hibernated:
        try:
            result = wake_for_cron_task(str(tenant.id))
            if result.get("status") not in {"woken_for_cron", "already_awake"}:
                raise RuntimeError("prep cron wake did not succeed")
        except Exception:
            CronJob.objects.filter(pk=cron.pk).update(enabled=False, updated_at=timezone.now())
            logger.exception("step prep wake failed tenant=%s cron=%s", tenant.id, cron.pk)
            return
        tenant.refresh_from_db()
    _push_at_cron_immediately(tenant, cron)


def run_soon(tenant):
    """After commit, off the request thread; the daily run remains the backstop."""
    tenant_id = tenant.pk

    def work():
        try:
            _schedule(tenant_id)
        except Exception:
            logger.exception("step prep dispatch failed tenant=%s", tenant_id)

    def threaded():
        try:
            work()
        finally:
            connection.close()

    def dispatch():
        try:
            if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
                work()
            else:
                threading.Thread(target=threaded, daemon=True).start()
        except Exception:
            logger.exception("step prep thread start failed tenant=%s", tenant_id)

    transaction.on_commit(dispatch)
