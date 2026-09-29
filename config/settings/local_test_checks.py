"""Offline regression tests on the dedicated Compose PostgreSQL only."""

from .local_test import *  # noqa: F401,F403

LOCAL_TEST_ROOT = ""
# Offline regression tests never call the local model (production behaviour, OpenRouter path).
LOCAL_TEST_LLM_URL = ""
ROOT_URLCONF = "config.urls"
OPENCLAW_USAGE_PLUGIN_ID = "nbhd-usage-reporter"
