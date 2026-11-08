"""`task_hygiene` pattern: weekly proactive task-list cleanup, system-defined.

Like ``daily_briefing``, this is a system pattern — the agent's
``nbhd_cron_create_*`` tools do not accept it. Seeded by
``apps/cron/services.py::seed_task_hygiene_cron`` behind the
``TASK_HYGIENE_TENANT_IDS`` canary gate.

Why this pattern exists separately from ``daily_briefing``: the briefing's
``get_tools_allow`` is a deliberate *no-mutations* guard (the structural fix
for the 22:07 cron-creates-duplicate-task cascade) and must not be weakened.
Proactive cleanup genuinely needs to close and defer things, so it gets its
own pattern with its own, narrower mutation budget rather than loosening the
briefing's.

The mutation budget is the whole design:

  ALLOWED   nbhd_task_complete / nbhd_task_skip / nbhd_task_defer — lifecycle
            transitions on tasks that ALREADY exist. Every one is reversible
            by the user in conversation, and none can invent an item.

  ABSENT    nbhd_task_create / nbhd_task_update / nbhd_task_delete,
            every goal mutation, every finance/fuel/document/cron write.
            The turn cannot fabricate work, rewrite titles, or destroy
            anything.

Deletion is PROPOSED, never performed. ``nbhd_task_delete`` (PR 1) is
structurally two-phase: a first call returns ``confirmation_required`` and only
a follow-up carrying ``confirm=true`` deletes. That handshake is designed to be
completed by a human saying yes in conversation — a scheduled turn has no user
present to ask, so it could only ever satisfy the handshake by confirming to
itself. Leaving the tool out of ``toolsAllow`` makes that impossible rather
than merely discouraged; the summary lists candidates and the user confirms
them in chat on their own time.

Outbound contract mirrors the briefing's: marker check with
``revise_then_allow`` (max 1 revision), so a missing marker costs one retry and
then ships anyway — a hygiene summary is worth more late than never.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from apps.billing.constants import DEEPSEEK_FLASH_MODEL

from . import register_handler
from .base import PatternHandler, PatternPayload

# Same allowlist-safe model every other typed pattern pins — see the long
# rationale in pure_reminder.py / daily_briefing.py. A platform-initiated turn
# whose model sits outside the tenant's agents.defaults.models allowlist is
# rejected by OpenClaw's cron preflight before any tool runs, so the whole
# hygiene pass would silently never happen.
_CRON_MODEL = DEEPSEEK_FLASH_MODEL

# Longer than the briefing's 90s: this turn reads the task list, may fetch
# individual tasks for detail, performs several lifecycle calls, and only then
# composes a summary.
_TURN_TIMEOUT_SECONDS = 300

_MARKER = "[block: task_hygiene]"

# Per-run read budgets for the four LOOK calls. Stale open tasks are the main
# triage pool; stale in-progress tasks are a smaller, higher-signal set (work
# the user started and forgot); the recent-open and past-due lists exist only
# so a dated one-off event (a flight, a booking) gets checked even when it is
# NOT stale, and so an overdue item inside the recent window is never missed.
_STALE_OPEN_LIMIT = 40
_STALE_IN_PROGRESS_LIMIT = 20
_RECENT_OPEN_LIMIT = 50
_PAST_DUE_LIMIT = 50

# Floor for auto-defer: an undated task untouched for this many days is
# auto-deferred rather than left in the active list. Deferral is reversible, so
# this is the safe default. A tenant's larger stale_after_days raises it.
_AUTO_DEFER_DAYS = 30

# Fire-time hard cap on lifecycle mutations per run (mirrors the outbound
# contract's ``limits.mutations``).
_MUTATION_CAP = 40

# Read-only context tools.
_HYGIENE_QUERY_TOOLS: tuple[str, ...] = (
    "nbhd_task_list",
    "nbhd_task_get",
    "nbhd_goal_list",
)

# The three permitted lifecycle transitions. Deliberately NOT create/update/
# delete — see the module docstring. This tuple is pinned exactly by
# ``TaskHygieneTests.test_tools_allow_is_pinned_exactly``: adding a tool here
# without a matching review is the failure mode that test exists to catch.
_HYGIENE_LIFECYCLE_TOOLS: tuple[str, ...] = (
    "nbhd_task_complete",
    "nbhd_task_skip",
    "nbhd_task_defer",
)


class TaskHygienePayload(PatternPayload):
    """Schema for a task-hygiene cron's typed_payload.

    Carries thresholds only — never task content. Facts come from fresh tool
    calls at fire time, and free text is barred by ``extra="forbid"`` on the
    base class (cron prompts bypass inbound PII redaction, so a payload that
    accepted prose would be a leak surface — the workout_congrats lesson).
    """

    stale_after_days: int = Field(
        default=14,
        ge=1,
        le=365,
        description="An open task untouched for this many days counts as stale.",
    )


class TaskHygieneHandler(PatternHandler):
    pattern = "task_hygiene"
    payload_schema = TaskHygienePayload

    def build_oc_data(
        self,
        payload: TaskHygienePayload,
        *,
        tenant: Any,
        name: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        defer_days = max(payload.stale_after_days, _AUTO_DEFER_DAYS)
        if defer_days > payload.stale_after_days:
            left_alone_bullet = (
                f"  - A stale task untouched for at least {payload.stale_after_days} "
                f"days but fewer than {defer_days}, or any task that has a date: "
                "leave it alone (dated ones were already handled in STEP 2).\n"
            )
        else:
            left_alone_bullet = (
                "  - Any task that has a date: leave it alone (dated ones were already handled in STEP 2).\n"
            )
        message = (
            "You are running the weekly task-hygiene pass. Nobody asked for "
            "this and nobody is waiting on it — the user is not present in "
            "this turn. Work quietly and only interrupt them if you actually "
            "changed something or need a decision.\n"
            "\n"
            "STEP 1 — LOOK. Call `nbhd_task_list` exactly four times, in this order:\n"
            f"  1. `nbhd_task_list({{status: 'open', stale_days: {payload.stale_after_days}, "
            f"order: 'updated_at', limit: {_STALE_OPEN_LIMIT}, fields: 'compact'}})` "
            "→ stale open tasks, oldest first.\n"
            f"  2. `nbhd_task_list({{status: 'in_progress', stale_days: {payload.stale_after_days}, "
            f"order: 'updated_at', limit: {_STALE_IN_PROGRESS_LIMIT}, fields: 'compact'}})` "
            "→ stale in-progress tasks.\n"
            f"  3. `nbhd_task_list({{status: 'open', limit: {_RECENT_OPEN_LIMIT}, "
            "fields: 'compact'})` → the 50 most recently updated open tasks. Use "
            "these ONLY for the past-date check in STEP 2, not for stale triage.\n"
            f"  4. `nbhd_task_list({{status: 'open', due_before: '<yesterday as YYYY-MM-DD>', "
            f"limit: {_PAST_DUE_LIMIT}, fields: 'compact'}})` → open tasks whose due "
            "date has passed. Use these ONLY for the past-date check in STEP 2.\n"
            "Then call `nbhd_goal_list` exactly once to see which goals are still live. "
            "Call `nbhd_task_get` only when a selected row is truly ambiguous, "
            "at most 5 calls per run. Every judgement you make below must trace to one of "
            "those tool results — never to memory or assumption. If a call errors on the "
            "new keys, retry it once with only `{status: ...}`; from such an unfiltered "
            "list review at most the 25 rows with the oldest `updated_at`.\n"
            "\n"
            "STEP 2 — PAST DATES FIRST (any age, from all four lists). Today's "
            "date is the current date shown in this turn (the runtime adds the "
            "live date/time). Only a DATED ONE-OFF EVENT whose moment is over counts "
            "as PAST-DATED and may be auto-closed here: a flight, trip, appointment, "
            "booking, reservation, ticket, or event whose written date (in "
            "`due_date` or the title) is before today. "
            "The written date must be the date of the event itself. A task to "
            "arrange something (buy tickets, make a booking, reserve a table) "
            "whose deadline passed is a missed deadline, not a past event — "
            "treat it as overdue, never close it. For a past event: if the record "
            "shows it happened → `nbhd_task_complete`; otherwise → "
            "`nbhd_task_skip` (the moment passed). Reason line in the summary: "
            "`date passed (<the date>)`. Any OTHER task with a past `due_date` or a "
            "missed deadline (a bill, a form, a 'deadline' item) is NOT closed — it "
            "may still need doing late. List it in the summary as `overdue since "
            "<date> — still need it?`. Only use a date that is actually written; if "
            "no clear date is written, the task is NOT past-dated.\n"
            "\n"
            "STEP 3 — CLOSE what is already done, then TRIAGE stale tasks (from "
            "lists 1 and 2). If the record shows the work happened (a completed "
            "session, a paid bill, a goal the task fed into that is already "
            "achieved), call `nbhd_task_complete`. If you are not sure it "
            "happened, do NOT complete it — propose it in the summary instead. "
            "Then go through lists 1 and 2 using each row's `days_untouched` "
            "field (the server computes it).\n"
            "  - DEFAULT ACTION: a task with `days_untouched` of at least "
            f"{defer_days} days, no `due_date`, and no date written in its "
            "title → `nbhd_task_defer` with no new date. Do this without "
            "asking: deferring is reversible (the task leaves the active list "
            "but is kept and can be reopened any time), and the user asked for "
            "old tasks to be cleared this way.\n"
            "  - `nbhd_task_skip` only when the moment for it has clearly "
            "passed and doing it no longer makes sense.\n"
            f"{left_alone_bullet}"
            "  - Be conservative with skip: a wrongly-skipped task is worse "
            "than a stale one. Defer is the safe default.\n"
            "\n"
            "STEP 4 — PROPOSE deletions, never perform them. Some items are "
            "stale junk: test entries, exact duplicates, fragments "
            "that were never real tasks. You CANNOT delete them here. "
            "`nbhd_task_delete` is not available in this turn, and by design: "
            "deleting a task is permanent, cascades to its subtasks, and "
            "requires the user's explicit spoken confirmation in conversation "
            "before it goes through. This is a scheduled turn with no user in "
            "it, so that confirmation cannot be obtained — you would only be "
            "confirming to yourself. List the candidates in your summary "
            "instead, with a short reason each, and let the user confirm them "
            "in chat whenever they get to it.\n"
            "\n"
            "STEP 5 — REPORT, at most once. If you changed nothing AND have no proposals "
            "(no overdue items, no stale items to ask about, no deletion "
            "candidates), send NOTHING AT ALL and end the turn — a "
            "message saying 'everything looks fine' is noise the user did not "
            "ask for. Otherwise call `nbhd_send_to_user` EXACTLY ONCE with a "
            "short, scannable summary. List every completion and skip with its "
            "one-line reason, past-dated closures first; then group ALL "
            "deferrals into ONE line: `Put off N old tasks (untouched "
            f'{defer_days}+ days, nothing deleted — say "bring back '
            '<task>" to restore): <title>, <title>, ...`. Then at '
            "most 10 proposals (overdue items, stale items you left alone but "
            "want to ask about, deletion candidates); if there are more, say how "
            f"many more. You can make at most {_MUTATION_CAP} changes per run; "
            "if you hit that, list the rest as proposals. Do not send a second "
            "message. Do not narrate your tool calls.\n"
            "\n"
            f"The first line of that message must include the literal marker "
            f"`{_MARKER}` so downstream tooling can identify the render type."
        )

        return {
            "name": name,
            "schedule": schedule,
            "sessionTarget": "isolated",
            "wakeMode": "next-heartbeat",
            "payload": {
                "kind": "agentTurn",
                "message": message,
                "model": _CRON_MODEL,
                "thinking": "low",
                "lightContext": False,
                "toolsAllow": self.get_tools_allow(payload),
                "timeoutSeconds": _TURN_TIMEOUT_SECONDS,
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        }

    def get_tools_allow(self, payload: TaskHygienePayload) -> list[str]:
        # The structural guard. Prose in the prompt can drift under a model
        # swap; this list cannot — the runtime simply has no create, update or
        # delete tool to call. Pinned exactly by the drift test.
        return [
            *_HYGIENE_QUERY_TOOLS,
            *_HYGIENE_LIFECYCLE_TOOLS,
            "nbhd_send_to_user",
        ]

    def get_outbound_contract(
        self,
        payload: TaskHygienePayload,
        *,
        name: str,
    ) -> dict[str, Any]:
        return {
            "check": {"kind": "marker", "marker": _MARKER},
            "on_fail": {"action": "revise_then_allow", "max_revisions": 1},
            # Fire-time hard caps, enforced in-container by the
            # nbhd-cron-enforcement plugin (see its MUTATION_TOOL_IDS — that set
            # MUST stay in lockstep with _HYGIENE_LIFECYCLE_TOOLS above; the
            # drift test pins this side).
            #
            # sends=1 is what actually makes "ONE summary" true. Everything else
            # about one-message-only is prose the model can talk itself out of;
            # this blocks dispatch number two outright. It also closes the
            # unmarked-second-message path: a message that never dispatches
            # cannot arrive without a marker.
            #
            # mutations=40 bounds the worst case of an unattended turn that
            # misreads the task list: 40 is up to 40 stale open+in-progress
            # rows reviewed per run. Every change is a reversible status flip
            # shown in one summary, so a wrong one is recoverable and visible;
            # a hundred is a data-loss incident. Past the cap the model is told
            # to list the rest as proposals, which is the same escape hatch
            # deletion already uses.
            "limits": {"sends": 1, "mutations": _MUTATION_CAP},
        }

    def validate_outbound_message(
        self,
        content: str,
        payload: TaskHygienePayload,
    ) -> tuple[bool, str | None]:
        if _MARKER in (content or ""):
            return True, None
        return False, (f"Task-hygiene outbound must include the marker {_MARKER!r}. Got: {(content or '')[:200]!r}")


register_handler(TaskHygieneHandler())
