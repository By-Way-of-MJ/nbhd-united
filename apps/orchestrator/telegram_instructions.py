"""Render managed channel instructions before writing them to tenant shares."""

import re

from apps.router.telegram_availability import telegram_inbound_enabled

SUPPORTED = "- Telegram and LINE are fully supported. Never assume a message came from Telegram."
RETIRED = (
    "- The NBHD app and LINE are supported. Telegram chat is not available and must not be offered or suggested "
    "for linking; a stored Telegram link does not mean replies work there. Never assume a message came from Telegram."
)


def replace_section_lines(current: str, heading: str | tuple[str, ...], replacements: dict[str, str]) -> str:
    """Replace exact managed lines only under a real level-two heading.

    Headings inside fenced examples are not sections. Preserve every other byte,
    including user Notes, code examples, line endings, and unknown custom prose.
    """
    headings = (heading,) if isinstance(heading, str) else heading
    active = False
    fence = None
    result = []
    for line in current.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", text)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
        else:
            if re.match(r"^ {0,3}#{1,2}\s", text):
                active = text.lstrip() in headings
            if active and text in replacements:
                line = replacements[text] + line[len(text) :]
        result.append(line)
    return "".join(result)


_GUIDE_HEADING = "## Multi-Channel (Telegram + LINE)"
_GUIDE_RETIRED_HEADING = "## Messaging channels"
_GUIDE_SUPPORTED = "**What:** The user can connect both Telegram and LINE and choose a preferred channel"
_GUIDE_SUPPORTED_END = "for proactive messages. Managed at `/settings/integrations`."
_GUIDE_RETIRED = (
    "**What:** The NBHD app and LINE are supported. Telegram chat is not available and must not be offered "
    "or suggested for linking."
)
_GUIDE_RETIRED_END = "A stored Telegram link does not mean replies work there. Manage LINE at `/settings/integrations`."


def render_platform_guide(current: str) -> str:
    """The named Multi-Channel passage is platform-managed; its template stays unchanged."""
    pairs = {
        _GUIDE_HEADING: _GUIDE_RETIRED_HEADING,
        _GUIDE_SUPPORTED: _GUIDE_RETIRED,
        _GUIDE_SUPPORTED_END: _GUIDE_RETIRED_END,
    }
    if telegram_inbound_enabled():
        pairs = {retired: supported for supported, retired in pairs.items()}
    return replace_section_lines(current, (_GUIDE_HEADING, _GUIDE_RETIRED_HEADING), pairs)
