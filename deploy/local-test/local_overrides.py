"""Process-local overrides for the local TEST Django server only."""

import os

from django.conf import settings

LOCAL_GENERATE_TIMEOUT_SECONDS = 1700.0


def apply():
    # The local sautai generates a real week with a local model, which takes
    # longer than the 125 s legacy synchronous timeout. The async contract needs
    # QStash, which is blank here, so give the synchronous POST room to finish,
    # still below `yuki_local chat-plan`'s 1800 s wait for the job.
    if not settings.LOCAL_TEST_ROOT or settings.DEBUG is not True or os.environ.get("AZURE_MOCK") != "true":
        raise RuntimeError("Refusing local timeout override outside the local test stack")
    from apps.integrations import sautai_client

    current = getattr(sautai_client, "REQUEST_TIMEOUT_SECONDS", None)
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise RuntimeError("sautai_client.REQUEST_TIMEOUT_SECONDS is missing; update the local override")
    sautai_client.REQUEST_TIMEOUT_SECONDS = LOCAL_GENERATE_TIMEOUT_SECONDS
