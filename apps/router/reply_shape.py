"""Count reply shape without retaining or logging conversation text."""

import logging
import re

from apps.router.chat_gates import _tenant_allowed

ONE_QUESTION_NOTE = (
    "[NBHD reply: Ask ONE question maximum this turn. Small pick: last line "
    "[[quick-replies: A | B]] with 2–4 short labels. Open question: no buttons.]"
)


def frame_reply_shape(tenant, content: str) -> str:
    """Prepend the ephemeral rule without changing the last user-message line."""
    if _tenant_allowed(tenant, "ONE_QUESTION_TENANT_IDS"):
        return ONE_QUESTION_NOTE + "\n" + content
    return content


logger = logging.getLogger(__name__)


def log_reply_shape(tenant, visible_text: str, quick_replies=None) -> None:
    """Log one count-only observation at the app reply's finalization seam."""
    if not _tenant_allowed(tenant, "ONE_QUESTION_TENANT_IDS"):
        return
    # Fenced blocks, including an unfinished fence, and indented Markdown code.
    fence = None
    questions = 0
    for line in visible_text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        if line.startswith(("    ", "\t")):
            continue
        questions += line.count("?") + line.count("？")
    logger.info(
        "reply_shape tenant=%s questions=%d buttons=%d", str(tenant.id)[:8], questions, int(bool(quick_replies))
    )
