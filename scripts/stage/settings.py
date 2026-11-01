"""Stage-only settings; imported from a read-only mount, never production."""

import os

from apps.orchestrator import local_test
from config.settings.development import *  # noqa: F403,F401

if os.environ.get("AZURE_MOCK") != "true" or os.environ.get("NBHD_STAGE") != "1":
    raise RuntimeError("Stage requires explicit mock storage and stage environment")

LOCAL_TEST_ROOT = "/home/node/.openclaw"
LOCAL_TEST_CHAT_TIMEOUT = 300
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
GRAVITY_ENABLED = False
ROOT_URLCONF = "urls"


def keep_production_gateway(config, tenant):
    """Use the local storage adapter without its separate Ollama config rewrite."""
    local_test.local_root(tenant.id)


# The existing local adapter couples storage with an Ollama-only rewrite.
# This process-local adapter leaves the real config generator's output intact.
local_test.configure_gateway = keep_production_gateway
