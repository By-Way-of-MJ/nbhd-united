"""Live synthetic evaluation of the production background routing question."""

import json
from collections import Counter
from pathlib import Path
from statistics import median
from time import monotonic
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings
from pydantic import TypeAdapter

from apps.common import jev
from apps.router.background_route import Decision, route_background

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/jev_background.json"


class Case(jev.StrictModel):
    language: Literal["en", "ja"]
    text: str
    expected: Decision
    has_attachment: bool = False


class Command(BaseCommand):
    help = "Run 30 synthetic prompts through live Jev background routing (network; no tenant records)."
    requires_system_checks = []

    def handle(self, *args, **options):
        if not settings.OPENROUTER_API_KEY:
            raise CommandError("OPENROUTER_API_KEY is required for the live eval")
        cases = TypeAdapter(list[Case]).validate_json(FIXTURE.read_text())
        tenant = SimpleNamespace(
            id=uuid4(), model_tier="starter", pii_entity_map={}, pii_type_counters={}, pii_denylist={}
        )
        # Warm real redaction before timing, never invent a confirmation.
        from apps.pii.ephemeral import redact_texts_ephemeral_checked

        try:
            warm = redact_texts_ephemeral_checked(["Hello"], tenant, deadline=monotonic() + 60)
            confirmed = bool(warm and warm[0].confirmed)
        except Exception:
            confirmed = False
        self.stdout.write(json.dumps({"redactor_warmup_confirmed": confirmed}))
        confusion = {expected: dict.fromkeys(("background", "inline"), 0) for expected in ("background", "inline")}
        latencies, reasons = [], Counter()
        correct = usable = 0
        with override_settings(SUBAGENT_TENANT_IDS=str(tenant.id)):
            for index, case in enumerate(cases, 1):
                result = route_background(case.text, tenant, has_attachment=case.has_attachment)
                # Do not let unavailable/redaction-failed inline fallbacks
                # inflate accuracy. Confusion still reports effective routing.
                evaluated = result.reason in {"ok", "low_confidence", "attachment"}
                correct += evaluated and result.decision == case.expected
                usable += evaluated
                confusion[case.expected][result.decision] += 1
                latencies.append(result.latency_ms)
                reasons[result.reason] += 1
                self.stdout.write(
                    json.dumps(
                        {"case": index, "language": case.language, "expected": case.expected, **result.model_dump()}
                    )
                )
        self.stdout.write(
            json.dumps(
                {
                    "cases": len(cases),
                    "accuracy": correct / len(cases),
                    "evaluated": usable,
                    "confusion_expected_actual": confusion,
                    "p50_latency_ms": median(latencies),
                    "reasons": reasons,
                }
            )
        )
