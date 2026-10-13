"""One availability source for Telegram conversational and interactive surfaces."""

from django.conf import settings

TELEGRAM_UNAVAILABLE_DETAIL = "Telegram replies are currently unavailable. Please use the NBHD app or LINE."


def telegram_inbound_enabled() -> bool:
    return settings.TELEGRAM_INBOUND_ENABLED
