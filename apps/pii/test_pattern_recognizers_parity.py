"""Offline fixed expectations, version-checked parity, and worker import contracts.

Both email implementations use tldextract's bundled snapshot, without disk cache
or HTTP. CI/Docker must use the requirements.txt Presidio pin. The laptop's
2.2.363 venv needs PII_PARITY_ALLOW_PRESIDIO_VERSION_DRIFT=1 while the pin is
2.2.364; that explicit local opt-out prints a warning, never silently skips parity.
"""

import json
import os
import random
import string
import subprocess
import sys
import warnings
from collections import Counter
from contextlib import contextmanager
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import phonenumbers
import regex
import tldextract

ROOT = Path(__file__).resolve().parents[2]
REGIONS = ("US", "GB", "JP", "CA", "AU", "DE", "FR", "IN", "BR", "IL")
IBANS = (
    "DE89370400440532013000",
    "GB82WEST12345698765432",
    "FR1420041010050500013M02606",
    "ES9121000418450200051332",
    "NL91ABNA0417164300",
    "BE68539007547034",
    "AT611904300234573201",
    "CH9300762011623852957",
    "IT60X0542811101000000123456",
    "NO9386011117947",
    "PL61109010140000071219812874",
    "IE29AIBK93115212345678",
    "DK5000400440116243",
    "FI2112345600000785",
    "PT50000201231234567890154",
)


def old_recognizers():
    # Frozen origin/main 6182bf30 factory, independent of production constants.
    from presidio_analyzer.predefined_recognizers import (
        CreditCardRecognizer,
        EmailRecognizer,
        IbanRecognizer,
        PhoneRecognizer,
    )

    class DualEmail:
        def __init__(self):
            self.unicode = EmailRecognizer()
            self.ascii = EmailRecognizer()
            self.ascii.global_regex_flags |= regex.ASCII

        def analyze(self, *, text, entities):
            unique = {}
            for result in self.unicode.analyze(text=text, entities=entities):
                unique[(result.entity_type, result.start, result.end)] = result
            for result in self.ascii.analyze(text=text, entities=entities):
                key = (result.entity_type, result.start, result.end)
                previous = unique.get(key)
                if previous is None or result.score > previous.score:
                    unique[key] = result
            return sorted(unique.values(), key=lambda result: (result.start, result.end))

    phone = PhoneRecognizer(supported_regions=REGIONS)
    phone.SCORE = 0.85
    return {
        "CREDIT_CARD": CreditCardRecognizer(),
        "IBAN_CODE": IbanRecognizer(),
        "EMAIL_ADDRESS": DualEmail(),
        "PHONE_NUMBER": phone,
    }


def corpus_texts():
    from apps.pii.eval_corpus import CASES, KNOWN_ENTITY_CASES, SEQUENCE_CASES

    golden = json.loads((Path(__file__).with_name("golden_set.json")).read_text())
    return (
        [case["text"] for case in golden]
        + [case.text for case in (*CASES, *KNOWN_ENTITY_CASES)]
        + [text for case in SEQUENCE_CASES for text in case.turns]
    )


