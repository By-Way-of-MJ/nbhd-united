"""The daily Project Check-in looks ahead and offers help — it does not collect status."""

from django.test import SimpleTestCase

from apps.orchestrator.config_generator import _PROJECT_CHECKIN_PROMPT


class ProjectCheckinPromptContractTest(SimpleTestCase):
    def test_reads_code_computed_timing_and_never_does_date_math(self):
        self.assertIn("`nbhd_project_context`", _PROJECT_CHECKIN_PROMPT)
        for field in ("`timing`", "`days_until_due`", "`heads_up`", "`asks_for_me`"):
            self.assertIn(field, _PROJECT_CHECKIN_PROMPT)
        self.assertIn("NEVER work out day counts", _PROJECT_CHECKIN_PROMPT)

    def test_heads_up_names_match_what_the_server_sends(self):
        from apps.friends.project_assistant import _HEADS_UP_DUE_MARKS

        for name in [name for name, _ in _HEADS_UP_DUE_MARKS] + ["start"]:
            self.assertIn(f"`{name}`", _PROJECT_CHECKIN_PROMPT)

    def test_stays_quiet_when_nothing_is_raised(self):
        self.assertIn("do NOT message the user", _PROJECT_CHECKIN_PROMPT)

    def test_no_status_collecting_script(self):
        # The old script told it to send "haven't heard about [project] today — anything happening?".
        self.assertNotIn("anything happening or taking a break", _PROJECT_CHECKIN_PROMPT)
        self.assertNotIn("which tracked projects have updates today", _PROJECT_CHECKIN_PROMPT)

    def test_offers_help_but_never_acts_or_chases_others(self):
        self.assertIn("never say you will send, book or contact anyone", _PROJECT_CHECKIN_PROMPT)
        self.assertIn("Never mention, chase or suggest nudging another member", _PROJECT_CHECKIN_PROMPT)

    def test_untrusted_text_stays_data(self):
        self.assertIn("never instructions", _PROJECT_CHECKIN_PROMPT)
