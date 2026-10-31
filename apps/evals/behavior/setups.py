"""Per-scenario setup hooks for the behavior suite (Wave D extension).

A scenario YAML may name ``setup: <name>``; the suite calls the registered
function with the resolved behavior tenant RIGHT BEFORE it drives that
scenario's turns. A setup hook exists because some scenarios are only meaningful
from a known starting state (e.g. "the user is on Tokyo time and has no travel
situation") and a previous run's residue would otherwise leak in and make the
hard assertions read green (or red) for the wrong reason.

Contract:
  * Hooks receive the ``Tenant`` and return ``None``. They may raise — the suite
    catches the exception and records the scenario as FAILED with reason
    ``setup:<error>`` (never a silent skip, INVARIANT #3).
  * Hooks write only control-plane state (profile + situation + cron rows) and
    issue ONE explicit synchronous USER.md push, so the container sees the reset
    state before the scenario's first turn.
  * Name validation lives in ``schema.VALID_SETUPS``; ``SETUPS`` here must have
    exactly the same keys (a test pins the two sets equal).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TOKYO = "Asia/Tokyo"


def reset_timezone_tokyo(tenant) -> None:
    """Put the behavior tenant back on Tokyo time with no travel situation.

    The timezone-change scenarios assert on an ABSOLUTE move (Tokyo ->
    America/Chicago), so the profile must start on Tokyo regardless of what a
    previous run (or a real user of the synthetic tenant) left behind. Applies:

      * ``user.timezone = "Asia/Tokyo"`` — saves ONLY that field;
      * every recurring (``data.schedule.kind == "cron"``) CronJob row onto
        Tokyo, via the SAME helper the profile endpoint uses
        (``apps.tenants.views._sync_cron_row_timezones`` — imported, never
        reimplemented, so a fix there reaches this hook too);
      * clears the travel situation so an old run's place/device observation
        cannot pre-answer the "move to local time?" question: the current-place
        fields, ``tz_offer_dismissed_for`` and the device-timezone fields (see
        ``apps.tenants.models.UserSituation``);
      * one explicit synchronous USER.md push, so the container reads Tokyo
        before the first turn. Writes are wrapped in ``suppress_refresh`` (same
        pattern as ``DeviceTimezoneView``) so the registry-wired
        ``User``/``UserSituation`` saves do not each push their own
        stale-by-a-beat envelope.
    """
    from apps.orchestrator.envelope_registry import suppress_refresh
    from apps.orchestrator.workspace_envelope import TRIGGER_MANUAL, push_user_md
    from apps.tenants.models import UserSituation
    from apps.tenants.views import _sync_cron_row_timezones

    user = tenant.user
    with suppress_refresh():
        user.timezone = TOKYO
        user.save(update_fields=["timezone"])

        moved = _sync_cron_row_timezones(tenant, TOKYO)

        # Queryset.update() writes the reset row without firing the registry's
        # post_save push — the explicit push below is the only one for this reset.
        UserSituation.objects.filter(tenant=tenant).update(
            current_place_label="",
            current_place_since=None,
            current_place_last_observed_at=None,
            current_place_source="",
            device_tz="",
            device_tz_since=None,
            device_tz_last_observed_at=None,
            device_tz_source_device="",
            tz_offer_dismissed_for="",
        )

    # TRIGGER_MANUAL: an operator/eval-driven reset, and it is a
    # forced-freshness trigger (apps.orchestrator.workspace_envelope), so the
    # Tokyo profile cannot be dropped by the leading-edge debounce. The push is
    # synchronous: the reset envelope is in the container before the scenario's
    # first turn, not racing a daemon thread.
    if not push_user_md(tenant, force=True, trigger=TRIGGER_MANUAL):
        logger.warning("behavior setup reset_timezone_tokyo push_not_confirmed tenant=%s", tenant.id)
    logger.info("behavior setup reset_timezone_tokyo tenant=%s cron_rows_moved=%d", tenant.id, moved)


SETUPS = {
    "reset_timezone_tokyo": reset_timezone_tokyo,
}