def hand_texts():
    texts = [
        "",
        "5x5 at 315",
        "door code 7391",
        "PIN 4821",
        "さんとtaro@example.jpに",
        "a+b@sub.example.com, then other@example.org.",
        "(foo@example.com); foo@example.invalid foo@localhost",
        "élise@例え.jp тест@xn--80ak6aa92e.com",
        "a@foo--bar.com a@-bad.com",
        "email card bank phone: nobody here",
        "a@b.co\nsecond@example.com",
    ]
    for card in (
        "4111111111111111",
        "4111111111111112",
        "378282246310005",
        "5555555555554444",
        "6011111111111117",
        "1234567890123",
    ):
        for separator in ("", " ", "-"):
            texts.append("card: " + separator.join(card[i : i + 4] for i in range(0, len(card), 4)) + ".")
    # Valid Luhn runs at each edge length, plus one-digit-off versions.
    for length in range(12, 21):
        base = "4" + "0" * (length - 2)
        for last in string.digits:
            card = base + last
            texts.extend(
                (
                    card,
                    " ".join(card[i : i + 4] for i in range(0, length, 4)),
                    "-".join(card[i : i + 4] for i in range(0, length, 4)),
                )
            )
    for iban in IBANS:
        invalid = iban[:3] + str((int(iban[3]) + 1) % 10) + iban[4:]
        for value in (iban, invalid):
            spaced = " ".join(value[i : i + 4] for i in range(0, len(value), 4))
            texts.extend((value, value.lower(), spaced, spaced.lower(), spaced.replace(" ", "-")))
        texts.extend((iban + " 2 days from today", iban + " ABCD XYZ", "x" + iban + "x"))
    for region in REGIONS:
        number = phonenumbers.example_number(region)
        for style in (phonenumbers.PhoneNumberFormat.NATIONAL, phonenumbers.PhoneNumberFormat.INTERNATIONAL):
            texts.append(f"Call {phonenumbers.format_number(number, style)} today.")
    return texts


def fuzz_texts():
    rng = random.Random(6182)
    atoms = [
        "さんと",
        "に",
        "@",
        ".com",
        "+",
        "-",
        " ",
        "\n",
        "bank",
        "4111111111111111",
        "DE89370400440532013000",
        "taro@example.jp",
        "０１２３",
        "١٢٣٤",
    ]
    for _ in range(5000):
        digits = "".join(rng.choices(string.digits, k=rng.randrange(1, 45)))
        if rng.randrange(2):
            yield digits
        else:
            yield "".join(rng.choices(atoms, k=rng.randrange(1, 10))) + digits


def spans(recognizer, entity, text):
    return [(r.entity_type, r.start, r.end, r.score) for r in recognizer.analyze(text=text, entities=[entity])]


@contextmanager
def bundled_suffixes():
    """Use the same fresh snapshot for both implementations; fail on any GET."""
    extractor = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(), fallback_to_snapshot=True)
    with (
        patch("tldextract.tldextract.TLD_EXTRACTOR", extractor),
        patch("requests.sessions.Session.get", side_effect=AssertionError("Unexpected network fetch")) as fetch,
    ):
        try:
            yield
        finally:
            fetch.assert_not_called()


