"""Direct pattern analysis ported from the installed presidio-analyzer 2.2.363 (MIT).

Parity was also checked against 2.2.364 from an extracted local uv cache;
the test oracle must match requirements.txt unless explicitly opted out locally.

Only the four default recognizers used by engine.py are ported. Direct analyze
calls do not apply NLP context enhancement. Explanations/registry metadata are
omitted: the redactor consumes only entity_type, start, end, and score.

Copyright (c) Microsoft Corporation. All rights reserved.
Licensed under the MIT License:
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import logging
import os
import string
from dataclasses import dataclass

import phonenumbers
import regex as re
import tldextract

logger = logging.getLogger(__name__)
REGEX_TIMEOUT_SECONDS = int(os.environ.get("REGEX_TIMEOUT_SECONDS", 60))


@dataclass(frozen=True)
class RecognizerResult:
    entity_type: str
    start: int
    end: int
    score: float


def _remove_duplicates(results):
    # Presidio orders by descending confidence, then start, then longest first,
    # and removes zero-score and same-type contained spans (not all overlaps).
    results = sorted(set(results), key=lambda r: (-r.score, r.start, -(r.end - r.start)))
    filtered = []
    for result in results:
        if result.score == 0:
            continue
        if any(
            result.start >= other.start and result.end <= other.end and result.entity_type == other.entity_type
            for other in filtered
        ):
            continue
        filtered.append(result)
    return filtered


class _PatternRecognizer:
    global_regex_flags = re.DOTALL | re.MULTILINE | re.IGNORECASE

    def analyze(self, text, entities, nlp_artifacts=None, regex_flags=None):
        flags = regex_flags if regex_flags else self.global_regex_flags
        # regex owns its compiled-pattern cache; no shared mutable Pattern cache
        # can race between the Unicode and ASCII email passes.
        pattern = re.compile(self.PATTERN, flags=flags)
        results = []
        try:
            for match in pattern.finditer(text, timeout=REGEX_TIMEOUT_SECONDS):
                start, end = match.span()
                if start == end:
                    continue
                valid = self.validate_result(text[start:end])
                score = self.SCORE if valid is None else (1.0 if valid else 0)
                if score > 0:
                    results.append(RecognizerResult(self.ENTITY_TYPE, start, end, score))
        except TimeoutError:
            logger.warning("Pattern regex timed out after %s seconds", REGEX_TIMEOUT_SECONDS)
        return _remove_duplicates(results)


class CreditCardRecognizer(_PatternRecognizer):
    ENTITY_TYPE = "CREDIT_CARD"
    SCORE = 0.3
    PATTERN = r"\b(?!1\d{12}(?!\d))((4\d{3})|(5[0-5]\d{2})|(6\d{3})|(1\d{3})|(3\d{3}))[- ]?(\d{3,4})[- ]?(\d{3,4})[- ]?(\d{3,5})\b"

    def validate_result(self, pattern_text):
        sanitized = pattern_text.replace("-", "").replace(" ", "")
        digits = [int(digit) for digit in sanitized]
        checksum = sum(digits[-1::-2])
        for digit in digits[-2::-2]:
            checksum += sum(int(d) for d in str(digit * 2))
        return checksum % 10 == 0


class EmailRecognizer(_PatternRecognizer):
    ENTITY_TYPE = "EMAIL_ADDRESS"
    SCORE = 0.5
    PATTERN = r"\b((([!#$%&'*+\-/=?^_`{|}~\w])|([!#$%&'*+\-/=?^_`{|}~\w][!#$%&'*+\-/=?^_`{|}~\.\w]{0,}[!#$%&'*+\-/=?^_`{|}~\w]))[@]\w+(?:-+\w+)*(?:\.\w+(?:-+\w+)*)+)\b"

    def validate_result(self, pattern_text):
        return tldextract.extract(pattern_text).fqdn != ""


class IbanRecognizer:
    ENTITY_TYPE = "IBAN_CODE"
    SCORE = 0.5
    PATTERN = (
        r"(?<![A-Z0-9])([A-Z]{2}[0-9]{2}(?:[ -]?[A-Z0-9]{4}){2,6})"
        r"((?:[ -]?[A-Z0-9]{4})?)((?:[ -]?[A-Z0-9]{1,3})?)(?![A-Z0-9])"
    )
    global_regex_flags = re.DOTALL | re.MULTILINE
    LETTERS = {ord(d): str(i) for i, d in enumerate(string.digits + string.ascii_uppercase)}

    def validate_result(self, pattern_text):
        try:
            value = pattern_text.replace("-", "").replace(" ", "")
            transformed = (value[:2] + "00" + value[4:]).upper()
            number = (transformed[4:] + transformed[:4]).translate(self.LETTERS)
            if f"{98 - (int(number) % 97):0>2}" == value[2:4]:
                if self._is_valid_format(value):
                    return True
                if self._is_valid_format(value.upper()):
                    return None
            return False
        except ValueError:
            # Unlike upstream's log, do not emit the candidate PII text.
            logger.error("Failed to validate IBAN text")
            return False

    @staticmethod
    def _is_valid_format(value):
        pattern = regex_per_country.get(value[:2], "")
        if pattern:
            try:
                # Default exact_match=False deliberately uses match, not fullmatch.
                return re.match(pattern, value, flags=re.DOTALL | re.MULTILINE, timeout=REGEX_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("IBAN format regex timed out after %s seconds", REGEX_TIMEOUT_SECONDS)
        return False

    def analyze(self, text, entities, nlp_artifacts=None, regex_flags=None):
        # Upstream IBAN analyze ignores its regex_flags argument.
        results = []
        try:
            matches = re.finditer(self.PATTERN, text, flags=self.global_regex_flags, timeout=REGEX_TIMEOUT_SECONDS)
            for match in matches:
                # Try progressively shorter capture groups, preserving suffix fallback.
                for group in reversed(range(1, len(match.groups()) + 1)):
                    start = match.span(0)[0]
                    end = match.span(group)[1] if match.span(group)[1] > 0 else match.span(0)[1]
                    candidate = text[start:end]
                    if not candidate:
                        continue
                    valid = self.validate_result(candidate)
                    score = self.SCORE if valid is None else (1.0 if valid else 0)
                    if score > 0:
                        results.append(RecognizerResult(self.ENTITY_TYPE, start, end, score))
                        break
        except TimeoutError:
            logger.warning("IBAN regex timed out after %s seconds", REGEX_TIMEOUT_SECONDS)
        return results


class PhoneRecognizer:
    SCORE = 0.4

    def __init__(self, supported_regions, leniency=1):
        self.supported_regions = supported_regions
        self.leniency = leniency

    def analyze(self, text, entities, nlp_artifacts=None):
        # Presidio's second parse only labels explanation metadata with the region;
        # it does not filter results or affect scores. The matcher does validation.
        results = [
            RecognizerResult("PHONE_NUMBER", match.start, match.end, self.SCORE)
            for region in self.supported_regions
            for match in phonenumbers.PhoneNumberMatcher(text, region, leniency=self.leniency)
        ]
        return _remove_duplicates(results)


# Presidio's complete country-format table, intentionally not corrected or
# modernized: even its permissive default prefix matches are part of parity.
# IBAN parts format
CC = "[A-Z]{2}"  # country code
CK = "[0-9]{2}[ ]?"  # checksum
BOS = "^"
EOS = "$"  # end of string

A = "[A-Z][ ]?"
A2 = "([A-Z][ ]?){2}"
A3 = "([A-Z][ ]?){3}"
A4 = "([A-Z][ ]?){4}"

C = "[a-zA-Z0-9][ ]?"
C2 = "([a-zA-Z0-9][ ]?){2}"
C3 = "([a-zA-Z0-9][ ]?){3}"
C4 = "([a-zA-Z0-9][ ]?){4}"

N = "[0-9][ ]?"
N2 = "([0-9][ ]?){2}"
N3 = "([0-9][ ]?){3}"
N4 = "([0-9][ ]?){4}"

regex_per_country = {
    # Albania (8n, 16c) ALkk bbbs sssx cccc cccc cccc cccc
    "AL": "(AL)" + CK + N4 + N4 + C4 + C4 + C4 + C4,
    # Andorra (8n, 12c) ADkk bbbb ssss cccc cccc cccc
    "AD": "(AD)" + CK + N4 + N4 + C4 + C4 + C4,
    # Austria (16n) ATkk bbbb bccc cccc cccc
    "AT": "(AT)" + CK + N4 + N4 + N4 + N4,
    # Azerbaijan    (4c,20n) AZkk bbbb cccc cccc cccc cccc cccc
    "AZ": "(AZ)" + CK + C4 + N4 + N4 + N4 + N4 + N4,
    # Bahrain   (4a,14c)    BHkk bbbb cccc cccc cccc cc
    "BH": "(BH)" + CK + A4 + C4 + C4 + C4 + C2,
    # Belarus (4c, 4n, 16c)   BYkk bbbb aaaa cccc cccc cccc cccc
    "BY": "(BY)" + CK + C4 + N4 + C4 + C4 + C4 + C4,
    # Belgium (12n)   BEkk bbbc cccc ccxx
    "BE": "(BE)" + CK + N4 + N4 + N4,
    # Bosnia and Herzegovina    (16n)   BAkk bbbs sscc cccc ccxx
    "BA": "(BA)" + CK + N4 + N4 + N4 + N4,
    # Brazil (23n,1a,1c) BRkk bbbb bbbb ssss sccc cccc ccct n
    "BR": "(BR)" + CK + N4 + N4 + N4 + N4 + N4 + N3 + A + C,
    # Bulgaria  (4a,6n,8c)  BGkk bbbb ssss ttcc cccc cc
    "BG": "(BG)" + CK + A4 + N4 + N + N + C2 + C4 + C2,
    # Costa Rica    (18n)   CRkk 0bbb cccc cccc cccc cc (0 = always zero)
    "CR": "(CR)" + CK + "[0]" + N3 + N4 + N4 + N4 + N2,
    # Croatia   (17n)   HRkk bbbb bbbc cccc cccc c
    "HR": "(HR)" + CK + N4 + N4 + N4 + N4 + N,
    # Cyprus    (8n,16c)    CYkk bbbs ssss cccc cccc cccc cccc
    "CY": "(CY)" + CK + N4 + N4 + C4 + C4 + C4 + C4,
    # Czech Republic    (20n)   CZkk bbbb ssss sscc cccc cccc
    "CZ": "(CZ)" + CK + N4 + N4 + N4 + N4 + N4,
    # Denmark   (14n)   DKkk bbbb cccc cccc cc
    "DK": "(DK)" + CK + N4 + N4 + N4 + N2,
    # Dominican Republic    (4a,20n)    DOkk bbbb cccc cccc cccc cccc cccc
    "DO": "(DO)" + CK + A4 + N4 + N4 + N4 + N4 + N4,
    # EAt Timor    (19n) TLkk bbbc cccc cccc cccc cxx
    "TL": "(TL)" + CK + N4 + N4 + N4 + N4 + N3,
    # Estonia   (16n) EEkk bbss cccc cccc cccx
    "EE": "(EE)" + CK + N4 + N4 + N4 + N4,
    # Faroe Islands    (14n) FOkk bbbb cccc cccc cx
    "FO": "(FO)" + CK + N4 + N4 + N4 + N2,
    # Finland   (14n) FIkk bbbb bbcc cccc cx
    "FI": "(FI)" + CK + N4 + N4 + N4 + N2,
    # France    (10n,11c,2n) FRkk bbbb bsss sscc cccc cccc cxx
    "FR": "(FR)" + CK + N4 + N4 + N2 + C2 + C4 + C4 + C + N2,
    # Georgia   (2c,16n)  GEkk bbcc cccc cccc cccc cc
    "GE": "(GE)" + CK + C2 + N2 + N4 + N4 + N4 + N2,
    # Germany   (18n) DEkk bbbb bbbb cccc cccc cc
    "DE": "(DE)" + CK + N4 + N4 + N4 + N4 + N2,
    # Gibraltar (4a,15c)  GIkk bbbb cccc cccc cccc ccc
    "GI": "(GI)" + CK + A4 + C4 + C4 + C4 + C3,
    # Greece    (7n,16c)  GRkk bbbs sssc cccc cccc cccc ccc
    "GR": "(GR)" + CK + N4 + N3 + C + C4 + C4 + C4 + C3,
    # Greenland     (14n) GLkk bbbb cccc cccc cc
    "GL": "(GL)" + CK + N4 + N4 + N4 + N2,
    # Guatemala (4c,20c)  GTkk bbbb mmtt cccc cccc cccc cccc
    "GT": "(GT)" + CK + C4 + C4 + C4 + C4 + C4 + C4,
    # Hungary   (24n) HUkk bbbs sssx cccc cccc cccc cccx
    "HU": "(HU)" + CK + N4 + N4 + N4 + N4 + N4 + N4,
    # Iceland   (22n) ISkk bbbb sscc cccc iiii iiii ii
    "IS": "(IS)" + CK + N4 + N4 + N4 + N4 + N4 + N2,
    # Ireland   (4c,14n)  IEkk aaaa bbbb bbcc cccc cc
    "IE": "(IE)" + CK + C4 + N4 + N4 + N4 + N2,
    # Israel (19n) ILkk bbbn nncc cccc cccc ccc
    "IL": "(IL)" + CK + N4 + N4 + N4 + N4 + N3,
    # Italy (1a,10n,12c)  ITkk xbbb bbss sssc cccc cccc ccc
    "IT": "(IT)" + CK + A + N3 + N4 + N3 + C + C3 + C + C4 + C3,
    # Jordan    (4a,22n)  JOkk bbbb ssss cccc cccc cccc cccc cc
    "JO": "(JO)" + CK + A4 + N4 + N4 + N4 + N4 + N4 + N2,
    # Kazakhstan    (3n,13c)  KZkk bbbc cccc cccc cccc
    "KZ": "(KZ)" + CK + N3 + C + C4 + C4 + C4,
    # Kosovo    (4n,10n,2n)   XKkk bbbb cccc cccc cccc
    "XK": "(XK)" + CK + N4 + N4 + N4 + N4,
    # Kuwait    (4a,22c)  KWkk bbbb cccc cccc cccc cccc cccc cc
    "KW": "(KW)" + CK + A4 + C4 + C4 + C4 + C4 + C4 + C2,
    # Latvia    (4a,13c)  LVkk bbbb cccc cccc cccc c
    "LV": "(LV)" + CK + A4 + C4 + C4 + C4 + C,
    # Lebanon   (4n,20c)  LBkk bbbb cccc cccc cccc cccc cccc
    "LB": "(LB)" + CK + N4 + C4 + C4 + C4 + C4 + C4,
    # LiechteNtein (5n,12c)  LIkk bbbb bccc cccc cccc c
    "LI": "(LI)" + CK + N4 + N + C3 + C4 + C4 + C,
    # Lithuania (16n) LTkk bbbb bccc cccc cccc
    "LT": "(LT)" + CK + N4 + N4 + N4 + N4,
    # Luxembourg    (3n,13c)  LUkk bbbc cccc cccc cccc
    "LU": "(LU)" + CK + N3 + C + C4 + C4 + C4,
    # Malta (4a,5n,18c)   MTkk bbbb ssss sccc cccc cccc cccc ccc
    "MT": "(MT)" + CK + A4 + N4 + N + C3 + C4 + C4 + C4 + C3,
    # Mauritania    (23n) MRkk bbbb bsss sscc cccc cccc cxx
    "MR": "(MR)" + CK + N4 + N4 + N4 + N4 + N4 + N3,
    # Mauritius (4a,19n,3a)   MUkk bbbb bbss cccc cccc cccc 000m mm
    "MU": "(MU)" + CK + A4 + N4 + N4 + N4 + N4 + N3 + A,
    # Moldova   (2c,18c)  MDkk bbcc cccc cccc cccc cccc
    "MD": "(MD)" + CK + C4 + C4 + C4 + C4 + C4,
    # Monaco    (10n,11c,2n)  MCkk bbbb bsss sscc cccc cccc cxx
    "MC": "(MC)" + CK + N4 + N4 + N2 + C2 + C4 + C4 + C + N2,
    # Montenegro    (18n) MEkk bbbc cccc cccc cccc xx
    "ME": "(ME)" + CK + N4 + N4 + N4 + N4 + N2,
    # Netherlands   (4a,10n)  NLkk bbbb cccc cccc cc
    "NL": "(NL)" + CK + A4 + N4 + N4 + N2,
    # North Macedonia   (3n,10c,2n)   MKkk bbbc cccc cccc cxx
    "MK": "(MK)" + CK + N3 + C + C4 + C4 + C + N2,
    # Norway    (11n) NOkk bbbb cccc ccx
    "NO": "(NO)" + CK + N4 + N4 + N3,
    # Pakistan  (4c,16n)  PKkk bbbb cccc cccc cccc cccc
    "PK": "(PK)" + CK + C4 + N4 + N4 + N4 + N4,
    # Palestinian territories   (4c,21n)  PSkk bbbb xxxx xxxx xccc cccc cccc c
    "PS": "(PS)" + CK + C4 + N4 + N4 + N4 + N4 + N,
    # Poland    (24n) PLkk bbbs sssx cccc cccc cccc cccc
    "PL": "(PL)" + CK + N4 + N4 + N4 + N4 + N4 + N4,
    # Portugal  (21n) PTkk bbbb ssss cccc cccc cccx x
    "PT": "(PT)" + CK + N4 + N4 + N4 + N4 + N,
    # Qatar (4a,21c)  QAkk bbbb cccc cccc cccc cccc cccc c
    "QA": "(QA)" + CK + A4 + C4 + C4 + C4 + C4 + C,
    # Romania   (4a,16c)  ROkk bbbb cccc cccc cccc cccc
    "RO": "(RO)" + CK + A4 + C4 + C4 + C4 + C4,
    # San Marino    (1a,10n,12c)  SMkk xbbb bbss sssc cccc cccc ccc
    "SM": "(SM)" + CK + A + N3 + N4 + N3 + C + C4 + C4 + C3,
    # Saudi Arabia  (2n,18c)  SAkk bbcc cccc cccc cccc cccc
    "SA": "(SA)" + CK + N2 + C2 + C4 + C4 + C4 + C4,
    # Serbia    (18n) RSkk bbbc cccc cccc cccc xx
    "RS": "(RS)" + CK + N4 + N4 + N4 + N4 + N2,
    # Slovakia  (20n) SKkk bbbb ssss sscc cccc cccc
    "SK": "(SK)" + CK + N4 + N4 + N4 + N4 + N4,
    # Slovenia  (15n) SIkk bbss sccc cccc cxx
    "SI": "(SI)" + CK + N4 + N4 + N4 + N3,
    # Spain (20n) ESkk bbbb ssss xxcc cccc cccc
    "ES": "(ES)" + CK + N4 + N4 + N4 + N4 + N4,
    # Sweden    (20n) SEkk bbbc cccc cccc cccc cccc
    "SE": "(SE)" + CK + N4 + N4 + N4 + N4 + N4,
    # Switzerland   (5n,12c)  CHkk bbbb bccc cccc cccc c
    "CH": "(CH)" + CK + N4 + N + C3 + C4 + C4 + C,
    # Tunisia   (20n) TNkk bbss sccc cccc cccc cccc
    "TN": "(TN)" + CK + N4 + N4 + N4 + N4 + N4,
    # Turkey    (5n,17c)  TRkk bbbb bxcc cccc cccc cccc cc
    "TR": "(TR)" + CK + N4 + N + C3 + C4 + C4 + C4 + C2,
    # United Arab Emirates  (3n,16n)  AEkk bbbc cccc cccc cccc ccc
    "AE": "(AE)" + CK + N4 + N4 + N4 + N4 + N3,
    # United Kingdom (4a,14n) GBkk bbbb ssss sscc cccc cc
    "GB": "(GB)" + CK + A4 + N4 + N4 + N4 + N2,
    # Vatican City  (3n,15n)  VAkk bbbc cccc cccc cccc cc
    "VA": "(VA)" + CK + N4 + N4 + N4 + N4 + N2,
    # Virgin Islands, British   (4c,16n)  VGkk bbbb cccc cccc cccc cccc
    "VG": "(VG)" + CK + C4 + N4 + N4 + N4 + N4,
    # Egypt (25n) EGkk bbbb ssss cccc cccc cccc ccccc
    "EG": "(EG)" + CK + N4 + N4 + N4 + N4 + N4 + N4 + N,
    # Iraq  (4a,15n)  IQkk bbbb sssc cccc cccc ccc
    "IQ": "(IQ)" + CK + A4 + N4 + N4 + N4 + N3,
    # Libya (21n) LYkk bbbs ssss cccc cccc c
    "LY": "(LY)" + CK + N4 + N4 + N4 + N4 + N4 + N,
    # Saint Lucia   (4a,24c)  LCkk bbbb cccc cccc cccc cccc cccc cccc
    "LC": "(LC)" + CK + A4 + C4 + C4 + C4 + C4 + C4 + C4,
    # Seychelles    (4a,20n,3a)   SCkk bbbb ssnn cccc cccc cccc cccm mmm
    "SC": "(SC)" + CK + A4 + N4 + N4 + N4 + N4 + N4 + A3,
    # Ukraine   (6n,19c)  UAkk bbbb bbcc cccc cccc cccc cccc c
    "UA": "(UA)" + CK + N4 + N2 + C4 + C4 + C4 + C4 + C3,
}
