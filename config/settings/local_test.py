"""Loaded only by deploy/local-test/run.py with a clean generated environment."""

import os

from .development import *  # noqa: F403,F401

if os.environ.get("AZURE_MOCK") != "true":
    raise RuntimeError("The local test stack cannot use Azure")
LOCAL_TEST_ROOT = os.environ["LOCAL_TEST_ROOT"]
# Local model turns outlast the 120 s fleet default; stay below yuki_local's 900 s poll.
LOCAL_TEST_CHAT_TIMEOUT = 840
# Django-side LLM features (e.g. Core meditation compose) use basecamp's loopback Ollama here instead
# of OpenRouter (no cloud keys exist in this stack). Only this settings module defines these.
LOCAL_TEST_LLM_URL = "http://127.0.0.1:11434/v1"
LOCAL_TEST_LLM_MODEL = "qwen3.8:27b-obliterated-q8"
LOCAL_TEST_LLM_TIMEOUT = 600
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
