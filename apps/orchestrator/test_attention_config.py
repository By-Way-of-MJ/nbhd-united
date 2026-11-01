"""Byte parity with the pre-hold config/prompts, and opt-in tool policy."""

import hashlib
import json
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

    def rendered(self):
        config = json.dumps(generate_openclaw_config(self.tenant), ensure_ascii=False, sort_keys=True)
        agents = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
        return config, agents

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
