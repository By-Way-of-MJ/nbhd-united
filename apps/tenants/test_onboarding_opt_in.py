"""App onboarding: "what I want help with" on the profile + opt-in scheduled messages.

A. ``onboarding_interests`` is readable/writable on /api/v1/tenants/profile/,
   stored in ``user.preferences`` without touching other keys, rendered in the
   USER.md Profile block, and marks the messaging intro done.
B. ``Tenant.scheduled_messages_opt_in`` (set for new app/web sign-ups) leaves the
   generic user-facing system crons out of the seed; existing tenants keep them.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.cron.models import CronJob, CronJobSource
from apps.orchestrator.config_generator import OPT_IN_SKIPPED_SEED_JOBS, build_cron_seed_jobs
from apps.orchestrator.first_session_welcome import compose_first_session_welcome
from apps.orchestrator.services import refresh_system_cron_rows_from_seed
from apps.router.onboarding import needs_reintroduction
from apps.tenants.envelope import render_profile
from apps.tenants.models import Tenant, User
from apps.tenants.services import create_tenant, prepare_tenant_provisioning

PROFILE_URL = "/api/v1/tenants/profile/"


class OnboardingInterestsProfileTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Interest Tester", telegram_chat_id=880011)
        self.user = self.tenant.user
        self.user.preferences = {"agent_persona": "coach", "_onboarding_country": "JP"}
        self.user.save(update_fields=["preferences"])
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_get_returns_empty_string_when_unset(self):
        response = self.client.get(PROFILE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["onboarding_interests"], "")

    def test_patch_stores_cleaned_text_and_preserves_other_preferences(self):
        response = self.client.patch(
            PROFILE_URL,
            {"onboarding_interests": "  Health & fitness,\n Money​  — sleep better  "},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["onboarding_interests"], "Health & fitness, Money — sleep better")

        self.user.refresh_from_db()
        self.assertEqual(self.user.preferences["onboarding_interests"], "Health & fitness, Money — sleep better")
        self.assertEqual(self.user.preferences["agent_persona"], "coach")
        self.assertEqual(self.user.preferences["_onboarding_country"], "JP")

        self.assertEqual(
            self.client.get(PROFILE_URL).data["onboarding_interests"], "Health & fitness, Money — sleep better"
        )

    def test_patch_empty_clears_only_that_key(self):
        self.client.patch(PROFILE_URL, {"onboarding_interests": "Money"}, format="json")
        response = self.client.patch(PROFILE_URL, {"onboarding_interests": ""}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["onboarding_interests"], "")
        self.user.refresh_from_db()
        self.assertNotIn("onboarding_interests", self.user.preferences)
        self.assertEqual(self.user.preferences["agent_persona"], "coach")

    def test_patch_rejects_text_over_limit(self):
        response = self.client.patch(PROFILE_URL, {"onboarding_interests": "x" * 401}, format="json")
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertNotIn("onboarding_interests", self.user.preferences)

    def test_patch_other_fields_leaves_interests_alone(self):
        self.client.patch(PROFILE_URL, {"onboarding_interests": "Money"}, format="json")
        self.client.patch(PROFILE_URL, {"display_name": "Mika"}, format="json")
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferences["onboarding_interests"], "Money")

    def test_get_flattens_legacy_list_value(self):
        self.user.preferences = {**self.user.preferences, "onboarding_interests": ["Money", " Sleep "]}
        self.user.save(update_fields=["preferences"])
        self.assertEqual(self.client.get(PROFILE_URL).data["onboarding_interests"], "Money, Sleep")

    def test_non_empty_interests_mark_messaging_intro_done(self):
        self.assertFalse(self.tenant.onboarding_complete)
        self.client.patch(PROFILE_URL, {"onboarding_interests": "Money"}, format="json")
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.onboarding_complete)
        self.assertEqual(self.tenant.onboarding_step, 5)
        self.assertFalse(needs_reintroduction(self.tenant))

    def test_empty_interests_do_not_mark_intro_done(self):
        self.client.patch(PROFILE_URL, {"onboarding_interests": ""}, format="json")
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.onboarding_complete)
        self.assertEqual(self.tenant.onboarding_step, 0)

    def test_render_profile_line_present_and_absent(self):
        self.assertNotIn("Interests and priorities", render_profile(self.tenant))
        self.client.patch(PROFILE_URL, {"onboarding_interests": "Money, sleep"}, format="json")
        self.tenant.user.refresh_from_db()
        self.assertIn("- Interests and priorities: Money, sleep", render_profile(self.tenant))


class ScheduledMessagesOptInSeedTests(TestCase):
    def _tenant(self, username, *, opt_in):
        user = User.objects.create_user(username=username, password="x")
        return Tenant.objects.create(user=user, status="active", model_tier="starter", scheduled_messages_opt_in=opt_in)

    def test_new_app_signup_is_opted_in(self):
        user = User.objects.create_user(username="fresh-signup", password="x")
        tenant, created = prepare_tenant_provisioning(user)
        self.assertTrue(created)
        self.assertTrue(tenant.scheduled_messages_opt_in)

    def test_existing_and_default_tenants_are_not_opted_in(self):
        self.assertFalse(create_tenant(display_name="Legacy", telegram_chat_id=880022).scheduled_messages_opt_in)
        self.assertFalse(self._tenant("plain", opt_in=False).scheduled_messages_opt_in)

    def test_opted_in_seed_skips_generic_jobs_keeps_background_and_heartbeat(self):
        names = {job["name"] for job in build_cron_seed_jobs(self._tenant("optin", opt_in=True))}
        self.assertFalse(names & OPT_IN_SKIPPED_SEED_JOBS)
        self.assertIn("Background Tasks", names)
        self.assertIn("Heartbeat Check-in", names)

    @override_settings(GRAVITY_ENABLED=True)
    def test_opted_in_seed_keeps_feature_gated_gravity(self):
        tenant = self._tenant("optin-gravity", opt_in=True)
        tenant.finance_enabled = True
        tenant.save(update_fields=["finance_enabled"])
        self.assertIn("Gravity Weekly Check-in", {job["name"] for job in build_cron_seed_jobs(tenant)})

    def test_unflagged_seed_unchanged(self):
        names = {job["name"] for job in build_cron_seed_jobs(self._tenant("legacy", opt_in=False))}
        self.assertTrue(names >= OPT_IN_SKIPPED_SEED_JOBS)
        self.assertIn("Background Tasks", names)

    def test_refresh_creates_nothing_generic_for_opted_in_and_keeps_user_rows(self):
        tenant = self._tenant("optin-refresh", opt_in=True)
        CronJob.objects.create(
            tenant=tenant,
            name="Morning Briefing",
            data={"payload": {"message": "my own briefing"}},
            source=CronJobSource.USER,
            managed=True,
        )
        refresh_system_cron_rows_from_seed(tenant)
        system_names = set(
            CronJob.objects.filter(tenant=tenant, source=CronJobSource.SYSTEM).values_list("name", flat=True)
        )
        self.assertFalse(system_names & OPT_IN_SKIPPED_SEED_JOBS)
        self.assertIn("Background Tasks", system_names)
        row = CronJob.objects.get(tenant=tenant, name="Morning Briefing")
        self.assertEqual(row.source, CronJobSource.USER)
        self.assertEqual(row.data["payload"]["message"], "my own briefing")

    def test_refresh_keeps_existing_tenant_jobs(self):
        tenant = self._tenant("legacy-refresh", opt_in=False)
        refresh_system_cron_rows_from_seed(tenant)
        before = set(CronJob.objects.filter(tenant=tenant).values_list("name", flat=True))
        self.assertTrue(before >= OPT_IN_SKIPPED_SEED_JOBS)
        refresh_system_cron_rows_from_seed(tenant)
        self.assertEqual(set(CronJob.objects.filter(tenant=tenant).values_list("name", flat=True)), before)


class FirstSessionWelcomeNameTests(TestCase):
    def test_placeholder_friend_is_not_used_as_a_name(self):
        self.assertTrue(compose_first_session_welcome("Friend").startswith("Welcome to the neighborhood."))
        self.assertTrue(compose_first_session_welcome("Mika").startswith("Hey Mika — welcome"))
