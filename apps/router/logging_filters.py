"""Logging filters that scrub secrets out of log records.

Why this exists: every Telegram Bot API call is made over httpx with the bot
token embedded in the URL *path* — e.g.

    https://api.telegram.org/bot<id>:<secret>/getUpdates

httpx logs each outbound request at INFO via the ``httpx`` logger:

    HTTP Request: GET https://api.telegram.org/bot<id>:<secret>/getUpdates "HTTP/1.1 200 OK"

In production the root logger is INFO with a single stdout ``console`` handler,
so that line — token and all — flows straight into Azure Container Apps Log
Analytics. The token is a long-lived credential for the shared platform bot and
must never reach a log sink.

This filter is attached to the console handler (see
``config/settings/production.py``) so it scrubs *every* record on its way to
stdout, regardless of which logger emitted it — httpx today, and any future code
that happens to log a constructed Bot API URL or the raw token. It is a
belt-and-suspenders backstop in the same spirit as
``apps.byo_models.logging_filters.RedactBYOPasteBody``.

The numeric bot id is preserved (it identifies the single shared bot and is not
itself the secret); only the auth secret after the colon is replaced, so log
lines stay readable as URLs while the credential is gone.

Separately, ``RedactHttpxQuery`` strips query strings from httpx request lines.
Queries routinely carry user data — the weather/geocoding calls in
``apps.tenants.situation`` and ``apps.orchestrator.briefing_weather`` send the
user's city as ``?name=<city>`` — and those lines otherwise flow at INFO into
Log Analytics. Host, path, and status stay readable; only the query is replaced.
"""

from __future__ import annotations

import logging
import re

# Telegram bot-token shape: a numeric bot id, a colon, then the auth secret
# (url-safe base64 — 35 chars in the classic format; the {30,} lower bound is
# loose so the pattern survives Telegram lengthening the token). Deliberately
# unanchored so it matches both the URL-path form (``…/bot<id>:<secret>/…`` —
# the leading ``bot`` is letters, so the match naturally starts at the digits)
# and a bare ``<id>:<secret>`` token. The 30+-char url-safe tail after the colon
# makes false positives against ordinary log content effectively impossible.
_TELEGRAM_TOKEN_RE = re.compile(r"(\d{6,}):[A-Za-z0-9_-]{30,}")
_REPLACEMENT = r"\1:[REDACTED]"

# httpx's request line: ``HTTP Request: GET https://host/path?query "HTTP/1.1 200 OK"``.
# Match an http(s) URL's host/path, then its ``?query`` up to whitespace or the
# closing quote, replacing only that query. The host/path and status stay intact.
_HTTPX_QUERY_RE = re.compile(r'(https?://[^\s"?]*)\?[^\s"]*')
_QUERY_REPLACEMENT = r"\1?[REDACTED]"


def scrub_telegram_token(text: str) -> str:
    """Return ``text`` with the secret half of any Telegram bot token replaced
    by ``[REDACTED]``, keeping the (non-secret) numeric bot id for readability."""
    return _TELEGRAM_TOKEN_RE.sub(_REPLACEMENT, text)


def scrub_httpx_query(text: str) -> str:
    """Return ``text`` with every http(s) URL query string replaced by
    ``[REDACTED]``, preserving the host, path, and surrounding log text."""
    return _HTTPX_QUERY_RE.sub(_QUERY_REPLACEMENT, text)


class RedactTelegramToken(logging.Filter):
    """Strip Telegram bot tokens from log records before a handler emits them.

    The token usually arrives inside ``record.args`` as an ``httpx.URL`` object
    (httpx logs ``request.url`` with ``%s``), so it is not a string until the
    record is formatted. We therefore render the message via
    ``record.getMessage()`` and, *only when* a token is present, collapse the
    record onto the redacted, fully-rendered text (``record.msg``) and clear
    ``record.args``. Records without a token are left untouched.

    The formatter appends exception and stack text *after* formatting the
    message. Pre-rendering ``exc_info`` into logging's cached ``exc_text`` lets
    us scrub that text before the handler formatter sees it; because
    ``Formatter.format`` reuses a populated ``exc_text``, it will not overwrite
    the redacted traceback. Stack text is scrubbed in place for the same reason.
    Always returns True — this filter redacts, it never drops records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # A formatting error here must never suppress the log record.
            pass
        else:
            if _TELEGRAM_TOKEN_RE.search(message):
                record.msg = scrub_telegram_token(message)
                record.args = ()

        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = scrub_telegram_token(record.exc_text)
        except Exception:
            # Redaction must not turn a logging failure into a dropped record.
            pass

        try:
            if record.stack_info:
                record.stack_info = scrub_telegram_token(record.stack_info)
        except Exception:
            pass
        return True


class RedactHttpxQuery(logging.Filter):
    """Strip query strings from httpx request records before a handler emits them.

    httpx logs every outbound request at INFO as
    ``HTTP Request: GET <url> "<status>"``. Query strings routinely carry user
    data — e.g. the geocoding lookups in ``apps.tenants.situation`` and
    ``apps.orchestrator.briefing_weather`` put the user's city in
    ``?name=<city>`` — so production root logging at INFO would stream that data
    into Log Analytics. Host, path, and status remain useful and are kept.

    Only records from the ``httpx`` logger (or an ``httpx.*`` child) are
    considered. As with ``RedactTelegramToken``, the URL usually rides in
    ``record.args`` as an ``httpx.URL`` object, so it is not a string until the
    record is formatted. We render via ``record.getMessage()`` and, *only when*
    an http(s) URL with a query is present, collapse the record onto the
    redacted, fully-rendered text (``record.msg``) and clear ``record.args``.
    Records without such a URL — and every other logger — are left untouched,
    args included. Always returns True — this filter redacts, it never drops.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.name == "httpx" or record.name.startswith("httpx."):
                message = record.getMessage()
                if _HTTPX_QUERY_RE.search(message):
                    record.msg = scrub_httpx_query(message)
                    record.args = ()
        except Exception:
            # A formatting error here must never suppress the log record.
            pass
        return True
