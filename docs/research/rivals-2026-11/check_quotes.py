#!/usr/bin/env python3
"""Verify every blockquote in RIVALS.md is a verbatim substring of the raw review."""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
raw = {r["review_id"]: r for r in json.load(open(HERE / "raw-low-reviews.json", encoding="utf-8"))}
lines = (HERE / "RIVALS.md").read_text(encoding="utf-8").splitlines()

quotes = []
for i, line in enumerate(lines):
    if not line.startswith("> ") or line.startswith("> —"):
        continue
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    m = re.search(r"review_id `(\d+)`", nxt)
    if m:
        quotes.append((line[2:], m.group(1)))

failures = 0
for n, (quote, rid) in enumerate(quotes, 1):
    review = raw.get(rid)
    if review and quote in review["text"]:
        print(f"PASS {n:02d}/{len(quotes):02d}  {review['app_name']}  review_id {rid}")
    else:
        failures += 1
        print(f"FAIL {n:02d}/{len(quotes):02d}  review_id {rid}  quote={quote!r}")

print(f"\n{len(quotes) - failures}/{len(quotes)} PASS, {failures} FAIL")
sys.exit(1 if failures else 0)