class OfflinePatternTests(TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(bundled_suffixes())


class FixedPatternExpectationsTests(OfflinePatternTests):
    """Hand-computed spans/scores; no Presidio imports or oracle consultation."""

    def check_cases(self, entity, cases):
        from apps.pii.engine import get_pattern_recognizers

        recognizer = get_pattern_recognizers()[entity]
        for text, expected in cases:
            with self.subTest(entity=entity, text=text):
                self.assertEqual(spans(recognizer, entity, text), expected)

    def test_credit_cards(self):
        self.check_cases(
            "CREDIT_CARD",
            [
                ("4111 1111 1111 1111", [("CREDIT_CARD", 0, 19, 1.0)]),
                ("Card: 4111-1111-1111-1111.", [("CREDIT_CARD", 6, 25, 1.0)]),
                ("4111 1111 1111 1112", []),
            ],
        )

    def test_ibans(self):
        self.check_cases(
            "IBAN_CODE",
            [
                ("GB82WEST12345698765432", [("IBAN_CODE", 0, 22, 1.0)]),
                ("GB82 WEST 1234 5698 7654 32", [("IBAN_CODE", 0, 27, 1.0)]),
                ("DE89370400440532013000", [("IBAN_CODE", 0, 22, 1.0)]),
                ("DE89 3704 0044 0532 0130 00", [("IBAN_CODE", 0, 27, 1.0)]),
                ("DE89370400440532013001", []),
                ("gb82west12345698765432", []),
            ],
        )

    def test_lowercase_iban_validation_none_retains_pattern_score(self):
        from apps.pii.engine import get_pattern_recognizers

        recognizer = get_pattern_recognizers()["IBAN_CODE"]
        text = "gb82west12345698765432"
        self.assertIsNone(recognizer.validate_result(text))
        # The factory is case-sensitive. Explicitly enable matching here to
        # exercise validate_result(None) -> 0.5 without changing that default.
        with patch.object(recognizer, "global_regex_flags", recognizer.global_regex_flags | regex.IGNORECASE):
            self.assertEqual(spans(recognizer, "IBAN_CODE", text), [("IBAN_CODE", 0, 22, 0.5)])

    def test_emails(self):
        self.check_cases(
            "EMAIL_ADDRESS",
            [
                ("taro@example.jp", [("EMAIL_ADDRESS", 0, 15, 1.0)]),
                ("さんとtaro@example.jpに", [("EMAIL_ADDRESS", 3, 18, 1.0)]),
                ("person@example.invalid", []),
            ],
        )

    def test_phones(self):
        self.check_cases(
            "PHONE_NUMBER",
            [
                ("(202) 555-0123", [("PHONE_NUMBER", 0, 14, 0.85)]),
                ("020 7946 0018", [("PHONE_NUMBER", 0, 13, 0.85)]),
                ("03-1234-5678", [("PHONE_NUMBER", 0, 12, 0.85)]),
                ("+81 3-1234-5678", [("PHONE_NUMBER", 0, 15, 0.85)]),
                ("5x5 at 315", []),
                ("door code 7391", []),
                ("PIN 4821", []),
            ],
        )


class PatternParityTests(OfflinePatternTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        installed = version("presidio-analyzer")
        pins = regex.findall(r"(?m)^presidio-analyzer==([^\s;]+)", (ROOT / "requirements.txt").read_text())
        if len(pins) != 1:
            raise AssertionError("Expected exactly one presidio-analyzer pin in requirements.txt")
        pinned = pins[0]
        print(
            f"PRESIDIO_ORACLE installed={installed} requirements_pin={pinned} "
            f"source={find_spec('presidio_analyzer').origin}",
            flush=True,
        )
        if installed != pinned:
            message = f"Presidio oracle version drift: installed={installed}, requirements_pin={pinned}"
            if os.environ.get("PII_PARITY_ALLOW_PRESIDIO_VERSION_DRIFT") != "1":
                raise AssertionError(message + "; local opt-out: PII_PARITY_ALLOW_PRESIDIO_VERSION_DRIFT=1")
            warnings.warn(message + "; explicit local opt-out enabled", RuntimeWarning, stacklevel=2)

    def test_patterns_country_table_and_validation_semantics(self):
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            EmailRecognizer,
            IbanRecognizer,
        )
        from presidio_analyzer.predefined_recognizers.generic.iban_patterns import regex_per_country

        from apps.pii import pattern_recognizers as port

        self.assertEqual(port.regex_per_country, regex_per_country)
        for upstream, replacement in (
            (CreditCardRecognizer, port.CreditCardRecognizer),
            (EmailRecognizer, port.EmailRecognizer),
            (IbanRecognizer, port.IbanRecognizer),
        ):
            self.assertEqual(replacement.PATTERN, upstream.PATTERNS[0].regex)
            self.assertEqual(replacement.SCORE, upstream.PATTERNS[0].score)
            self.assertEqual(replacement.global_regex_flags, upstream().global_regex_flags)
        # Lowercase IBAN validation returns None (retain 0.5), even though the
        # default uppercase matching regex will not detect the lowercase code.
        for value in IBANS:
            for candidate in (value, value.lower(), value + "0", " " + value):
                self.assertIs(
                    port.IbanRecognizer().validate_result(candidate), IbanRecognizer().validate_result(candidate)
                )
        self.assertIsNone(port.IbanRecognizer().validate_result(IBANS[0].lower()))
        self.assertFalse(port.IbanRecognizer().validate_result("ZZ00!"))
        for recognizer in (CreditCardRecognizer(), port.CreditCardRecognizer()):
            with self.assertRaises(ValueError):
                recognizer.validate_result("4111x11111111111")

    def test_all_corpora_and_seeded_fuzz(self):
        from apps.pii.engine import get_pattern_recognizers
        from apps.pii.pattern_recognizers import CreditCardRecognizer

        actual = get_pattern_recognizers()
        self.assertIsInstance(actual["CREDIT_CARD"], CreditCardRecognizer)
        self.assertIs(actual, get_pattern_recognizers())
        expected = old_recognizers()
        groups = {"corpus": corpus_texts(), "hand": hand_texts(), "fuzz": list(fuzz_texts())}
        positives = Counter()
        for group, texts in groups.items():
            for index, text in enumerate(texts):
                for entity, recognizer in expected.items():
                    with self.subTest(group=group, index=index, entity=entity, text=text):
                        oracle_spans = spans(recognizer, entity, text)
                        positives[entity] += len(oracle_spans)
                        self.assertEqual(spans(actual[entity], entity, text), oracle_spans)
        for entity in expected:
            self.assertGreater(positives[entity], 0, f"Oracle never detected {entity}")
        self.assertGreater(sum(positives.values()), 100, "Oracle detections unexpectedly sparse")
        print(
            "PATTERN_PARITY "
            + " ".join(f"{key}={len(value)}" for key, value in groups.items())
            + f" comparisons={4 * sum(map(len, groups.values()))}"
            + f" positives={dict(positives)}"
        )


class PatternImportTests(TestCase):
    def check_process(self, warm):
        script = r"""
import importlib.util
import os
import sys
from unittest.mock import Mock, patch
import environ
# No .env reads, even when this module is run outside the offline wrapper.
environ.Env.read_env = classmethod(lambda cls, *args, **kwargs: None)
import django
django.setup()
from apps.pii.engine import get_pattern_recognizers
if sys.argv[1] == "warm":
    spec = importlib.util.spec_from_file_location("gunicorn_config", "gunicorn.conf.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worker = Mock()
    with patch.object(module, "_wait_for_shared_detector"), patch(
        "apps.crypto.prewarm.start_prewarm_thread"
    ):
        module.post_worker_init(worker)
    worker.log.warning.assert_not_called()
    worker.log.error.assert_not_called()
    worker.log.info.assert_any_call("post_worker_init: PII pattern recognizers warmed")
recognizers = get_pattern_recognizers()
from apps.pii.test_pattern_recognizers_parity import bundled_suffixes
with bundled_suffixes():
    for entity, recognizer in recognizers.items():
        recognizer.analyze(text="4111111111111111 DE89370400440532013000 taro@example.jp +1 202-555-0123", entities=[entity])
# Resolve the URL graph too, covering imports beyond AppConfig.ready().
import config.urls
from apps.pii.engine import get_pii_pipeline
if os.environ["PII_DETECTOR_TRANSPORT"] == "shared":
    assert type(get_pii_pipeline()).__name__ == "SharedPiiPipeline"
blocked = {"presidio_analyzer", "torch", "transformers", "spacy", "thinc", "triton"}
loaded = sorted(name for name in sys.modules if name.split(".")[0] in blocked)
assert not loaded, loaded
print("NO_HEAVY_IMPORTS")
"""
        env = os.environ.copy()
        env.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SECRET_KEY="test-only",
            NBHD_DISABLE_BACKGROUND_THREADS="True",
            AZURE_MOCK="true",
            PII_DETECTOR_TRANSPORT="shared" if warm else "local",
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, "warm" if warm else "direct"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "NO_HEAVY_IMPORTS")

    def test_direct_imports(self):
        self.check_process(False)

    def test_shared_worker_warm_imports(self):
        self.check_process(True)
