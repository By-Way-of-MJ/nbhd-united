"""DeepSeek V4.1 Flash is a selectable TRIAL model: allowed, priced, last in the
chain, and pinned to nothing — the 0731 Flash keeps every scheduled/worker slot."""

from django.test import SimpleTestCase

from apps.billing.constants import (
    DEEPSEEK_FLASH_41_DISPLAY,
    DEEPSEEK_FLASH_41_MODEL,
    DEEPSEEK_FLASH_MODEL,
    MODEL_RATES,
    canonical_model_id,
    display_name_for_model,
)
from apps.billing.model_health import MONITORED_MODELS
from apps.orchestrator.config_generator import HEARTBEAT_MODEL, TIER_MODEL_CONFIGS, TIER_TASK_DEFAULTS


class Flash41SelectableTest(SimpleTestCase):
    def test_allowed_and_last_so_it_only_joins_the_end_of_the_fallback_chain(self):
        allowlist = list(TIER_MODEL_CONFIGS["starter"])
        self.assertIn(DEEPSEEK_FLASH_41_MODEL, allowlist)
        self.assertEqual(allowlist[-1], DEEPSEEK_FLASH_41_MODEL)
        self.assertIn(DEEPSEEK_FLASH_MODEL, allowlist, "the 0731 Flash stays selectable during the trial")

    def test_priced_and_named_under_every_spelling_usage_can_report(self):
        bare = DEEPSEEK_FLASH_41_MODEL.removeprefix("openrouter/")
        self.assertEqual(MODEL_RATES[DEEPSEEK_FLASH_41_MODEL], MODEL_RATES[bare])
        self.assertEqual(display_name_for_model(DEEPSEEK_FLASH_41_MODEL), DEEPSEEK_FLASH_41_DISPLAY)
        # The "4.1" follows a letter, so canonicalisation must leave the dot alone
        # (rewriting it to "4-1" would miss the rate table and bill the default rate).
        self.assertEqual(canonical_model_id(DEEPSEEK_FLASH_41_MODEL), bare)
        self.assertIn(canonical_model_id(DEEPSEEK_FLASH_41_MODEL), MODEL_RATES)

    def test_pricing_is_refreshed_like_the_other_selectable_models(self):
        self.assertIn(DEEPSEEK_FLASH_41_MODEL, MONITORED_MODELS)

    def test_nothing_is_pinned_to_the_trial_model(self):
        self.assertEqual(HEARTBEAT_MODEL, DEEPSEEK_FLASH_MODEL)
        self.assertNotIn(DEEPSEEK_FLASH_41_MODEL, TIER_TASK_DEFAULTS["starter"].values())
