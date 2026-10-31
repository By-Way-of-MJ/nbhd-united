"""Tests for the log redaction filters.

The Telegram leak these guard against: httpx logs every outbound request at INFO
with the URL — and the Telegram Bot API puts the token in the URL path — so
without that filter the shared bot token streams into Log Analytics on every
call. The httpx-query filter guards the same request lines against user data in
query strings (e.g. geocode place names).
"""

from __future__ import annotations

import ast
import io
import json
import logging
import subprocess
import sys
from pathlib import Path

import httpx
from django.test import SimpleTestCase

from apps.router.logging_filters import (
    RedactHttpxQuery,
    RedactTelegramToken,
    scrub_telegram_token,
)

# Obviously-fake token in the classic Telegram shape: <id>:<35 url-safe chars>.
_FAKE_ID = "123456789"
_FAKE_SECRET = "AAH_Fake-Token_value_000111222333444"  # noqa: S105 (not a real secret)
_FAKE_TOKEN = f"{_FAKE_ID}:{_FAKE_SECRET}"


def _record(msg, args=(), name="httpx"):
    """Build a LogRecord the way the logging machinery does, so getMessage()
    behaves exactly as it will in production."""
    return logging.LogRecord(
        name=name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


class ScrubHelperTests(SimpleTestCase):
    def test_redacts_secret_keeps_id(self):
        url = f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates"
        out = scrub_telegram_token(url)
        self.assertNotIn(_FAKE_SECRET, out)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", out)
        # Surrounding URL structure is preserved for debuggability.
        self.assertEqual(out, f"https://api.telegram.org/bot{_FAKE_ID}:[REDACTED]/getUpdates")

    def test_redacts_bare_token(self):
        self.assertEqual(scrub_telegram_token(_FAKE_TOKEN), f"{_FAKE_ID}:[REDACTED]")

    def test_redacts_file_download_url(self):
        url = f"https://api.telegram.org/file/bot{_FAKE_TOKEN}/photos/file_1.jpg"
        out = scrub_telegram_token(url)
        self.assertNotIn(_FAKE_SECRET, out)
        self.assertIn(f"bot{_FAKE_ID}:[REDACTED]/photos/file_1.jpg", out)

    def test_leaves_innocent_text_untouched(self):
        for text in (
            'HTTP Request: GET https://openrouter.ai/api/v1/chat "HTTP/1.1 200 OK"',
            "sendMessage failed (429): too many requests",
            "tenant 148ccf1c-ef13-47f8-a woke at 12:34:56",  # colons, but no token shape
            "id=123456 status=ok",  # digits + colon-free
        ):
            self.assertEqual(scrub_telegram_token(text), text)


class RedactTelegramTokenFilterTests(SimpleTestCase):
    def setUp(self):
        self.filter = RedactTelegramToken()

    def test_scrubs_httpx_record_with_url_object_in_args(self):
        # This is the exact shape httpx emits: the URL rides in args as a
        # httpx.URL object, NOT a string — the crux of the leak. A naive
        # isinstance(arg, str) filter would miss it.
        url = httpx.URL(f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates")
        record = _record(
            'HTTP Request: %s %s "%s %d %s"',
            ("GET", url, "HTTP/1.1", 200, "OK"),
        )
        self.assertTrue(self.filter.filter(record))

        rendered = record.getMessage()
        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", rendered)
        # The non-secret parts of the log line survive.
        self.assertIn("HTTP Request: GET", rendered)
        self.assertIn("/getUpdates", rendered)
        self.assertIn('"HTTP/1.1 200 OK"', rendered)
        # args were collapsed into the redacted message.
        self.assertEqual(record.args, ())

    def test_scrubs_token_embedded_directly_in_msg(self):
        record = _record(f"downloading https://api.telegram.org/file/bot{_FAKE_TOKEN}/x.jpg")
        self.assertTrue(self.filter.filter(record))
        rendered = record.getMessage()
        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertIn(f"bot{_FAKE_ID}:[REDACTED]/x.jpg", rendered)

    def test_scrubs_bare_token_passed_as_arg(self):
        record = _record("token configured: %s", (_FAKE_TOKEN,))
        self.assertTrue(self.filter.filter(record))
        rendered = record.getMessage()
        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", rendered)

    def test_innocent_record_passes_through_unmodified(self):
        # A non-token record must keep BOTH its template and its args intact,
        # so lazy %-formatting and downstream structured logging are unaffected.
        record = _record("sendMessage failed (%s): %s", (429, "rate limited"))
        self.assertTrue(self.filter.filter(record))
        self.assertEqual(record.msg, "sendMessage failed (%s): %s")
        self.assertEqual(record.args, (429, "rate limited"))
        self.assertEqual(record.getMessage(), "sendMessage failed (429): rate limited")

    def test_filter_never_drops_records(self):
        # Even a record whose args would raise on formatting must survive.
        record = _record("bad format %d", ("not-an-int",))
        self.assertTrue(self.filter.filter(record))

    def test_httpx_url_arg_scrubbed_and_innocent_arg_record_untouched(self):
        # Regression guard for the two behaviours that matter most, asserted
        # side by side: (1) the httpx.URL-in-args leak shape is scrubbed and its
        # args collapsed; (2) an innocent record with args is left completely
        # untouched — template AND args survive for lazy %-formatting.
        leaky = _record(
            "HTTP Request: %s %s",
            ("GET", httpx.URL(f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates")),
        )
        self.assertTrue(self.filter.filter(leaky))
        self.assertNotIn(_FAKE_SECRET, leaky.getMessage())
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", leaky.getMessage())
        self.assertEqual(leaky.args, ())

        innocent = _record("HTTP Request: %s %s", ("GET", "https://openrouter.ai/api/v1/chat"))
        self.assertTrue(self.filter.filter(innocent))
        self.assertEqual(innocent.msg, "HTTP Request: %s %s")
        self.assertEqual(innocent.args, ("GET", "https://openrouter.ai/api/v1/chat"))

    def test_handler_redacts_http_status_error_exc_info(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            logging.Formatter(
                "{levelname} {asctime} {name} {message}",
                style="{",
            )
        )
        handler.addFilter(self.filter)

        request = httpx.Request(
            "POST",
            f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates",
        )
        response = httpx.Response(409, request=request)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            record = logging.LogRecord(
                name="apps.router.poller",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="Error in poll loop, backing off %ds",
                args=(1,),
                exc_info=sys.exc_info(),
            )

        handler.handle(record)
        rendered = stream.getvalue()

        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", rendered)
        self.assertIn("httpx.HTTPStatusError", rendered)
        self.assertIn("/getUpdates", rendered)

    def test_handler_redacts_stack_info(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            logging.Formatter(
                "{levelname} {asctime} {name} {message}",
                style="{",
            )
        )
        handler.addFilter(self.filter)
        record = _record("diagnostic stack")
        record.stack_info = f"Stack: https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates"

        handler.handle(record)
        rendered = stream.getvalue()

        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", rendered)
        self.assertIn("/getUpdates", rendered)


class RedactHttpxQueryFilterTests(SimpleTestCase):
    def setUp(self):
        self.filter = RedactHttpxQuery()

    def test_scrubs_query_from_httpx_url_object_in_args(self):
        # The exact httpx request-line shape: URL rides in args as an
        # httpx.URL object and carries a user-supplied geocode place name.
        url = httpx.URL("https://geocoding-api.open-meteo.com/v1/search?name=Houston&count=10")
        record = _record(
            'HTTP Request: %s %s "%s %d %s"',
            ("GET", url, "HTTP/1.1", 200, "OK"),
        )
        self.assertTrue(self.filter.filter(record))

        rendered = record.getMessage()
        self.assertNotIn("Houston", rendered)
        self.assertIn("geocoding-api.open-meteo.com/v1/search?[REDACTED]", rendered)
        self.assertIn('"HTTP/1.1 200 OK"', rendered)
        # args were collapsed into the redacted message.
        self.assertEqual(record.args, ())

    def test_record_without_query_is_untouched(self):
        record = _record(
            'HTTP Request: %s %s "%s %d %s"',
            (
                "GET",
                httpx.URL("https://api.open-meteo.com/v1/forecast"),
                "HTTP/1.1",
                200,
                "OK",
            ),
        )
        self.assertTrue(self.filter.filter(record))
        # Template AND args survive for lazy %-formatting.
        self.assertEqual(record.msg, 'HTTP Request: %s %s "%s %d %s"')
        self.assertEqual(
            record.args,
            ("GET", httpx.URL("https://api.open-meteo.com/v1/forecast"), "HTTP/1.1", 200, "OK"),
        )
        self.assertNotIn("[REDACTED]", record.getMessage())

    def test_non_httpx_logger_is_untouched(self):
        record = _record(
            "weather lookup for %s",
            ("https://geocoding-api.open-meteo.com/v1/search?name=Houston",),
            name="apps.orchestrator.briefing_weather",
        )
        self.assertTrue(self.filter.filter(record))
        self.assertEqual(record.msg, "weather lookup for %s")
        self.assertEqual(
            record.args,
            ("https://geocoding-api.open-meteo.com/v1/search?name=Houston",),
        )
        self.assertIn("?name=Houston", record.getMessage())

    def test_telegram_token_and_query_both_scrubbed_in_production_order(self):
        # Production order is Telegram first, then httpx query. The first filter
        # pre-renders the record; the second must still scrub the query from it.
        url = httpx.URL(f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates?name=Houston")
        record = _record(
            'HTTP Request: %s %s "%s %d %s"',
            ("GET", url, "HTTP/1.1", 200, "OK"),
        )
        self.assertTrue(RedactTelegramToken().filter(record))
        self.assertTrue(self.filter.filter(record))

        rendered = record.getMessage()
        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertNotIn("Houston", rendered)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", rendered)
        self.assertIn("?[REDACTED]", rendered)
        self.assertIn('"HTTP/1.1 200 OK"', rendered)


class ProductionLoggingConfigTests(SimpleTestCase):
    """The filter only closes the leak if production actually wires it onto the
    console handler. base.py defines no LOGGING dict — production.py owns it — so
    we read the LOGGING structure straight from the production settings *source*
    via ast rather than importing the module (importing it would mutate the
    shared DATABASES dict and read deploy-only env vars)."""

    def _production_logging(self) -> dict:
        src = Path(__file__).resolve().parents[2] / "config" / "settings" / "production.py"
        tree = ast.parse(src.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "LOGGING" for target in node.targets
            ):
                return ast.literal_eval(node.value)
        self.fail("No module-level LOGGING dict found in config/settings/production.py")

    def test_console_handler_attaches_telegram_filter(self):
        logging_cfg = self._production_logging()

        filters = logging_cfg["filters"]
        self.assertIn("redact_telegram_token", filters)
        self.assertEqual(
            filters["redact_telegram_token"]["()"],
            "apps.router.logging_filters.RedactTelegramToken",
        )

        console_filters = logging_cfg["handlers"]["console"]["filters"]
        self.assertIn("redact_telegram_token", console_filters)
        # We add, not replace: the pre-existing BYO filter must stay wired.
        self.assertIn("redact_byo_paste_body", console_filters)

    def test_console_handler_attaches_httpx_query_filter(self):
        logging_cfg = self._production_logging()

        filters = logging_cfg["filters"]
        self.assertIn("redact_httpx_query", filters)
        self.assertEqual(
            filters["redact_httpx_query"]["()"],
            "apps.router.logging_filters.RedactHttpxQuery",
        )

        console_filters = logging_cfg["handlers"]["console"]["filters"]
        self.assertIn("redact_httpx_query", console_filters)
        # Appended after the pre-existing two, preserving their order.
        self.assertEqual(
            console_filters,
            ["redact_byo_paste_body", "redact_telegram_token", "redact_httpx_query"],
        )

    def test_azure_warning_level(self):
        self.assertEqual(self._production_logging()["loggers"]["azure"], {"level": "WARNING"})

    def test_azure_child_filters_info_and_propagates_warning(self):
        # dictConfig closes existing handlers: isolate it in a fresh process so
        # the suite's logging state (including handler registries) is untouched.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, logging.config, sys\n"
                "logging.config.dictConfig(json.load(sys.stdin))\n"
                "child = logging.getLogger('azure.core.pipeline.policies.http_logging_policy')\n"
                "child.info('azure info marker')\n"
                "child.warning('azure warning marker')\n",
            ],
            input=json.dumps(self._production_logging()),
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("azure.core.pipeline.policies.http_logging_policy azure warning marker", result.stderr)
        self.assertNotIn("azure info marker", result.stderr + result.stdout)
