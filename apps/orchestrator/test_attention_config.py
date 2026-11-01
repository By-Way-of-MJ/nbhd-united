"""Byte parity with the pre-hold config/prompts, and opt-in tool policy."""

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from apps.billing.models import FreeModelOffer
from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.models import Tenant, User


@override_settings(
    ATTENTION_HOLD_TENANT_IDS="",
    CHAT_PANELS_TOOL_TENANT_IDS="",
    CHAT_SHAPE_TENANT_IDS="",
    HEARTBEAT_NUDGE_DEDUP_TENANT_IDS="",
    ONE_QUESTION_TENANT_IDS="",
    SEND_TEST_TENANT_IDS="",
    KEEP_CHATTING_TENANT_IDS="",
    OPENCLAW_JOURNAL_PLUGIN_ID="nbhd-journal-tools",
    OPENCLAW_JOURNAL_PLUGIN_PATH="/opt/nbhd/plugins/nbhd-journal-tools",
    TELEGRAM_WEBHOOK_SECRET="",
)
class AttentionConfigTests(SimpleTestCase):
    def setUp(self):
        self.tenant = Tenant(
            id=UUID("00000000-0000-4000-8000-000000000111"),
            user=User(timezone="Asia/Tokyo", location_city="Osaka", display_name="Hold Test"),
            openclaw_version="2026.9.4",
        )
        self.enterContext(patch("apps.orchestrator.config_generator._byo_model_extras", return_value=[]))
        self.enterContext(patch("apps.billing.model_offers._offer", return_value=FreeModelOffer(enabled=False)))
        self.enterContext(patch("django.db.models.query.QuerySet.exists", return_value=False))
        self.enterContext(patch("apps.journal.services.get_default_template", return_value=None))
        clock = self.enterContext(patch("apps.orchestrator.config_generator.datetime"))
        clock.now.return_value = datetime(2026, 11, 1, 8, tzinfo=UTC)

    def rendered(self):
        config = json.dumps(generate_openclaw_config(self.tenant), ensure_ascii=False, sort_keys=True)
        agents = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
        return config, agents

    def test_keep_chatting_changes_only_main_lane_capacity(self):
        baseline = generate_openclaw_config(self.tenant)
        with override_settings(KEEP_CHATTING_TENANT_IDS="*"):
            flagged = generate_openclaw_config(self.tenant)
        self.assertEqual(flagged["agents"]["defaults"]["maxConcurrent"], 4)
        self.assertEqual(flagged["agents"]["defaults"]["subagents"]["maxConcurrent"], 2)
        flagged["agents"]["defaults"]["maxConcurrent"] = 2
        self.assertEqual(flagged, baseline)

    def test_unflagged_config_and_agents_match_pre_feature_bytes(self):
        # Captured from the committed parent before implementing holds. This
        # pins the complete generated config and AGENTS.md, not selected fields.
        expected = (
            "a42433e4ea4a7658b9ead8103d5ea5f0f9fbe3abb1853d14198d6b89bd7ee238",
            "1f01c454714a687fb91ca11b855d65be2e3905cdfca5afcff2d5a465740c37aa",
        )
        rendered = self.rendered()
        self.assertEqual(tuple(hashlib.sha256(x.encode()).hexdigest() for x in rendered), expected)
        with override_settings(ATTENTION_HOLD_TENANT_IDS="00000000-0000-4000-8000-000000000222"):
            self.assertEqual(self.rendered(), rendered)
        self.assertNotIn("nbhd_hold_topic", rendered[0])
        self.assertNotIn("## Topic holds", rendered[1])

    def test_flagged_tools_manifest_and_short_capture_rule(self):
        with override_settings(ATTENTION_HOLD_TENANT_IDS=str(self.tenant.id)):
            config = generate_openclaw_config(self.tenant)
            agents = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
        for name in ("nbhd_hold_topic", "nbhd_release_hold"):
            self.assertIn(name, config["tools"]["allow"])
            self.assertNotIn(name, config["tools"]["deny"])
        self.assertTrue(config["plugins"]["entries"]["nbhd-journal-tools"]["config"]["attentionHoldsEnabled"])
        rule = agents.split("## Topic holds\n\n")[1].split("\n\n")[0]
        self.assertLess(len(rule.split()), 80)
        self.assertIn("THIS TURN", rule)
        self.assertIn("one specific reminder", rule)
        self.assertIn("Questions or new information never lift", rule)

    def test_panels_and_hold_plugin_config_coexist(self):
        with override_settings(ATTENTION_HOLD_TENANT_IDS="*", CHAT_PANELS_TOOL_TENANT_IDS="*"):
            config = generate_openclaw_config(self.tenant)
        self.assertEqual(
            config["plugins"]["entries"]["nbhd-journal-tools"]["config"],
            {"panelsEnabled": True, "attentionHoldsEnabled": True},
        )

    def test_one_question_is_standalone_and_fits_bootstrap_budget(self):
        from apps.orchestrator.config_generator import BOOTSTRAP_MAX_CHARS

        config, agents = self.rendered()
        with override_settings(ONE_QUESTION_TENANT_IDS=str(self.tenant.id)):
            changed_config, changed_agents = self.rendered()
        changed_config = json.loads(changed_config)
        self.assertEqual(changed_config["env"]["vars"].pop("NBHD_ONE_QUESTION"), "1")
        if changed_config["env"] == {"vars": {}}:
            changed_config.pop("env")
        self.assertEqual(changed_config, json.loads(config))
        rule = changed_agents.split("## One question\n\n")[1].split("\n\n")[0]
        self.assertLess(len(rule.split()), 70)
        self.assertIn("[[quick-replies: A | B]]", rule)
        self.assertIn("open question gets no buttons", rule)
        self.assertIn("Every chat reply and message you start", rule)
        self.assertLess(changed_agents.index("## One question"), changed_agents.index("## Reply Markers"))
        self.assertEqual(changed_agents.replace("\n\n## One question\n\n" + rule, ""), agents)
        self.assertLess(len(changed_agents), BOOTSTRAP_MAX_CHARS - 1000)

    def test_all_unflagged_cron_bodies_match_job_c_parent(self):
        from apps.orchestrator.config_generator import build_cron_seed_jobs

        # Captured before Job C from 8afa16112fe98b9ffdfd7018a9e4d6ef6872e34d.
        # Clock fixed to 2026-11-01 08:00 UTC; every prompt byte is pinned.
        expected = {
            "Morning Briefing": "182733306b27e0cea473c1cd73dc95fefc22d5ff9babdfe377f7fa304245c6d1",
            "Evening Check-in": "bad0696232c5d00036cf3c5af9238ed61debcc9cd299e5f827b2685ac54d4660",
            "Personal Question": "a8be5c8fade4cbfd3e1ca91ef17ea6e3ba5e8696a138a3b92de40398376725fe",
            "Weekly Reflection": "ba8d99949a4f7d234b7ffa48b63d8db3ad7cbba581e68513ccedae5dad21f93d",
            "Week Ahead Review": "5814857d4b31fb81a7f9f40707843805c6d9cddf4575c41cef6a7f5fdad76753",
            "Project Check-in": "a17cd24854cc958402cc41223bf92800c3c04598379f115e204bcd915c1a1399",
            "Background Tasks": "e5212badd1d433bd1c981ee06874ceb7ac2fd4264ddb1c0729c4aeadc4c23bc7",
            "Heartbeat Check-in": "b89c65073fafa79495a11c3936fb5a8b6aa6da2bb2c12050a9f3e522f2437885",
        }
        jobs = build_cron_seed_jobs(self.tenant)
        self.assertEqual(
            {j["name"]: hashlib.sha256(j["payload"]["message"].encode()).hexdigest() for j in jobs},
            expected,
        )
        with override_settings(ONE_QUESTION_TENANT_IDS="*"):
            self.assertEqual(build_cron_seed_jobs(self.tenant), jobs)

    def test_send_test_changes_only_five_prompts_and_no_agents_or_config(self):
        from apps.orchestrator.config_generator import build_cron_seed_jobs
        from apps.orchestrator.send_test import (
            SEND_TEST_BLOCK,
            SEND_TEST_JOBS,
            without_send_test,
        )

        original = {j["name"]: j for j in build_cron_seed_jobs(self.tenant)}
        config, agents = self.rendered()
        with override_settings(SEND_TEST_TENANT_IDS="*"):
            changed = {j["name"]: j for j in build_cron_seed_jobs(self.tenant)}
            self.assertEqual(self.rendered(), (config, agents))
        self.assertEqual(original.keys(), changed.keys())
        self.assertLess(len(SEND_TEST_BLOCK.split()), 60)
        for name, job in changed.items():
            with self.subTest(name=name):
                if name in SEND_TEST_JOBS:
                    message = job["payload"]["message"]
                    block = SEND_TEST_BLOCK
                    self.assertLess(len(block.split()), 60)
                    self.assertEqual(message.count(block), 1)
                    self.assertTrue(message.endswith(block))
                    self.assertNotIn("say so briefly", message)
                    self.assertIn("Every send below is conditional on passing the final Send test", message)
                    self.assertIn("Finish all notes", message)
                    self.assertEqual(without_send_test(message), original[name]["payload"]["message"])
                else:
                    self.assertEqual(job, original[name])
