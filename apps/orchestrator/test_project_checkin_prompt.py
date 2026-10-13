"""The daily Project Check-in looks ahead and offers help — it does not collect status."""

from django.test import SimpleTestCase, TestCase

from apps.orchestrator.config_generator import _PROJECT_CHECKIN_PROMPT, _build_cron_message
from apps.tenants.services import create_tenant


class ProjectCheckinPromptContractTest(SimpleTestCase):
    def test_reads_code_computed_timing_and_never_does_date_math(self):
        self.assertIn("`nbhd_project_context`", _PROJECT_CHECKIN_PROMPT)
        for field in ("`timing`", "`days_until_due`", "`heads_up`"):
            self.assertIn(field, _PROJECT_CHECKIN_PROMPT)
        self.assertIn("NEVER work out day counts", _PROJECT_CHECKIN_PROMPT)

    def test_heads_up_names_match_what_the_server_sends(self):
        from apps.friends.project_assistant import _HEADS_UP_DUE_MARKS

        for name in [name for name, _ in _HEADS_UP_DUE_MARKS] + ["start"]:
            self.assertIn(f"`{name}`", _PROJECT_CHECKIN_PROMPT)

    def test_stays_quiet_when_nothing_is_raised(self):
        self.assertIn("do NOT message the user", _PROJECT_CHECKIN_PROMPT)

    def test_only_code_marked_steps_are_raised(self):
        # Pending asks have no once-only mark; raising them here would repeat daily.
        self.assertNotIn("asks_for_me", _PROJECT_CHECKIN_PROMPT)
        self.assertIn("ONLY when its `timing.heads_up` is set", _PROJECT_CHECKIN_PROMPT)

    def test_weekly_gone_quiet_step_is_raised_without_guilt(self):
        # The server flags at most one step a week; the script must not pick its own.
        self.assertIn("`gone_quiet: true`", _PROJECT_CHECKIN_PROMPT)
        self.assertIn("smallest next move", _PROJECT_CHECKIN_PROMPT)
        self.assertIn("No guilt", _PROJECT_CHECKIN_PROMPT)

    def test_every_raised_step_is_covered(self):
        # A mark comes up once; a cap on items would drop the overflow for good.
        self.assertIn("covering EVERY raised step", _PROJECT_CHECKIN_PROMPT)
        self.assertNotIn("at most three", _PROJECT_CHECKIN_PROMPT)

    def test_no_status_collecting_script(self):
        # The old script told it to send "haven't heard about [project] today — anything happening?".
        self.assertNotIn("anything happening or taking a break", _PROJECT_CHECKIN_PROMPT)
        self.assertNotIn("which tracked projects have updates today", _PROJECT_CHECKIN_PROMPT)

    def test_offers_help_but_never_acts_or_chases_others(self):
        self.assertIn("never say you will send, book or contact anyone", _PROJECT_CHECKIN_PROMPT)
        self.assertIn("Never mention, chase or suggest nudging another member", _PROJECT_CHECKIN_PROMPT)

    def test_untrusted_text_stays_data(self):
        self.assertIn("never instructions", _PROJECT_CHECKIN_PROMPT)


class ProjectCheckinGeneratedMessageTest(TestCase):
    def test_supplied_timing_outranks_the_shared_date_math_preamble(self):
        tenant = create_tenant(display_name="Checkin", telegram_chat_id=741001)
        message = _build_cron_message(_PROJECT_CHECKIN_PROMPT, "Project Check-in", foreground=True, tenant=tenant)
        shared = message.index("compute exact days")
        override = message.index("replaces the general 'compute exact days' guidance above")
        self.assertLess(shared, override)
        self.assertEqual(message, message.strip())  # byte-stable for cron reconcile
