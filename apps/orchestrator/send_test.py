"""Opt-in delivery instructions for the six platform default check-ins."""

from apps.orchestrator.cron_drift import strip_date_line
from apps.router.chat_gates import _tenant_allowed

SEND_TEST_JOBS = frozenset(
    {
        "Morning Briefing",
        "Evening Check-in",
        "Personal Question",
        "Weekly Reflection",
        "Week Ahead Review",
        "Project Check-in",
    }
)
SEND_TEST_BLOCK = (
    "\n\n## Send test\n\n"
    "Finish all notes, saves and journal reports above first. Before sending, ask what "
    "the person can now use, decide or do. Send only a useful fact, decision aid or "
    "concrete next step. Never ask questions to fill memory gaps. Otherwise never call "
    "`nbhd_send_to_user` or announce nothing to report. A quiet run is a good run."
)
PERSONAL_QUESTION_SEND_TEST_BLOCK = (
    "\n\n## Send test\n\n"
    "Finish all notes and saves above first. Ask only if the answer would change "
    "how you help this person this week. Otherwise never call `nbhd_send_to_user` "
    "or announce nothing to report. A quiet run is a good run."
)
_CONCRETE_SEND_TEST_BLOCK = (
    "\n\n## Send test\n\n"
    "Finish every note, save and journal report above first. Override sends above: "
    "what can the person now use, decide or do? Supply a usable fact, decision aid "
    "or concrete next step; background questions do not qualify. If nothing, never "
    "call `nbhd_send_to_user` or announce nothing to report. A quiet run is a good run."
)
_BACKGROUND_SEND_TEST_BLOCK = (
    "\n\n## Send test\n\n"
    "Finish every note, save and journal report above first. Before any send above, ask: "
    "what can the person now use, decide or do because of this message? Generic check-ins "
    "and collecting background do not count. If nothing, do not call `nbhd_send_to_user` "
    "or announce nothing to report. A quiet run is a good run."
)
# Recognize the first version as managed too, so prompt revisions converge
# without mistaking an earlier platform block for a user edit.
_PREVIOUS_SEND_TEST_BLOCK = (
    "\n\n## Send test\n\n"
    "Complete every note, save and journal report above first. Before any send instructed above, ask: "
    "what can the person now use, decide or do because of this message? If nothing, do not call "
    "`nbhd_send_to_user` or announce that there was nothing to report. A quiet run is a good run."
)
_OLD_QUIET = "say so briefly or reply HEARTBEAT_OK (for heartbeats) rather than padding with stale content."
_NEW_QUIET = "finish every note and save below, then apply the Send test; never send a nothing-new message."
_OLD_STEP3 = "3. Before writing or sending anything, check each item against the daily note:\n"
_NEW_STEP3 = (
    "3. Before writing or sending anything, check each item against the daily note. "
    "Every send below is conditional on passing the final Send test:\n"
)


def apply_send_test(prompt: str, job_name: str, tenant) -> str:
    """Add the rule only while composing an identified platform seed job."""
    if job_name not in SEND_TEST_JOBS or not _tenant_allowed(tenant, "SEND_TEST_TENANT_IDS"):
        return prompt
    block = PERSONAL_QUESTION_SEND_TEST_BLOCK if job_name == "Personal Question" else SEND_TEST_BLOCK
    return prompt.replace(_OLD_STEP3, _NEW_STEP3).replace(_OLD_QUIET, _NEW_QUIET).rstrip() + block


def without_send_test(prompt: str) -> str:
    """Remove only our exact managed block, preserving surrounding user edits."""
    blocks = (
        SEND_TEST_BLOCK,
        PERSONAL_QUESTION_SEND_TEST_BLOCK,
        _CONCRETE_SEND_TEST_BLOCK,
        _BACKGROUND_SEND_TEST_BLOCK,
        _PREVIOUS_SEND_TEST_BLOCK,
    )
    if not any(block in prompt for block in blocks):
        return prompt
    for block in blocks:
        prompt = prompt.replace(block, "")
    return prompt.replace(_NEW_STEP3, _OLD_STEP3).replace(_NEW_QUIET, _OLD_QUIET)


def is_send_test_default(existing_message: str, desired_message: str) -> bool:
    """Compare full template bodies across either flag state, ignoring the clock."""
    from apps.orchestrator.config_generator import _MORNING_BRIEFING_PROACTIVE_DEFER_LINE, _PROACTIVE_SUGGESTIONS_BLOCK

    def body(message):
        # This existing platform-owned optional suffix can independently flip.
        # Remove exact managed text only; any user addition still differs.
        return (
            strip_date_line(without_send_test(message))
            .replace("\n" + _MORNING_BRIEFING_PROACTIVE_DEFER_LINE, "")
            .replace("\n\n" + _PROACTIVE_SUGGESTIONS_BLOCK, "")
            .strip()
        )

    return body(existing_message) == body(desired_message)
