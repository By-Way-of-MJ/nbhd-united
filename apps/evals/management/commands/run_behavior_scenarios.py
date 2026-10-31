"""Drive chosen behavior scenarios once, on demand, and print one line each.

    manage.py run_behavior_scenarios timezone_move_after_yes

Runs the REAL suite (``run_behavior_suite``) against the configured behavior
tenant's container for exactly the named scenarios, in the given order, with
``trigger=manual``. It deliberately does NOT move the persisted rotation cursor,
so a hand-picked fire never perturbs the scheduled rotation.

Output is one line per scenario — ``<id> PASS|FAIL <assertion>:<detail>`` — and
NEVER the reply text (INVARIANT #1: reply content is not an operator-facing
artifact). ``<detail>`` is the hard assertion's machine code (e.g.
``timezone_is:user_tz=Asia/Tokyo``), so a red run says which check failed and
what the DB actually held.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.evals.behavior.schema import load_all_scenarios
from apps.evals.behavior.targets import BehaviorConfigError
from apps.evals.models import EvalRun
from apps.evals.suites.behavior import run_behavior_suite


class Command(BaseCommand):
    help = "Run only the named behavior scenarios once (no rotation-cursor advance)."

    def add_arguments(self, parser):
        parser.add_argument(
            "scenario_ids",
            nargs="+",
            metavar="scenario_id",
            help="Behavior scenario id(s) to drive, in order (e.g. timezone_move_after_yes).",
        )

    def handle(self, *args, **options):
        requested = list(dict.fromkeys(options["scenario_ids"]))
        known = {scenario.id for scenario in load_all_scenarios()}
        unknown = [scenario_id for scenario_id in requested if scenario_id not in known]
        if unknown:
            raise CommandError(
                f"unknown behavior scenario id(s): {', '.join(unknown)}. Known ids: {', '.join(sorted(known))}"
            )

        try:
            run = run_behavior_suite(only=requested, trigger=EvalRun.Trigger.MANUAL)
        except BehaviorConfigError as exc:
            # Misconfiguration (tenant/transport/pre-warm) — fail loudly, non-zero.
            raise CommandError(f"behavior run could not start: {exc}") from exc

        results = list(run.results.all())
        for scenario_id in requested:
            rows = [r for r in results if r.case_id.startswith(f"{scenario_id}::")]
            hard = [r for r in rows if r.details.get("kind") == "hard"]
            failed = [r for r in hard if not r.passed]
            if failed:
                row = failed[0]
                status = "FAIL"
            elif hard:
                row = hard[0]
                status = "PASS"
            else:
                # The suite recorded no hard row for a scenario we asked it to drive
                # (e.g. a skip row) — never print a silent green.
                skipped = next((r for r in rows if r.details.get("kind") == "skipped"), None)
                reason = skipped.details.get("reason", "no_result") if skipped else "no_result"
                self.stdout.write(f"{scenario_id} FAIL skipped:{reason}")
                continue
            assertion = row.details.get("assertion", "")
            code = row.details.get("code", "")
            self.stdout.write(f"{scenario_id} {status} {assertion}:{code}")
