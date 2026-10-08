"""Loaded only by deploy/local-test/run.py with a clean generated environment."""

import os

from .development import *  # noqa: F403,F401

if os.environ.get("AZURE_MOCK") != "true":
    raise RuntimeError("The local test stack cannot use Azure")
LOCAL_TEST_ROOT = os.environ["LOCAL_TEST_ROOT"]
# .env.local-test is generated from .env.example, whose blank OPENCLAW_JOURNAL_PLUGIN_PATH overrides the base
# default and silently drops the assistant's journal/task/calendar tools ("plugin not found"). Use the image
# path; configure_gateway maps /opt/nbhd/plugins/* to this checkout's runtime plugins.
OPENCLAW_JOURNAL_PLUGIN_PATH = OPENCLAW_JOURNAL_PLUGIN_PATH or "/opt/nbhd/plugins/nbhd-journal-tools"  # noqa: F405
# Local model turns outlast the 120 s fleet default; stay below yuki_local's 900 s poll.
LOCAL_TEST_CHAT_TIMEOUT = 840
# Django-side LLM features (e.g. Core meditation compose) use basecamp's loopback Ollama here instead
# of OpenRouter (no cloud keys exist in this stack). Only this settings module defines these.
LOCAL_TEST_LLM_URL = "http://127.0.0.1:11434/v1"
LOCAL_TEST_LLM_MODEL = "qwen3.8:27b-obliterated-q8"
LOCAL_TEST_LLM_TIMEOUT = 600
# Core narration: no Gemini key here, so meditations render with the existing mock voice.
LOCAL_TEST_CORE_MOCK_TTS = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
SAUTAI_M2M_BASE_URL = "http://127.0.0.1:8000"
SAUTAI_PLATFORM_SECRET = os.environ.get("SAUTAI_PLATFORM_SECRET", "")
ROOT_URLCONF = "deploy.local-test.urls"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "root": {"handlers": ["null"], "level": "CRITICAL"},
}
