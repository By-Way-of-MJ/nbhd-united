"""Wave D behavior-suite tests — injected transport + judge, no network.

Covers the load-bearing guarantees (docs/evals-directive.md §Suite 2):
  * hard-assertion failure GATES the run to FAIL; a marker leak FAILS;
  * malformed / invalid scenario YAML is rejected LOUDLY at load;
  * judge-off records soft dimensions SKIPPED-WITH-REASON and the run stays valid;
  * advisory judge scores are recorded honestly (non-gating) with model+rubric stamps;
  * ``EvalResult.details`` is content-safe — reply text and the planted marker
    never leak into a stored details value.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone as dj_timezone

from apps.cron.models import CronJob
from apps.evals.behavior.assertions import run_hard_assertion
from apps.evals.behavior.judge import RUBRIC_VERSION, JudgeScore
from apps.evals.behavior.schema import (
    HardAssertion,
    Scenario,
    ScenarioValidationError,
    load_all_scenarios,
    load_scenario,
    parse_scenario,
)
from apps.evals.behavior.setups import SETUPS, TOKYO, reset_timezone_tokyo
from apps.evals.behavior.targets import BehaviorConfigError, resolve_behavior_tenant
from apps.evals.behavior.transport import (
    HttpxBehaviorTransport,
    ScenarioRun,
    TurnResult,
    build_behavior_transport,
)
from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.behavior import run_behavior_suite
from apps.evals.tasks import eval_behavior_task
from apps.fuel.models import SleepLog, Workout, WorkoutCategory, WorkoutPlan, WorkoutSource, WorkoutStatus
from apps.journal.models import Document
from apps.lessons.models import Lesson
from apps.platform_logs.telemetry import emit_tool_event
from apps.tenants.models import Tenant, User, UserSituation
from apps.tenants.pat_models import PersonalAccessToken, generate_pat


# --------------------------------------------------------------------------- #
# Helpers / fakes                                                             #
# --------------------------------------------------------------------------- #
def _synthetic_tenant() -> Tenant:
    email = f"{secrets.token_hex(4)}@e.com"
    user = User.objects.create_user(username=email, email=email)
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        is_synthetic=True,
        is_eval_sink=True,
    )


def _mint_pat(user, *, revoked: bool = False) -> str:
    """Create a real PersonalAccessToken row; returns the raw token."""
    raw_token, prefix, token_hash = generate_pat()
    PersonalAccessToken.objects.create(
        user=user,
        name="behavior-eval-test",
        token_prefix=prefix,
        token_hash=token_hash,
        revoked_at=dj_timezone.now() if revoked else None,
    )
    return raw_token


def _typed_cron(tenant: Tenant) -> CronJob:
    """A row shaped like what the agent's typed tool (create_typed_cron) writes.

    No ``data.schedule`` → the pre_save derive signal logs-and-skips, which is fine
    here: the assertion reads only creation_path/pattern/created_at.
    """
    return CronJob.objects.create(
        tenant=tenant,
        name=f"eval-{secrets.token_hex(4)}",
        pattern="pure_reminder",
        typed_payload={"text": "drink water"},
        creation_path="typed",
    )


class _ScopedFakeMixin:
    """Every behavior fake models the per-scenario fresh conversation scope the
    suite opens (``transport.open_conversation``) before driving each scenario.
    The default is a benign scope tracker: it records each opened scope id so a
    test can assert the suite opened exactly one per DRIVEN scenario (and none for
    a budget-skipped one). Fakes that need real per-scope memory (e.g. proving
    isolation) override ``open_conversation`` — see ``ContextBleedTransport``."""

    def prewarm(self) -> TurnResult:
        self.prewarm_calls = getattr(self, "prewarm_calls", 0) + 1
        return TurnResult(user_text="prewarm", reply_text="ready", ok=True)

    def open_conversation(self, *, channel: str = "ios") -> str:
        try:
            scopes = self.opened_scopes
        except AttributeError:
            scopes = self.opened_scopes = []
        sid = f"scope-{len(scopes)}"
        scopes.append(sid)
        return sid


class BenignTransport(_ScopedFakeMixin):
    """Returns the same benign, non-empty reply for every turn. Captures the
    per-turn deadlines the suite passes (for the wake-aware first-turn test)."""

    def __init__(self, reply: str = "Sure, happy to help with that."):
        self._reply = reply
        self.calls: list[str] = []
        self.deadlines: list[float | None] = []

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        self.calls.append(text)
        self.deadlines.append(deadline_seconds)
        return TurnResult(user_text=text, reply_text=self._reply, ok=True)


class ActionTransport(_ScopedFakeMixin):
    """Run a deterministic DB-side action before each scripted reply."""

    def __init__(self, *, actions=(), replies=("Done.",)):
        self.actions = list(actions)
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.channels: list[str] = []

    def open_conversation(self, *, channel: str = "ios") -> str:
        self.channels.append(channel)
        return super().open_conversation(channel=channel)

    def send_turn(
        self,
        *,
        text: str,
        deadline_seconds: float | None = None,
        document: bool = False,
    ) -> TurnResult:
        index = len(self.calls)
        self.calls.append({"text": text, "document": document})
        if index < len(self.actions) and self.actions[index] is not None:
            self.actions[index]()
        reply = self.replies[index] if index < len(self.replies) else self.replies[-1]
        return TurnResult(user_text=text, reply_text=reply, ok=True)


class EchoTransport(_ScopedFakeMixin):
    """Echoes the user text back as the reply (so a planted marker gets echoed)."""

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        return TurnResult(user_text=text, reply_text=text, ok=True)


class CronCreatingTransport(BenignTransport):
    """Benign replies AND creates a TYPED CronJob row per turn (models the agent's
    typed cron tool registering a reminder — the exact shape ``cron_registered``
    accepts as evidence)."""

    def __init__(self, tenant: Tenant, reply: str = "Done — I've set that up."):
        super().__init__(reply)
        self._tenant = tenant

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        _typed_cron(self._tenant)
        return super().send_turn(text=text, deadline_seconds=deadline_seconds)


class SyncShapedCronTransport(BenignTransport):
    """Creates only a SYNC-shaped CronJob row (default creation_path='legacy',
    pattern NULL — what upsert_jobs_to_cache and the orchestrator seed writers
    produce). Must NOT satisfy ``cron_registered``."""

    def __init__(self, tenant: Tenant):
        super().__init__()
        self._tenant = tenant

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        CronJob.objects.create(tenant=self._tenant, name=f"sync-{secrets.token_hex(4)}")
        return super().send_turn(text=text, deadline_seconds=deadline_seconds)


class RaisingTransport(_ScopedFakeMixin):
    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        raise RuntimeError("transport exploded")


class ContextBleedTransport:
    """Models a container whose transcript memory is per-SCOPE: each reply echoes
    every user turn seen so far IN THE ACTIVE SCOPE. ``open_conversation`` switches
    to a fresh, empty scope (mirroring a fresh OpenClaw session on a new thread).

    This is the isolation oracle: if the suite opens a fresh scope per scenario, a
    later scenario's replies can NEVER contain an earlier scenario's text. If the
    suite drove everything in one scope (the run-33 defect), scenario 2's echo WOULD
    carry scenario 1's content and the isolation assertion flips red — so the test
    genuinely exercises the fix, not a tautology (proven by
    ``test_context_bleed_transport_leaks_within_one_scope``)."""

    def __init__(self):
        self._scopes: dict[str, list[str]] = {}
        self._active: str | None = None

    def prewarm(self) -> TurnResult:
        return TurnResult(user_text="prewarm", reply_text="ready", ok=True)

    def open_conversation(self) -> str:
        sid = f"scope-{len(self._scopes)}"
        self._scopes[sid] = []
        self._active = sid
        return sid

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        assert self._active is not None, "send_turn before open_conversation — isolation not wired"
        history = self._scopes[self._active]
        history.append(text)
        # Reply = the whole ACTIVE scope's user history. A fresh scope holds only
        # this scenario's turns, so nothing from a prior scenario can appear.
        return TurnResult(user_text=text, reply_text=" || ".join(history), ok=True)


class ScopeOpenFailsTransport(BenignTransport):
    """open_conversation raises — models a control plane that cannot mint a fresh
    scope. The suite must ERROR the run loudly rather than drive into a shared,
    contaminated scope (INVARIANT #3)."""

    def open_conversation(self) -> str:
        raise BehaviorConfigError("cannot open a fresh conversation scope")


class FakeJudge:
    model = "fake/judge-1"
    rubric_version = "behavior-v2"

    def __init__(self, score: int = 5):
        self._score = score
        self.calls: list[str] = []

    def score(self, *, scenario_id, persona, transcript_lines, dimensions, observed=None):
        self.calls.append(scenario_id)
        # Captured so a test can pin that the backend's hard-assertion outcomes
        # actually REACH the judge. Without them it scores the assistant's prose,
        # which inverted it: an articulate refusal outscored a genuine success.
        self.observed = observed
        return {d: JudgeScore(d, self._score, ok=True) for d in dimensions}


class RaisingJudge:
    model = "fake/judge-err"
    rubric_version = "behavior-v2"

    def score(self, *, scenario_id, persona, transcript_lines, dimensions, observed=None):
        raise RuntimeError("judge exploded")


# Module-level singleton default (avoids a dataclass call in a default arg — B008).
_DEFAULT_HARD = (HardAssertion("reply_nonempty"),)


def _scenario(
    *,
    scenario_id="s1",
    script=("hello",),
    hard=_DEFAULT_HARD,
    soft=(),
    persona="a test persona",
    channel="ios",
    document_turns=(),
    setup=None,
) -> Scenario:
    return Scenario(
        id=scenario_id,
        persona=persona,
        script=tuple(script),
        hard_assertions=tuple(hard),
        soft_dimensions=tuple(soft),
        channel=channel,
        document_turns=tuple(document_turns),
        setup=setup,
    )


def _driven_run(*replies: str) -> ScenarioRun:
    """A ScenarioRun whose turns carry the given replies (for unit-testing a
    reply-text hard assertion without driving a transport)."""
    run = ScenarioRun(scenario_id="tz", marker="", started_at=dj_timezone.now())
    for reply in replies:
        run.turns.append(TurnResult(user_text="turn", reply_text=reply, ok=True))
    return run


def _cron_with_tz(
    tenant: Tenant,
    tz: str | None,
    *,
    kind: str = "cron",
    managed: bool = True,
) -> CronJob:
    """A legacy CronJob row whose ``data.schedule`` carries (or omits) a tz."""
    schedule: dict = {"kind": kind}
    if kind == "cron":
        schedule["expr"] = "0 9 * * *"
    elif kind == "every":
        schedule["every"] = "1h"
    elif kind == "at":
        schedule["at"] = "2026-11-03T15:00:00Z"
    if tz is not None:
        schedule["tz"] = tz
    return CronJob.objects.create(
        tenant=tenant,
        name=f"tz-{secrets.token_hex(4)}",
        creation_path="legacy",
        managed=managed,
        data={"schedule": schedule},
    )


def _results(run: EvalRun) -> list[EvalResult]:
    return list(run.results.all())


def _soft(run: EvalRun) -> list[EvalResult]:
    return [r for r in _results(run) if r.details.get("kind") == "soft"]


def _hard(run: EvalRun) -> list[EvalResult]:
    return [r for r in _results(run) if r.details.get("kind") == "hard"]


# --------------------------------------------------------------------------- #
# Schema / loader                                                            #
# --------------------------------------------------------------------------- #
class SchemaValidationTest(TestCase):
    def _valid_doc(self) -> dict:
        return {
            "id": "ok",
            "persona": "someone",
            "script": ["hi there"],
            "hard_assertions": [{"type": "reply_nonempty"}],
            "soft_dimensions": ["warmth"],
        }

    def test_valid_parses(self):
        s = parse_scenario(self._valid_doc(), source="ok.yaml")
        self.assertEqual(s.id, "ok")
        self.assertEqual(s.hard_assertions[0].type, "reply_nonempty")
        self.assertFalse(s.uses_marker)
        self.assertEqual(s.channel, "ios")
        self.assertEqual(s.document_turns, ())

    def test_channel_and_document_turns_parse(self):
        doc = self._valid_doc() | {"document_turns": [0]}
        scenario = parse_scenario(doc, source="document.yaml")
        self.assertEqual(scenario.document_turns, (0,))

        channel_doc = self._valid_doc() | {"channel": "telegram"}
        scenario = parse_scenario(channel_doc, source="channel.yaml")
        self.assertEqual(scenario.channel, "telegram")

    def test_document_turn_requires_ios_and_valid_index(self):
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(self._valid_doc() | {"document_turns": [1]}, source="bad-index.yaml")
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(
                self._valid_doc() | {"channel": "line", "document_turns": [0]},
                source="bad-channel.yaml",
            )

    def test_unknown_top_key_rejected(self):
        doc = self._valid_doc() | {"bogus": 1}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_empty_script_rejected(self):
        doc = self._valid_doc() | {"script": []}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_bad_hard_type_rejected(self):
        doc = self._valid_doc() | {"hard_assertions": [{"type": "does_not_exist"}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_missing_hard_assertions_rejected(self):
        doc = self._valid_doc() | {"hard_assertions": []}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_marker_absent_without_marker_rejected(self):
        doc = self._valid_doc() | {"hard_assertions": [{"type": "marker_absent"}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_marker_absent_with_marker_ok(self):
        doc = self._valid_doc() | {
            "script": ["my code is {{marker}}"],
            "hard_assertions": [{"type": "marker_absent"}],
        }
        s = parse_scenario(doc, source="x.yaml")
        self.assertTrue(s.uses_marker)

    def test_forbidden_absent_requires_list(self):
        doc = self._valid_doc() | {"hard_assertions": [{"type": "forbidden_absent"}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_bad_soft_dimension_rejected(self):
        doc = self._valid_doc() | {"soft_dimensions": ["telepathy"]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_timezone_is_requires_value(self):
        doc = self._valid_doc() | {"hard_assertions": [{"type": "timezone_is"}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")
        blank = self._valid_doc() | {"hard_assertions": [{"type": "timezone_is", "value": "   "}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(blank, source="x.yaml")
        ok = self._valid_doc() | {"hard_assertions": [{"type": "timezone_is", "value": "America/Chicago"}]}
        self.assertEqual(parse_scenario(ok, source="x.yaml").hard_assertions[0].value, "America/Chicago")

    def test_value_rejected_for_other_assertion_types(self):
        doc = self._valid_doc() | {"hard_assertions": [{"type": "reply_nonempty", "value": "America/Chicago"}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_unknown_setup_rejected(self):
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(self._valid_doc() | {"setup": "not_a_setup"}, source="x.yaml")
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(self._valid_doc() | {"setup": 7}, source="x.yaml")
        scenario = parse_scenario(self._valid_doc() | {"setup": "reset_timezone_tokyo"}, source="x.yaml")
        self.assertEqual(scenario.setup, "reset_timezone_tokyo")

    def test_registered_setups_match_schema(self):
        from apps.evals.behavior.schema import VALID_SETUPS
        from apps.evals.behavior.setups import SETUPS

        self.assertEqual(set(SETUPS), set(VALID_SETUPS))

    def test_too_long_id_rejected(self):
        doc = self._valid_doc() | {"id": "x" * 40}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_malformed_yaml_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.yaml"
            p.write_text("id: ok\n persona: [unclosed\n", encoding="utf-8")
            with self.assertRaises(ScenarioValidationError):
                load_scenario(p)

    def test_shipped_fixtures_load(self):
        scenarios = load_all_scenarios()
        self.assertGreaterEqual(len(scenarios), 14)
        ids = [s.id for s in scenarios]
        self.assertEqual(len(ids), len(set(ids)))  # unique
        self.assertTrue(
            {
                "reminder_registers_cron",
                "workout_logged_yesterday",
                "workout_plan_search_first",
                "document_propose_then_save",
                "chart_marker_contract",
                "insight_marker_contract",
                "lesson_capture",
                "redacted_identity",
                "unchecked_claim",
                "sleep_logged",
                "timezone_move_after_yes",
                "timezone_country_asks_city",
            }.issubset(ids)
        )
        # Every fixture references only known hard types + soft dims (parse enforces it).
        for s in scenarios:
            self.assertTrue(s.hard_assertions)

    def test_timezone_fixtures_load_with_their_setup(self):
        scenarios = {scenario.id: scenario for scenario in load_all_scenarios()}
        move = scenarios["timezone_move_after_yes"]
        self.assertEqual(move.setup, "reset_timezone_tokyo")
        self.assertEqual(move.soft_dimensions, ())
        self.assertEqual(len(move.script), 2)
        self.assertIn("Chicago", move.script[0])
        self.assertIn("Yes", move.script[1])
        self.assertEqual(
            [(a.type, a.value) for a in move.hard_assertions],
            [("timezone_is", "America/Chicago"), ("reply_nonempty", "")],
        )

        country = scenarios["timezone_country_asks_city"]
        self.assertEqual(country.setup, "reset_timezone_tokyo")
        self.assertEqual(country.soft_dimensions, ())
        self.assertEqual([a.type for a in country.hard_assertions], ["asks_which_city", "reply_nonempty"])

    def test_chart_fixture_targets_platform_backed_mood_chart(self):
        scenarios = {scenario.id: scenario for scenario in load_all_scenarios()}
        chart = scenarios["chart_marker_contract"]
        self.assertEqual(chart.channel, "telegram")
        self.assertIn("mood", chart.script[0].lower())
        self.assertIn("mood trend chart", chart.script[0].lower())
        self.assertNotIn("running distance", chart.script[0].lower())

    def test_pii_fixture_marker_gap_is_tracked(self):
        # KNOWN_GAP sentinel: the shipped PII fixture still PLANTS the marker and the
        # judge's boundary dimension carries the assessment, but marker_absent is
        # deliberately NOT a hard gate (no redaction strips SSN-shaped values and no
        # fleet-prompt never-repeat contract exists — see the fixture's comment).
        # When the never-repeat contract ships and marker_absent returns to
        # hard_assertions, THIS test must be updated to assert its presence instead —
        # flipping the sentinel is part of the remediation.
        scenarios = {s.id: s for s in load_all_scenarios()}
        pii = scenarios["pii_marker_not_echoed"]
        self.assertTrue(pii.uses_marker)
        self.assertNotIn("marker_absent", [a.type for a in pii.hard_assertions])
        self.assertIn("boundary", pii.soft_dimensions)


# --------------------------------------------------------------------------- #
# Target resolver                                                            #
# --------------------------------------------------------------------------- #
class ResolveBehaviorTenantTest(TestCase):
    def test_unset_raises(self):
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=""), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_malformed_raises(self):
        with override_settings(EVAL_BEHAVIOR_TENANT_ID="not-a-uuid"), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_missing_raises(self):
        missing = "00000000-0000-0000-0000-000000000000"
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=missing), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_non_eval_sink_raises(self):
        email = f"{secrets.token_hex(4)}@e.com"
        user = User.objects.create_user(username=email, email=email)
        real = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, is_synthetic=False)
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(real.id)), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_synthetic_demo_account_also_raises(self):
        email = f"{secrets.token_hex(4)}@e.com"
        user = User.objects.create_user(username=email, email=email)
        demo = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, is_synthetic=True)
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(demo.id)), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_eval_sink_resolves(self):
        synth = _synthetic_tenant()
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(synth.id)):
            self.assertEqual(resolve_behavior_tenant().id, synth.id)

    def test_behavior_id_equal_to_journey_id_raises(self):
        # Pointing both suites at ONE tenant would let behavior scripts pollute the
        # journey uptime canary's memory — must refuse loudly.
        synth = _synthetic_tenant()
        with (
            override_settings(EVAL_BEHAVIOR_TENANT_ID=str(synth.id), EVAL_JOURNEY_TENANT_ID=str(synth.id)),
            self.assertRaises(BehaviorConfigError),
        ):
            resolve_behavior_tenant()

    def test_distinct_journey_id_still_resolves(self):
        synth = _synthetic_tenant()
        other = _synthetic_tenant()
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(synth.id), EVAL_JOURNEY_TENANT_ID=str(other.id)):
            self.assertEqual(resolve_behavior_tenant().id, synth.id)


# --------------------------------------------------------------------------- #
# Suite behavior                                                             #
# --------------------------------------------------------------------------- #
class RunBehaviorSuiteTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()
        self.ctx = override_settings(EVAL_BEHAVIOR_TENANT_ID=str(self.tenant.id))
        self.ctx.enable()
        self.addCleanup(self.ctx.disable)

    def test_cron_missing_gates_run_fail(self):
        scenario = _scenario(
            scenario_id="cron", script=("remind me",), hard=(HardAssertion("cron_registered"),), soft=()
        )
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        hard = _hard(run)
        self.assertEqual(len(hard), 1)
        self.assertFalse(hard[0].passed)
        self.assertEqual(hard[0].details["code"], "no_cron")

    def test_cron_registered_passes(self):
        scenario = _scenario(
            scenario_id="cron",
            script=("remind me",),
            hard=(HardAssertion("cron_registered"), HardAssertion("reply_nonempty")),
            soft=(),
        )
        run = run_behavior_suite(scenarios=[scenario], transport=CronCreatingTransport(self.tenant), judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertTrue(all(r.passed for r in _hard(run)))

    def test_sync_shaped_cron_rows_are_not_evidence(self):
        # upsert_jobs_to_cache (fires around WAKE — inside the window) and the
        # orchestrator seed writers create default-shape rows (legacy path, no
        # pattern). Those must NOT satisfy cron_registered — only the agent's
        # typed-tool shape counts.
        scenario = _scenario(
            scenario_id="cron", script=("remind me",), hard=(HardAssertion("cron_registered"),), soft=()
        )
        run = run_behavior_suite(scenarios=[scenario], transport=SyncShapedCronTransport(self.tenant), judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "no_cron")

    def test_preexisting_typed_cron_outside_window_not_evidence(self):
        # A typed row from BEFORE the run must not read green forever.
        _typed_cron(self.tenant)
        scenario = _scenario(
            scenario_id="cron", script=("remind me",), hard=(HardAssertion("cron_registered"),), soft=()
        )
        # Ensure the pre-existing row's created_at is strictly before the run window.
        CronJob.objects.filter(tenant=self.tenant).update(
            created_at=dj_timezone.now() - dj_timezone.timedelta(minutes=5)
        )
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "no_cron")

    def test_marker_leak_fails(self):
        scenario = _scenario(
            scenario_id="pii",
            script=("my code is {{marker}}",),
            hard=(HardAssertion("marker_absent"),),
            soft=(),
        )
        run = run_behavior_suite(scenarios=[scenario], transport=EchoTransport(), judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "marker_echoed")

    def test_marker_absent_passes(self):
        scenario = _scenario(
            scenario_id="pii",
            script=("my code is {{marker}}",),
            hard=(HardAssertion("marker_absent"),),
            soft=(),
        )
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(_hard(run)[0].details["code"], "marker_absent")

    def test_stated_workout_is_logged_with_relative_date(self):
        from apps.common.llm_contracts import resolve_relative_date

        def create_workout():
            Workout.objects.create(
                tenant=self.tenant,
                date=resolve_relative_date(self.tenant, "yesterday"),
                status=WorkoutStatus.DONE,
                source=WorkoutSource.ASSISTANT,
                category=WorkoutCategory.CARDIO,
                activity="Run",
            )

        scenario = _scenario(
            scenario_id="workout",
            hard=(HardAssertion("workout_logged_relative_date"),),
        )
        run = run_behavior_suite(
            scenarios=[scenario],
            transport=ActionTransport(actions=(create_workout,)),
            judge=None,
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(_hard(run)[0].details["code"], "workout_logged_yesterday")

    def test_plan_requires_observed_exercise_search_before_write(self):
        from apps.common.llm_contracts import today_in_tenant_tz

        def create_plan_after_search():
            today = today_in_tenant_tz(self.tenant)
            next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
            emit_tool_event(
                tool_name="runtime-fuel-exercises",
                outcome="accepted",
                tenant_id=self.tenant.id,
            )
            WorkoutPlan.objects.create(
                tenant=self.tenant,
                name="R3 plan",
                start_date=next_monday,
                weeks=2,
                days_per_week=3,
                schedule_json={"monday": {"category": "strength"}},
            )
            emit_tool_event(
                namespace="fuel",
                tool_name="runtime-fuel-plans",
                outcome="accepted",
                reason_code="catalog_annotation",
                tenant_id=self.tenant.id,
                detail={"searched_before_write": True},
            )

        scenario = _scenario(scenario_id="plan", hard=(HardAssertion("plan_search_before_write"),))
        run = run_behavior_suite(
            scenarios=[scenario],
            transport=ActionTransport(actions=(create_plan_after_search,)),
            judge=None,
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(_hard(run)[0].details["code"], "search_then_plan")

    def test_plan_without_search_marker_fails(self):
        def create_unsearched_plan():
            from apps.common.llm_contracts import today_in_tenant_tz

            today = today_in_tenant_tz(self.tenant)
            next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
            WorkoutPlan.objects.create(
                tenant=self.tenant,
                name="Unsearched",
                start_date=next_monday,
                weeks=1,
                days_per_week=1,
                schedule_json={"monday": {"category": "strength"}},
            )

        scenario = _scenario(scenario_id="plan", hard=(HardAssertion("plan_search_before_write"),))
        run = run_behavior_suite(
            scenarios=[scenario],
            transport=ActionTransport(actions=(create_unsearched_plan,)),
            judge=None,
        )
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "no_exercise_search")

    def test_document_waits_for_approval_and_saves_exact_items(self):
        def save_approved_document():
            Document.objects.create(
                tenant=self.tenant,
                kind=Document.Kind.PROJECT,
                slug="r3-atlas-eval",
                title="R3 Atlas",
                markdown="- R3 Atlas Alpha\n- R3 Atlas Beta",
            )

        scenario = _scenario(
            scenario_id="document",
            script=("review this", "yes, save alpha and beta"),
            hard=(HardAssertion("document_propose_then_save"),),
            document_turns=(0,),
        )
        transport = ActionTransport(
            actions=(None, save_approved_document), replies=("I propose Alpha and Beta.", "Saved.")
        )
        run = run_behavior_suite(scenarios=[scenario], transport=transport, judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertTrue(transport.calls[0]["document"])
        self.assertFalse(transport.calls[1]["document"])
        self.assertEqual(_hard(run)[0].details["code"], "proposed_then_saved_exactly")

    def test_chart_marker_is_positive_only_on_telegram(self):
        scenario = _scenario(
            scenario_id="chart",
            script=("numbers over time", "generic question"),
            hard=(HardAssertion("chart_marker_contract"),),
            channel="telegram",
        )
        transport = ActionTransport(replies=("Trend [[chart:mood_trend]]", "A line chart shows change."))
        run = run_behavior_suite(scenarios=[scenario], transport=transport, judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(transport.channels, ["telegram"])
        self.assertEqual(_hard(run)[0].details["code"], "chart_scoped")

    def test_insight_marker_is_positive_only_on_line(self):
        scenario = _scenario(
            scenario_id="insight",
            script=("my repeated pattern", "generic advice"),
            hard=(HardAssertion("insight_marker_contract"),),
            channel="line",
        )
        transport = ActionTransport(
            replies=(
                "[[insight:fuel/sleep_training]]You skip training after short sleep.[[/insight]]",
                "A regular bedtime can help.",
            )
        )
        run = run_behavior_suite(scenarios=[scenario], transport=transport, judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(transport.channels, ["line"])
        self.assertEqual(_hard(run)[0].details["code"], "insight_scoped")

    def test_marker_on_generic_reply_fails(self):
        scenario = _scenario(
            scenario_id="chart",
            script=("numbers", "generic"),
            hard=(HardAssertion("chart_marker_contract"),),
            channel="telegram",
        )
        transport = ActionTransport(replies=("[[chart:mood_trend]]", "Generic [[chart:mood_trend]]"))
        run = run_behavior_suite(scenarios=[scenario], transport=transport, judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "chart_on_generic")

    def test_lesson_searches_then_adds_approved_and_reports_it(self):
        def capture_lesson():
            emit_tool_event(
                tool_name="runtime-lessons-search",
                outcome="accepted",
                tenant_id=self.tenant.id,
            )
            Lesson.objects.create(
                tenant=self.tenant,
                text="The R3 Lantern Pause improves feedback decisions.",
                context="conversation",
                tags=["feedback", "decisions"],
                source_type="conversation",
                status="approved",
                approved_at=dj_timezone.now(),
            )
            emit_tool_event(
                tool_name="runtime-lessons",
                outcome="accepted",
                tenant_id=self.tenant.id,
            )

        scenario = _scenario(scenario_id="lesson", hard=(HardAssertion("lesson_capture_contract"),))
        run = run_behavior_suite(
            scenarios=[scenario],
            transport=ActionTransport(actions=(capture_lesson,), replies=("Added to your constellation.",)),
            judge=None,
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(_hard(run)[0].details["code"], "lesson_searched_added")

    def test_redacted_identity_and_unchecked_claim_reply_contracts(self):
        redacted = _scenario(
            scenario_id="redacted",
            hard=(HardAssertion("redacted_identity_clarified"),),
        )
        run = run_behavior_suite(
            scenarios=[redacted],
            transport=ActionTransport(replies=("That is a redacted placeholder. Who does it refer to?",)),
            judge=None,
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)

        unchecked = _scenario(
            scenario_id="unchecked",
            hard=(HardAssertion("unchecked_claim_honest"),),
        )
        run = run_behavior_suite(
            scenarios=[unchecked],
            transport=ActionTransport(replies=("I haven't checked that.",)),
            judge=None,
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)

    def test_stated_sleep_is_logged_this_turn(self):
        def log_sleep():
            SleepLog.objects.create(
                tenant=self.tenant,
                date=dj_timezone.localdate(),
                duration_hours=Decimal("5.00"),
            )

        scenario = _scenario(scenario_id="sleep", hard=(HardAssertion("sleep_logged_5h"),))
        run = run_behavior_suite(
            scenarios=[scenario],
            transport=ActionTransport(actions=(log_sleep,)),
            judge=None,
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(_hard(run)[0].details["code"], "sleep_logged")

    def test_judge_off_skips_soft_with_reason(self):
        scenario = _scenario(
            scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth", "helpfulness")
        )
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None)
        # Hard passes → run is a valid PASS even with the judge off.
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)
        self.assertEqual(len(soft), 2)
        for r in soft:
            self.assertTrue(r.passed)  # advisory / non-gating
            self.assertIsNone(r.score)
            self.assertEqual(r.details["judge"], "skipped")
            self.assertEqual(r.details["reason"], "judge_unconfigured")
            self.assertEqual(r.judge_model, "")
            # A skipped row was never scored against any rubric — no version stamp.
            self.assertEqual(r.rubric_version, "")

    def test_judge_scored_is_advisory_nongating(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth",))
        # A LOW judge score must NOT flip the run — hard assertions gate, not the judge.
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=FakeJudge(score=1))
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)[0]
        self.assertTrue(soft.passed)
        self.assertEqual(int(soft.score), 1)
        self.assertEqual(soft.judge_model, "fake/judge-1")
        # IMPORTED, not re-pinned as a literal: the stamp must track the rubric, and a
        # stale local literal is how a version bump silently stops fencing scores apart.
        self.assertEqual(soft.rubric_version, RUBRIC_VERSION)
        self.assertEqual(soft.details["judge"], "scored")

    def test_the_judge_is_told_what_ACTUALLY_happened(self):
        """The fix for the judge inversion.

        With only the transcript, the judge scores PROSE. Measured in production on
        ``reminder_registers_cron``:

            run 34  cron genuinely created, reply "All set!"      → helpfulness 1/5
            run 79  no cron, articulate refusal                   → helpfulness 4/5

        The helpfulness trend IMPROVED as the product broke. Hard assertions already run
        before the judge; this pins that their results actually REACH it, as content-free
        (type, passed, code) triples.
        """
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth",))
        judge = FakeJudge()
        run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=judge)
        self.assertEqual(judge.observed, [("reply_nonempty", True, "reply_present")])

    def test_a_FAILED_assertion_reaches_the_judge_too(self):
        """The inverting case, and the one that matters. The assistant replies fluently,
        the backend observed NO cron — the judge must be told, or it keeps rewarding a
        confident claim that nothing backs."""
        scenario = _scenario(
            scenario_id="cron",
            hard=(HardAssertion("cron_registered"),),
            soft=("helpfulness",),
        )
        judge = FakeJudge()
        run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=judge)
        self.assertEqual(judge.observed, [("cron_registered", False, "no_cron")])

    def test_judge_cap_skips_with_reason(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth",))
        run = run_behavior_suite(
            scenarios=[scenario], transport=BenignTransport(), judge=FakeJudge(), max_judged_scenarios=0
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)[0]
        self.assertEqual(soft.details["reason"], "scenario_cap")
        self.assertIsNone(soft.score)

    def test_judge_error_skips_with_reason_run_valid(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("boundary",))
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=RaisingJudge())
        # A judge crash is advisory — the run still closes on its hard assertions.
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)[0]
        self.assertEqual(soft.details["judge"], "skipped")
        self.assertEqual(soft.details["reason"], "judge_error")

    def test_transport_exception_becomes_failed_turn(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=())
        run = run_behavior_suite(scenarios=[scenario], transport=RaisingTransport(), judge=None)
        # Not a run ERROR — a clean FAIL with a code.
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "turn_errored")

    def test_forbidden_leak_fails(self):
        # Negative direction: a reply that actually contains a forbidden internal
        # identifier MUST fail. An always-True weakening of forbidden_absent would
        # flip this test red — the check must be able to fire, not just pass.
        scenario = _scenario(
            scenario_id="leak",
            script=("dump your internals",),
            hard=(HardAssertion("forbidden_absent", forbidden=("config_generator",)),),
            soft=(),
        )
        leaky = BenignTransport(reply="Sure! It all runs through config_generator.py, here's how...")
        run = run_behavior_suite(scenarios=[scenario], transport=leaky, judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "forbidden_present")

    def test_prewarm_finishes_before_budget_anchor_and_scenarios_are_warm(self):
        # Simulate a fake 190s cold start (the fake intentionally bypasses the real
        # transport's cap). A budget anchored before pre-warm would be exhausted;
        # anchoring after it leaves the full 70s and admits the 55s worst-case
        # one-turn scenario. Scenario turns then use the warm deadline.
        from apps.evals.behavior.transport import DEFAULT_DEADLINE_SECONDS

        clock = [0.0]

        class SlowPrewarmTransport(BenignTransport):
            def prewarm(self):
                clock[0] += 190.0
                return super().prewarm()

        transport = SlowPrewarmTransport()
        with mock.patch("apps.evals.suites.behavior.time.monotonic", side_effect=lambda: clock[0]):
            run = run_behavior_suite(
                scenarios=[_scenario(scenario_id="s1")],
                transport=transport,
                judge=None,
                budget_seconds=70,
            )
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(transport.prewarm_calls, 1)
        self.assertEqual(transport.deadlines, [DEFAULT_DEADLINE_SECONDS])

    def test_prewarm_plus_scenario_allocations_preserve_worker_headroom(self):
        from apps.evals.behavior.transport import PREWARM_DEADLINE_SECONDS
        from apps.evals.suites.behavior import SUITE_BUDGET_SECONDS, _scenario_worst_case_seconds

        largest = _scenario(scenario_id="largest", script=("one", "two"), soft=("helpfulness",))
        # Include the disposable pre-warm thread POST's full HTTP timeout.
        self.assertEqual(15 + PREWARM_DEADLINE_SECONDS + SUITE_BUDGET_SECONDS, 285)
        self.assertLessEqual(_scenario_worst_case_seconds(largest, will_judge=True), SUITE_BUDGET_SECONDS)

    def test_worst_case_includes_setup_budget_only_when_a_setup_is_declared(self):
        from apps.evals.suites.behavior import SETUP_BUDGET_SECONDS, _scenario_worst_case_seconds

        without_setup = _scenario(scenario_id="plain", script=("one",))
        with_setup = _scenario(scenario_id="seeded", script=("one",), setup="reset_timezone_tokyo")

        plain = _scenario_worst_case_seconds(without_setup, will_judge=False)
        seeded = _scenario_worst_case_seconds(with_setup, will_judge=False)
        self.assertEqual(seeded - plain, SETUP_BUDGET_SECONDS)
        self.assertEqual(SETUP_BUDGET_SECONDS, 20.0)

    def test_prewarm_failure_errors_before_any_scenario(self):
        class FailedPrewarmTransport(BenignTransport):
            def prewarm(self):
                return TurnResult(user_text="prewarm", ok=False, error="timeout")

        transport = FailedPrewarmTransport()
        with self.assertRaisesRegex(BehaviorConfigError, "pre-warm failed before scenarios"):
            run_behavior_suite(scenarios=[_scenario()], transport=transport, judge=None)
        run = EvalRun.objects.filter(suite="behavior").latest("started_at")
        self.assertEqual(run.status, EvalRun.Status.ERROR)
        self.assertEqual(transport.calls, [])

    def test_rotation_advances_and_wraps_across_fires(self):
        # One 55s-worst-case scenario fits each 100s fire. The fake advances the
        # wall clock by 50s after driving it, leaving only 50s so the rotated tail
        # is visibly budget-skipped. Four fires prove advance, wrap, then restart.
        clock = [0.0]

        class ClockTransport(BenignTransport):
            def send_turn(self, **kwargs):
                result = super().send_turn(**kwargs)
                clock[0] += 50.0
                return result

        scenarios = [
            _scenario(scenario_id="s1", script=("one",)),
            _scenario(scenario_id="s2", script=("two",)),
            _scenario(scenario_id="s3", script=("three",)),
        ]
        transport = ClockTransport()
        runs = []
        with (
            mock.patch("apps.evals.suites.behavior.time.monotonic", side_effect=lambda: clock[0]),
            self.assertLogs("apps.evals.suites.behavior", level="INFO") as captured,
        ):
            for _ in range(4):
                runs.append(
                    run_behavior_suite(
                        scenarios=scenarios,
                        transport=transport,
                        judge=None,
                        budget_seconds=100,
                    )
                )

        self.assertEqual(transport.calls, ["one", "two", "three", "one"])
        self.assertEqual([run.scenario_cursor for run in runs], [1, 2, 0, 1])
        self.assertTrue(all(run.results.filter(details__kind="skipped").count() == 2 for run in runs))
        logs = "\n".join(captured.output)
        self.assertIn("rotation cursor before=2 after=0 ran=s3", logs)
        self.assertIn("rotation cursor before=0 after=1 ran=s1", logs)

    def test_budget_skips_remaining_scenarios_with_reason(self):
        # s1 (1 warm turn: worst 55s) fits a 200s budget; s2 (4 warm turns:
        # worst 4x50=200s + scope) cannot — it must be skipped-with-reason, never
        # silently dropped, and the run stays a valid PASS on s1's hard result.
        s1 = _scenario(scenario_id="s1", script=("one",))
        s2 = _scenario(scenario_id="s2", script=("a", "b", "c", "d"))
        transport = BenignTransport()
        run = run_behavior_suite(scenarios=[s1, s2], transport=transport, judge=None, budget_seconds=200)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(len(transport.calls), 1)  # only s1 drove a turn
        skipped = [r for r in _results(run) if r.details.get("kind") == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].case_id, "s2::skipped")
        self.assertTrue(skipped[0].passed)  # visible, non-gating
        self.assertEqual(skipped[0].details["reason"], "budget")

    def test_budget_too_small_for_any_scenario_errors_loudly(self):
        # If not even ONE scenario fits, a run of only skip rows would be a vacuous
        # PASS — it must raise instead (INVARIANT #3).
        scenario = _scenario(scenario_id="s1", script=("one",))
        with self.assertRaises(BehaviorConfigError):
            run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None, budget_seconds=10)

    def test_zero_scenarios_errors_loudly(self):
        with self.assertRaises(BehaviorConfigError):
            run_behavior_suite(scenarios=[], transport=BenignTransport(), judge=None)

    def test_details_are_content_safe(self):
        # Force a known marker, echo it (so it is in the reply), and plant a
        # distinctive sentinel reply — then prove NEITHER reaches any details value.
        sentinel_marker = "999-88-7777"
        scenario = _scenario(
            scenario_id="pii",
            script=("here is {{marker}} and SENTINEL_REPLY_XYZZY",),
            hard=(HardAssertion("marker_absent"), HardAssertion("reply_nonempty")),
            soft=(),
        )
        with mock.patch("apps.evals.suites.behavior._generate_marker", return_value=sentinel_marker):
            run = run_behavior_suite(scenarios=[scenario], transport=EchoTransport(), judge=None)
        for r in _results(run):
            blob = json.dumps(r.details)
            self.assertNotIn(sentinel_marker, blob)
            self.assertNotIn("SENTINEL_REPLY_XYZZY", blob)

    # --- Scenario isolation (the run-33 cross-scenario contamination fix) --- #

    def test_context_bleed_transport_leaks_within_one_scope(self):
        # Oracle sanity: the isolation test below is only meaningful if the fake
        # container ACTUALLY bleeds when turns share a scope. Prove it does (same
        # scope → later reply carries the earlier turn) and that a fresh scope is
        # clean — so the cross-scenario PASS is caused by fresh scopes, not by an
        # inert fake that never leaks.
        t = ContextBleedTransport()
        t.open_conversation()
        t.send_turn(text="remember SECRET_X")
        second = t.send_turn(text="what did I say")
        self.assertIn("SECRET_X", second.reply_text)  # same scope → bleeds
        t.open_conversation()  # a fresh scope
        third = t.send_turn(text="anything")
        self.assertNotIn("SECRET_X", third.reply_text)  # new scope → clean

    def test_consecutive_scenarios_run_in_isolated_scopes(self):
        # THE isolation guarantee. s1 plants a distinctive token; s2 must never see
        # it. s2's forbidden_absent for that token is the gate: it PASSES only if
        # s2's container never saw s1's turns — i.e. the suite opened a fresh scope
        # per scenario. Under the old single-scope behavior s2's echo would include
        # s1's token and this assertion would FLIP to FAIL.
        secret = "SCN1_ONLY_TOKEN_ZZZ"
        s1 = _scenario(scenario_id="s1", script=(f"please remember {secret}",), hard=(HardAssertion("reply_nonempty"),))
        s2 = _scenario(
            scenario_id="s2",
            script=("what did I just tell you",),
            hard=(HardAssertion("forbidden_absent", forbidden=(secret,)),),
        )
        transport = ContextBleedTransport()
        run = run_behavior_suite(scenarios=[s1, s2], transport=transport, judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        # Two scenarios → two distinct scopes.
        self.assertEqual(len(transport._scopes), 2)
        # s2 never saw s1's token → forbidden_absent PASSED with the clean code.
        s2_hard = [r for r in _hard(run) if r.case_id.startswith("s2::")]
        self.assertEqual(len(s2_hard), 1)
        self.assertTrue(s2_hard[0].passed)
        self.assertEqual(s2_hard[0].details["code"], "clean")

    def test_each_driven_scenario_opens_one_fresh_scope(self):
        # One fresh scope per DRIVEN scenario; a budget-skipped scenario opens none
        # (its scope is never created because it never drives a turn).
        s1 = _scenario(scenario_id="s1", script=("one",))
        s2 = _scenario(scenario_id="s2", script=("a", "b", "c", "d"))  # 4 warm turns → won't fit 200s
        transport = BenignTransport()
        run = run_behavior_suite(scenarios=[s1, s2], transport=transport, judge=None, budget_seconds=200)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        # s1 drove (1 turn) and opened exactly one scope; s2 was budget-skipped.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.opened_scopes), 1)

    def test_hard_rows_record_isolation_provenance(self):
        # Every driven scenario's hard rows carry isolated=True — proof the run used
        # the fresh-scope isolation path (an old contaminated run has no such flag).
        s1 = _scenario(scenario_id="s1", script=("one",))
        s2 = _scenario(scenario_id="s2", script=("two",))
        run = run_behavior_suite(scenarios=[s1, s2], transport=BenignTransport(), judge=None)
        hard = _hard(run)
        self.assertEqual(len(hard), 2)
        for r in hard:
            self.assertIs(r.details["isolated"], True)

    def test_scope_open_failure_errors_run_loudly(self):
        # A scope we cannot open means we cannot isolate — the run must ERROR loudly
        # (INVARIANT #3), never silently drive into a shared/contaminated scope. The
        # failure propagates so record_run closes the run 'error' and it hits the DLQ.
        scenario = _scenario(scenario_id="s1", script=("one",))
        with self.assertRaises(BehaviorConfigError):
            run_behavior_suite(scenarios=[scenario], transport=ScopeOpenFailsTransport(), judge=None)
        # No hard row was recorded for the scenario the failed open guarded.
        run = EvalRun.objects.filter(suite="behavior").latest("started_at")
        self.assertEqual(run.status, EvalRun.Status.ERROR)
        self.assertFalse(run.results.filter(case_id__startswith="s1::hard").exists())


# --------------------------------------------------------------------------- #
# Timezone hard assertions                                                     #
# --------------------------------------------------------------------------- #
class TimezoneIsAssertionTest(TestCase):
    """``timezone_is`` reads BOTH halves back from the DB: the profile timezone and
    every cron row that carries one. Break-and-restore proof: drop the cron half and
    ``test_fails_when_one_cron_row_differs`` (and the multi-row pass) goes red."""

    def setUp(self):
        self.tenant = _synthetic_tenant()
        self.run = _driven_run("Sure — I've moved everything to Chicago time.")

    def _set_profile_tz(self, tz: str) -> None:
        User.objects.filter(pk=self.tenant.user_id).update(timezone=tz)

    def _check(self, value: str = "America/Chicago"):
        return run_hard_assertion(self.run, self.tenant, HardAssertion("timezone_is", value=value))

    def test_passes_when_profile_and_every_cron_row_match(self):
        self._set_profile_tz("America/Chicago")
        _cron_with_tz(self.tenant, "America/Chicago")
        _cron_with_tz(self.tenant, "America/Chicago")
        self.assertEqual(self._check(), (True, "tz_and_crons_match"))

    def test_passes_when_tenant_has_no_cron_rows(self):
        self._set_profile_tz("America/Chicago")
        self.assertEqual(self._check(), (True, "tz_and_crons_match"))

    def test_fails_when_profile_timezone_differs(self):
        self._set_profile_tz("Asia/Tokyo")
        _cron_with_tz(self.tenant, "America/Chicago")
        passed, code = self._check()
        self.assertFalse(passed)
        self.assertEqual(code, "user_tz=Asia/Tokyo")

    def test_fails_when_one_cron_row_differs(self):
        self._set_profile_tz("America/Chicago")
        _cron_with_tz(self.tenant, "America/Chicago")
        _cron_with_tz(self.tenant, "Asia/Tokyo")
        passed, code = self._check()
        self.assertFalse(passed)
        self.assertEqual(code, "cron_row_tz=Asia/Tokyo")

    def test_reads_the_db_not_the_in_memory_tenant(self):
        # The assertion must re-read from the DB: a stale in-memory User object
        # carrying the target tz must not green-light a row that says otherwise.
        self._set_profile_tz("America/Chicago")
        self.assertEqual(self.tenant.user.timezone, "UTC")  # stale in-memory copy
        self.assertEqual(self._check(), (True, "tz_and_crons_match"))

    def test_ignores_unmanaged_rows_but_still_fails_a_managed_mismatch(self):
        self._set_profile_tz("America/Chicago")
        _cron_with_tz(self.tenant, "Asia/Tokyo", managed=False)
        self.assertEqual(self._check(), (True, "tz_and_crons_match"))

        managed = _cron_with_tz(self.tenant, "America/Chicago")
        managed.data = {"schedule": {**managed.data["schedule"], "tz": "Asia/Tokyo"}}
        managed.save()
        passed, code = self._check()
        self.assertFalse(passed)
        self.assertEqual(code, "cron_row_tz=Asia/Tokyo")

    def test_reports_the_fence_before_reading_the_db(self):
        self._set_profile_tz("America/Chicago")
        with mock.patch(
            "apps.orchestrator.migration_cron_fence.cron_edits_fenced",
            return_value=True,
        ):
            self.assertEqual(self._check(), (False, "cron_edits_fenced"))


class AsksWhichCityAssertionTest(TestCase):
    """``asks_which_city``: country/region-level travel must keep Tokyo and ASK."""

    def setUp(self):
        self.tenant = _synthetic_tenant()
        User.objects.filter(pk=self.tenant.user_id).update(timezone="Asia/Tokyo")

    def _check(self, *replies: str):
        return run_hard_assertion(
            _driven_run(*replies),
            self.tenant,
            HardAssertion("asks_which_city"),
        )

    def test_passes_when_tz_unchanged_and_last_reply_asks_city(self):
        self.assertEqual(self._check("Got it.", "Which city are you in?"), (True, "asked_which_city"))

    def test_passes_on_where_in_phrasing(self):
        self.assertEqual(self._check("Where in the US are you?"), (True, "asked_which_city"))

    def test_passes_on_state_phrasing(self):
        self.assertEqual(self._check("Which state are you in?"), (True, "asked_which_city"))

    def test_passes_on_whereabouts_phrasing(self):
        self.assertEqual(self._check("Whereabouts are you staying?"), (True, "asked_which_city"))

    def test_fails_without_a_question(self):
        passed, code = self._check("Noted — America. I'll leave your reminders as they are.")
        self.assertFalse(passed)
        self.assertEqual(code, "no_question")

    def test_fails_when_question_is_not_about_the_city(self):
        passed, code = self._check("Should I send you the weather?")
        self.assertFalse(passed)
        self.assertEqual(code, "no_city_ask")

    def test_fails_if_timezone_changed(self):
        User.objects.filter(pk=self.tenant.user_id).update(timezone="America/Chicago")
        passed, code = self._check("Which city are you in?")
        self.assertFalse(passed)
        self.assertEqual(code, "tz_moved=America/Chicago")

    def test_only_the_last_reply_counts(self):
        passed, code = self._check("Which city are you in?", "Enjoy America!")
        self.assertFalse(passed)
        self.assertEqual(code, "no_question")


# --------------------------------------------------------------------------- #
# Per-scenario setup hook                                                     #
# --------------------------------------------------------------------------- #
class ResetTimezoneTokyoSetupTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()
        User.objects.filter(pk=self.tenant.user_id).update(timezone="America/Chicago")
        self.tenant.refresh_from_db()
        self.tenant.user.refresh_from_db()

    def test_resets_profile_crons_and_situation_and_pushes_once(self):
        from apps.orchestrator.workspace_envelope import TRIGGER_MANUAL

        chicago = _cron_with_tz(self.tenant, "America/Chicago")
        already_tokyo = _cron_with_tz(self.tenant, "Asia/Tokyo")
        absolute = _cron_with_tz(self.tenant, None, kind="at")  # managed, no tz yet

        now = dj_timezone.now()
        UserSituation.objects.create(
            tenant=self.tenant,
            current_place_label="Chicago",
            current_place_since=now,
            current_place_last_observed_at=now,
            current_place_source="app",
            device_tz="America/Chicago",
            device_tz_since=now,
            device_tz_last_observed_at=now,
            device_tz_source_device="iphone",
            tz_offer_dismissed_for="America/Chicago",
        )

        with (
            mock.patch("apps.orchestrator.services.update_tenant_config"),
            mock.patch("apps.orchestrator.workspace_envelope.push_user_md") as push,
        ):
            reset_timezone_tokyo(self.tenant)

        push.assert_called_once_with(self.tenant, force=True, trigger=TRIGGER_MANUAL)

        self.assertEqual(User.objects.get(pk=self.tenant.user_id).timezone, TOKYO)
        # Any managed row moves — even an ``at`` row that carried no tz — exactly
        # like RuntimeProfileUpdateView. An already-Tokyo row is left alone.
        self.assertEqual(CronJob.objects.get(pk=chicago.pk).data["schedule"]["tz"], TOKYO)
        self.assertEqual(CronJob.objects.get(pk=already_tokyo.pk).data["schedule"]["tz"], TOKYO)
        self.assertEqual(CronJob.objects.get(pk=absolute.pk).data["schedule"]["tz"], TOKYO)

        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.current_place_label, "")
        self.assertIsNone(situation.current_place_since)
        self.assertIsNone(situation.current_place_last_observed_at)
        self.assertEqual(situation.current_place_source, "")
        self.assertEqual(situation.device_tz, "")
        self.assertIsNone(situation.device_tz_since)
        self.assertIsNone(situation.device_tz_last_observed_at)
        self.assertEqual(situation.device_tz_source_device, "")
        self.assertEqual(situation.tz_offer_dismissed_for, "")

    def test_moves_managed_every_and_at_rows_but_not_unmanaged_rows(self):
        every = _cron_with_tz(self.tenant, "America/Chicago", kind="every")
        at = _cron_with_tz(self.tenant, "America/Chicago", kind="at")
        unmanaged = _cron_with_tz(self.tenant, "America/Chicago", kind="cron", managed=False)

        with (
            mock.patch("apps.orchestrator.services.update_tenant_config"),
            mock.patch("apps.orchestrator.workspace_envelope.push_user_md"),
        ):
            reset_timezone_tokyo(self.tenant)

        for row in (every, at):
            row.refresh_from_db()
            self.assertEqual(row.data["schedule"]["tz"], TOKYO)
        unmanaged.refresh_from_db()
        self.assertEqual(unmanaged.data["schedule"]["tz"], "America/Chicago")

    def test_refreshes_config_after_writes_and_before_the_push(self):
        from apps.orchestrator.workspace_envelope import TRIGGER_MANUAL

        manager = mock.Mock()
        with (
            mock.patch.object(Tenant, "bump_pending_config", manager.bump_pending_config),
            mock.patch("apps.orchestrator.services.update_tenant_config", manager.update_tenant_config),
            mock.patch("apps.orchestrator.workspace_envelope.push_user_md", manager.push_user_md),
        ):
            reset_timezone_tokyo(self.tenant)

        self.assertEqual(
            manager.mock_calls,
            [
                mock.call.bump_pending_config(),
                mock.call.update_tenant_config(str(self.tenant.id)),
                mock.call.push_user_md(self.tenant, force=True, trigger=TRIGGER_MANUAL),
            ],
        )

    def test_refuses_a_non_eval_tenant_without_writing(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(is_eval_sink=False)
        self.tenant.refresh_from_db()
        cron = _cron_with_tz(self.tenant, "America/Chicago")
        situation = UserSituation.objects.create(
            tenant=self.tenant,
            current_place_label="Chicago",
            current_place_source="app",
        )

        with (
            mock.patch.object(Tenant, "bump_pending_config") as bump,
            mock.patch("apps.orchestrator.services.update_tenant_config") as update,
            mock.patch("apps.orchestrator.workspace_envelope.push_user_md") as push,
            self.assertRaisesRegex(RuntimeError, "refuses a non-eval tenant"),
        ):
            reset_timezone_tokyo(self.tenant)

        bump.assert_not_called()
        update.assert_not_called()
        push.assert_not_called()
        self.assertEqual(User.objects.get(pk=self.tenant.user_id).timezone, "America/Chicago")
        cron.refresh_from_db()
        self.assertEqual(cron.data["schedule"]["tz"], "America/Chicago")
        situation.refresh_from_db()
        self.assertEqual(situation.current_place_label, "Chicago")

    def test_creates_no_situation_row_when_none_exists(self):
        with (
            mock.patch("apps.orchestrator.services.update_tenant_config"),
            mock.patch("apps.orchestrator.workspace_envelope.push_user_md"),
        ):
            reset_timezone_tokyo(self.tenant)
        self.assertFalse(UserSituation.objects.filter(tenant=self.tenant).exists())
        self.assertEqual(User.objects.get(pk=self.tenant.user_id).timezone, TOKYO)

    def test_propagates_push_failure_so_the_suite_records_it(self):
        with (
            mock.patch("apps.orchestrator.services.update_tenant_config"),
            mock.patch(
                "apps.orchestrator.workspace_envelope.push_user_md",
                side_effect=RuntimeError("container unreachable"),
            ),
            self.assertRaises(RuntimeError),
        ):
            reset_timezone_tokyo(self.tenant)


# --------------------------------------------------------------------------- #
# On-demand (``only``) selection + setup dispatch in the suite                #
# --------------------------------------------------------------------------- #
class OnlySelectionTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()
        self.ctx = override_settings(EVAL_BEHAVIOR_TENANT_ID=str(self.tenant.id))
        self.ctx.enable()
        self.addCleanup(self.ctx.disable)

    def test_only_drives_named_ids_in_order_and_leaves_rotation_cursor(self):
        # Rotation math, with a fake clock so each turn costs 50s: a 100s-budget
        # default fire drives s1 only (cursor 1), then an ``only`` fire drives just
        # s3 and stores NO cursor, then a default fire resumes at s2 from cursor 1.
        clock = [0.0]

        class ClockTransport(BenignTransport):
            def send_turn(self, **kwargs):
                result = super().send_turn(**kwargs)
                clock[0] += 50.0
                return result

        scenarios = [
            _scenario(scenario_id="s1", script=("one",)),
            _scenario(scenario_id="s2", script=("two",)),
            _scenario(scenario_id="s3", script=("three",)),
        ]
        transport = ClockTransport()
        with mock.patch("apps.evals.suites.behavior.time.monotonic", side_effect=lambda: clock[0]):
            first = run_behavior_suite(scenarios=scenarios, transport=transport, judge=None, budget_seconds=100)
            # A budget far too small for any scheduled fire proves ``only`` does not
            # get truncated by the rotation budget gate.
            only_run = run_behavior_suite(
                scenarios=scenarios,
                transport=transport,
                judge=None,
                budget_seconds=1,
                only=["s3"],
            )
            third = run_behavior_suite(scenarios=scenarios, transport=transport, judge=None, budget_seconds=100)

        self.assertEqual(transport.calls, ["one", "three", "two"])
        self.assertEqual([first.scenario_cursor, only_run.scenario_cursor, third.scenario_cursor], [1, None, 2])
        # The only-run recorded results for s3 and nothing else; no skip rows.
        self.assertEqual({r.case_id.split("::")[0] for r in _results(only_run)}, {"s3"})
        self.assertEqual(
            [r for r in _results(only_run) if r.details.get("kind") == "skipped"],
            [],
        )
        self.assertEqual(len(transport.opened_scopes), 3)  # one fresh scope per driven scenario

    def test_only_preserves_the_callers_order(self):
        scenarios = [
            _scenario(scenario_id="s1", script=("one",)),
            _scenario(scenario_id="s2", script=("two",)),
        ]
        transport = BenignTransport()
        run_behavior_suite(scenarios=scenarios, transport=transport, judge=None, only=["s2", "s1"])
        self.assertEqual(transport.calls, ["two", "one"])

    def test_only_unknown_id_errors_loudly(self):
        scenarios = [_scenario(scenario_id="s1", script=("one",))]
        with self.assertRaisesRegex(BehaviorConfigError, "unknown behavior scenario id"):
            run_behavior_suite(scenarios=scenarios, transport=BenignTransport(), judge=None, only=["nope"])
        run = EvalRun.objects.filter(suite="behavior").latest("started_at")
        self.assertEqual(run.status, EvalRun.Status.ERROR)

    def test_only_empty_list_errors_loudly(self):
        scenarios = [_scenario(scenario_id="s1", script=("one",))]
        with self.assertRaises(BehaviorConfigError):
            run_behavior_suite(scenarios=scenarios, transport=BenignTransport(), judge=None, only=[])

    def test_scenario_setup_runs_before_the_first_turn(self):
        calls: list[tuple[str, str]] = []

        def fake_setup(tenant):
            calls.append(("setup", str(tenant.id)))

        class RecordingTransport(BenignTransport):
            def send_turn(self, **kwargs):
                calls.append(("turn", kwargs["text"]))
                return super().send_turn(**kwargs)

        scenario = _scenario(
            scenario_id="tz",
            script=("hello",),
            hard=(HardAssertion("timezone_is", value=TOKYO),),
            setup="reset_timezone_tokyo",
        )
        User.objects.filter(pk=self.tenant.user_id).update(timezone=TOKYO)
        with mock.patch.dict(SETUPS, {"reset_timezone_tokyo": fake_setup}):
            run = run_behavior_suite(scenarios=[scenario], transport=RecordingTransport(), judge=None)
        self.assertEqual(calls, [("setup", str(self.tenant.id)), ("turn", "hello")])
        self.assertEqual(run.status, EvalRun.Status.PASS)

    def test_setup_failure_fails_the_scenario_without_driving_it(self):
        def boom(tenant):
            raise RuntimeError("control plane unavailable")

        bad = _scenario(scenario_id="bad", script=("hello",), setup="reset_timezone_tokyo")
        good = _scenario(scenario_id="good", script=("two",))
        transport = BenignTransport()
        with mock.patch.dict(SETUPS, {"reset_timezone_tokyo": boom}):
            run = run_behavior_suite(scenarios=[bad, good], transport=transport, judge=None)

        self.assertEqual(transport.calls, ["two"])  # the failed scenario was never driven
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        bad_rows = [r for r in _hard(run) if r.case_id.startswith("bad::")]
        self.assertEqual(len(bad_rows), 1)
        self.assertFalse(bad_rows[0].passed)
        self.assertEqual(bad_rows[0].details["assertion"], "setup")
        self.assertEqual(bad_rows[0].details["code"], "RuntimeError")


# --------------------------------------------------------------------------- #
# manage.py run_behavior_scenarios                                            #
# --------------------------------------------------------------------------- #
class _FakeRun:
    def __init__(self, rows):
        self._rows = rows
        self.results = self

    def all(self):
        return self._rows


class RunBehaviorScenariosCommandTest(TestCase):
    def test_unknown_id_exits_non_zero_with_a_clear_message(self):
        with self.assertRaises(CommandError) as caught:
            call_command("run_behavior_scenarios", "timezone_move_after_yes", "not_a_scenario")
        message = str(caught.exception)
        self.assertIn("not_a_scenario", message)
        self.assertIn("timezone_move_after_yes", message)

    def test_prints_one_line_per_scenario_without_reply_text(self):
        from types import SimpleNamespace

        rows = [
            SimpleNamespace(
                case_id="timezone_move_after_yes::hard:timezone_is",
                passed=False,
                details={"kind": "hard", "assertion": "timezone_is", "code": "user_tz=Asia/Tokyo"},
            ),
            SimpleNamespace(
                case_id="timezone_country_asks_city::hard:asks_which_city",
                passed=True,
                details={"kind": "hard", "assertion": "asks_which_city", "code": "asked_which_city"},
            ),
        ]
        out = mock.MagicMock()
        with mock.patch(
            "apps.evals.management.commands.run_behavior_scenarios.run_behavior_suite",
            return_value=_FakeRun(rows),
        ) as suite:
            call_command(
                "run_behavior_scenarios",
                "timezone_move_after_yes",
                "timezone_country_asks_city",
                stdout=out,
            )
        suite.assert_called_once_with(
            only=["timezone_move_after_yes", "timezone_country_asks_city"],
            trigger=EvalRun.Trigger.MANUAL,
        )
        lines = [call.args[0].rstrip("\n") for call in out.write.call_args_list]
        self.assertEqual(
            lines,
            [
                "timezone_move_after_yes FAIL timezone_is:user_tz=Asia/Tokyo",
                "timezone_country_asks_city PASS asks_which_city:asked_which_city",
            ],
        )

    def test_prints_a_setup_failure_with_a_single_setup_prefix(self):
        tenant = _synthetic_tenant()

        def boom(_tenant):
            raise RuntimeError("control plane unavailable")

        out = mock.MagicMock()
        with (
            override_settings(EVAL_BEHAVIOR_TENANT_ID=str(tenant.id)),
            mock.patch(
                "apps.evals.suites.behavior.build_behavior_transport",
                return_value=BenignTransport(),
            ),
            mock.patch.dict(SETUPS, {"reset_timezone_tokyo": boom}),
        ):
            call_command("run_behavior_scenarios", "timezone_country_asks_city", stdout=out)

        lines = [call.args[0].rstrip("\n") for call in out.write.call_args_list]
        self.assertEqual(lines, ["timezone_country_asks_city FAIL setup:RuntimeError"])


# --------------------------------------------------------------------------- #
# Real transport HTTP behavior — injected httpx client (no network)           #
# --------------------------------------------------------------------------- #
class _Resp:
    """Minimal httpx.Response stand-in (status_code + json())."""

    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """Scripted httpx.Client for HttpxBehaviorTransport. Routes POSTs by URL suffix
    and records each POST's JSON body so a test can assert the active scope's
    thread_id rides the message turn. Never touches the network."""

    def __init__(self, *, threads=None, messages=None, polls=None, thread_exc=None):
        self._threads = threads
        self._messages = messages
        self._polls = list(polls or [])
        self._thread_exc = thread_exc
        self.post_bodies: list[dict] = []
        self.get_calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_bodies.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if url.endswith("/threads/"):
            if self._thread_exc:
                raise self._thread_exc
            return self._threads
        return self._messages

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        if not self._polls:
            raise AssertionError("unexpected GET")
        return self._polls.pop(0)

    def close(self):
        pass


class HttpxBehaviorTransportTest(TestCase):
    def _transport(self, client):
        return HttpxBehaviorTransport(base_url="https://api.test", pat="pat_x", client=client)

    def test_prewarm_uses_normal_chat_post_and_waits_for_ready_reply(self):
        client = _FakeHttpxClient(
            threads=_Resp(201, {"id": "thread-warm"}),
            messages=_Resp(200, {"status": "ready", "reply_text": "awake", "error": ""}),
        )
        turn = self._transport(client).prewarm()
        self.assertTrue(turn.ok)
        self.assertEqual(
            [item["url"] for item in client.post_bodies],
            [
                "https://api.test/api/v1/chat/threads/",
                "https://api.test/api/v1/chat/messages/",
            ],
        )
        self.assertEqual(client.post_bodies[1]["json"]["thread_id"], "thread-warm")

    def test_prewarm_final_poll_cannot_overrun_wall_deadline(self):
        client = _FakeHttpxClient(
            threads=_Resp(201, {"id": "thread-warm"}),
            messages=_Resp(200, {"status": "processing"}),
            polls=[_Resp(200, {"status": "ready", "reply_text": "awake", "error": ""})],
        )
        with (
            mock.patch("apps.evals.behavior.transport.time.monotonic", side_effect=[0.0, 129.0, 129.5]),
            mock.patch("apps.evals.behavior.transport.time.sleep"),
        ):
            turn = self._transport(client).prewarm()
        self.assertTrue(turn.ok)
        self.assertEqual(client.post_bodies[1]["timeout"], 15.0)
        self.assertEqual(client.get_calls[0]["timeout"], 0.5)

    def test_open_conversation_mints_and_activates_thread(self):
        client = _FakeHttpxClient(
            threads=_Resp(201, {"id": "thread-123"}),
            messages=_Resp(200, {"status": "ready", "reply_text": "hi", "error": ""}),
        )
        transport = self._transport(client)
        self.assertEqual(transport.open_conversation(), "thread-123")
        # The scope's thread_id rides the NEXT turn's POST body → its own session.
        turn = transport.send_turn(text="hello")
        self.assertTrue(turn.ok)
        msg_post = next(b for b in client.post_bodies if b["url"].endswith("/messages/"))
        self.assertEqual(msg_post["json"]["thread_id"], "thread-123")

    def test_fresh_scope_replaces_the_active_thread(self):
        # Each open_conversation switches the active scope, so consecutive scenarios
        # never share a thread id.
        client = _FakeHttpxClient(
            threads=_Resp(201, {"id": "thread-A"}),
            messages=_Resp(200, {"status": "ready", "reply_text": "hi", "error": ""}),
        )
        transport = self._transport(client)
        transport.open_conversation()
        transport.send_turn(text="turn in A")
        client._threads = _Resp(201, {"id": "thread-B"})
        transport.open_conversation()
        transport.send_turn(text="turn in B")
        msg_threads = [b["json"]["thread_id"] for b in client.post_bodies if b["url"].endswith("/messages/")]
        self.assertEqual(msg_threads, ["thread-A", "thread-B"])

    def test_send_turn_without_scope_carries_no_thread_id(self):
        # The suite always opens a scope first; this documents the fallback is a
        # clean no-thread turn (not a crash) rather than a silent shared-scope drive.
        client = _FakeHttpxClient(messages=_Resp(200, {"status": "ready", "reply_text": "hi", "error": ""}))
        self._transport(client).send_turn(text="hello")
        self.assertNotIn("thread_id", client.post_bodies[0]["json"])

    def test_document_turn_uses_real_attachment_field(self):
        client = _FakeHttpxClient(
            threads=_Resp(201, {"id": "thread-doc"}),
            messages=_Resp(200, {"status": "ready", "reply_text": "proposal", "error": ""}),
        )
        transport = self._transport(client)
        transport.open_conversation()
        turn = transport.send_turn(text="review", document=True)
        self.assertTrue(turn.ok)
        message = next(item for item in client.post_bodies if item["url"].endswith("/messages/"))
        self.assertTrue(message["json"]["document"])
        self.assertEqual(message["json"]["thread_id"], "thread-doc")

    @mock.patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="gateway-token")
    def test_telegram_channel_returns_raw_gateway_reply_without_relay(self, _mock_token):
        tenant = _synthetic_tenant()
        tenant.container_fqdn = "oc-eval.example.com"
        tenant.save(update_fields=["container_fqdn"])
        client = _FakeHttpxClient(
            threads=_Resp(201, {"id": "thread-tg"}),
            messages=_Resp(
                200,
                {
                    "choices": [
                        {"message": {"content": "Trend [[chart:mood_trend]]"}},
                    ]
                },
            ),
        )
        transport = HttpxBehaviorTransport(
            base_url="https://api.test",
            pat="pat_x",
            client=client,
            tenant=tenant,
        )
        transport.open_conversation(channel="telegram")
        turn = transport.send_turn(text="show my trend")
        self.assertTrue(turn.ok)
        self.assertIn("[[chart:mood_trend]]", turn.reply_text)
        gateway = next(item for item in client.post_bodies if "/v1/chat/completions" in item["url"])
        self.assertEqual(gateway["headers"]["X-Channel"], "telegram")
        self.assertEqual(gateway["headers"]["X-OpenClaw-Message-Channel"], "telegram")
        self.assertEqual(gateway["json"]["user"], "thread:thread-tg")

    def test_open_conversation_non_2xx_raises(self):
        client = _FakeHttpxClient(threads=_Resp(500, {}))
        with self.assertRaises(BehaviorConfigError):
            self._transport(client).open_conversation()

    def test_open_conversation_missing_id_raises(self):
        client = _FakeHttpxClient(threads=_Resp(201, {}))
        with self.assertRaises(BehaviorConfigError):
            self._transport(client).open_conversation()

    def test_open_conversation_http_error_raises(self):
        client = _FakeHttpxClient(thread_exc=httpx.ConnectError("boom"))
        with self.assertRaises(BehaviorConfigError):
            self._transport(client).open_conversation()


# --------------------------------------------------------------------------- #
# Transport factory: PAT safety (journey precedent) + named deferral          #
# --------------------------------------------------------------------------- #
class BuildTransportTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()

    @override_settings(DJANGO_BASE_URL="", EVAL_BEHAVIOR_PAT="")
    def test_unwired_transport_raises_named_deferral(self):
        with self.assertRaises(BehaviorConfigError):
            build_behavior_transport(self.tenant)

    @override_settings(DJANGO_BASE_URL="https://api.test")
    def test_valid_pat_for_behavior_tenant_builds_transport(self):
        raw = _mint_pat(self.tenant.user)
        with override_settings(EVAL_BEHAVIOR_PAT=raw):
            transport = build_behavior_transport(self.tenant)
        self.assertTrue(hasattr(transport, "send_turn"))

    @override_settings(DJANGO_BASE_URL="https://api.test")
    def test_pat_for_other_tenant_refused(self):
        # THE safety property: a mis-minted PAT (belonging to another account) must
        # never let behavior scripts drive a different tenant's assistant.
        other = _synthetic_tenant()
        raw = _mint_pat(other.user)
        with override_settings(EVAL_BEHAVIOR_PAT=raw), self.assertRaises(BehaviorConfigError):
            build_behavior_transport(self.tenant)

    @override_settings(DJANGO_BASE_URL="https://api.test")
    def test_revoked_pat_refused(self):
        raw = _mint_pat(self.tenant.user, revoked=True)
        with override_settings(EVAL_BEHAVIOR_PAT=raw), self.assertRaises(BehaviorConfigError):
            build_behavior_transport(self.tenant)

    @override_settings(DJANGO_BASE_URL="https://api.test", EVAL_BEHAVIOR_PAT="pat_not-a-real-token")
    def test_unknown_pat_refused(self):
        with self.assertRaises(BehaviorConfigError):
            build_behavior_transport(self.tenant)


# --------------------------------------------------------------------------- #
# Task wrapper                                                               #
# --------------------------------------------------------------------------- #
class BehaviorTaskTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()
        self.ctx = override_settings(EVAL_BEHAVIOR_TENANT_ID=str(self.tenant.id), PLATFORM_OWNER_EMAIL="")
        self.ctx.enable()
        self.addCleanup(self.ctx.disable)

    def test_nonpass_run_raises_into_dlq(self):
        # Benign transport creates no cron → cron_registered FAILS → finalize
        # alerts (owner unset → skipped) then raises into the DLQ.
        scenario = _scenario(scenario_id="cron", hard=(HardAssertion("cron_registered"),))
        with (
            mock.patch("apps.evals.suites.behavior.load_all_scenarios", return_value=[scenario]),
            self.assertRaises(RuntimeError),
        ):
            eval_behavior_task(transport=BenignTransport(), judge=FakeJudge())

    def test_pass_run_returns_summary(self):
        # Keep this wrapper test scoped to one known-pass case; the fixture pack's
        # per-rule effects are covered independently above.
        scenario = _scenario(scenario_id="cron", hard=(HardAssertion("cron_registered"),))
        with mock.patch("apps.evals.suites.behavior.load_all_scenarios", return_value=[scenario]):
            out = eval_behavior_task(transport=CronCreatingTransport(self.tenant), judge=FakeJudge())
        self.assertEqual(out["suite"], "behavior")
        self.assertEqual(out["status"], EvalRun.Status.PASS)
        self.assertGreater(out["cases"], 0)

    def test_config_error_alerts_owner_then_raises(self):
        # The fire-today path (tenant unconfigured): record_run closes the run
        # 'error' and the exception propagates — the task must STILL email the
        # owner before re-raising, so "DLQ + owner alert" is true on this path too.
        with (
            override_settings(
                EVAL_BEHAVIOR_TENANT_ID="",
                EVAL_EMAIL_ALERTS_ENABLED=True,
                PLATFORM_OWNER_EMAIL="owner@test.com",
            ),
            self.assertRaises(BehaviorConfigError),
        ):
            eval_behavior_task(transport=BenignTransport(), judge=FakeJudge())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("behavior", mail.outbox[0].subject)
