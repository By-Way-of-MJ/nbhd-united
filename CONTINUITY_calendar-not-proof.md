# Task Ledger: Calendar Is Not Completion Proof

Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: apps/orchestrator/config_generator.py; apps/cron/patterns
Owner: codex

## Goal
- Require completion records or user confirmation; never infer completion from elapsed calendar events or assistant-written daily notes.
## Constraints / Assumptions
- Prompt/tool text only, no models or AGENTS.md changes; preserve budget pins.
- Work only in this worktree; no docker-gate, push, PR, stash, or branch switch.
- Commit with the exact requested title and co-author trailer; stage by path.
## Key decisions
- Cover seeded morning/evening/heartbeat/week-ahead prompts and typed calendar-reading patterns, plus both read-tool descriptions.
## State
- Done: Scoped prompt/tool changes, regression assertions, and final validation: 328 Django tests + 11 plugin tests passed; Ruff and diff checks passed.
- Now: Complete; included in the requested local fix commit.
- Next: User handoff; no push or PR.
## Links
- Upstream: CONTINUITY.md
- Downstream: none
- Related: docs/agents/backend.md
## Open questions
- None.
## Working set
- apps/orchestrator/config_generator.py; apps/orchestrator/tests.py
- apps/cron/patterns/{daily_briefing,domain_summary,quote_user_intent}.py
- runtime/openclaw/plugins/{nbhd-google-tools,nbhd-datebook-tools}
## Notes
- Root cause supplied by user; no production investigation needed.
- Implemented evidence-only preamble, local calendar guards, and heartbeat daily-note deduplication wording; updated both calendar read descriptions.
- Regression coverage: all four seeded prompts, three typed patterns under both readiness states, and both registered tool descriptions.
- First make test-local attempt stopped before tests: DATABASE_URL absent. Resolved with local PostgreSQL (127.0.0.1:5432/postgres, pgvector installed); runner uses test_nbhd_calendar_not_proof_89cb76.
- First focused Django run: 318 tests passed. Final run includes test_reminder_capability and quote-boundary assertion added during review.
- node --test runtime/openclaw/plugins/nbhd-google-tools/index.test.mjs runtime/openclaw/plugins/nbhd-datebook-tools/agenda-render.test.js: 11 passed, 0 skipped.
- ruff check and ruff format --check on all six changed Python files: passed (6 already formatted); git diff --check passed. Initial missing CALENDAR_READ_TOOLS imports caught by Ruff and fixed.
- Final Django command: DATABASE_URL=postgres://michaeljones@127.0.0.1:5432/postgres make test-local TESTS='apps.orchestrator.tests apps.cron.tests.test_patterns apps.orchestrator.test_cron_prompt_invariants apps.orchestrator.test_cron_prompt_churn apps.orchestrator.test_cron_envelope apps.orchestrator.test_rules_delivery apps.orchestrator.test_reminder_capability apps.orchestrator.test_datebook_tool_schema apps.orchestrator.test_contextual_location_prompt apps.orchestrator.test_proactive_suggestions_prompt'.
- Ruff commands: ruff check and ruff format --check, each with apps/orchestrator/config_generator.py apps/orchestrator/tests.py apps/cron/patterns/daily_briefing.py apps/cron/patterns/domain_summary.py apps/cron/patterns/quote_user_intent.py apps/cron/tests/test_patterns.py.
- Final Django run: 328 tests passed, 0 failures/skips; test database destroyed by runner. Includes 23,505-character rules-delivery pin and reminder/bootstrap budget tests, unchanged.
- Final plugin run: 11 tests passed, 0 failures/skips. Final Ruff lint/format and git diff --check passed.
- Commit title: fix(cron): a passed calendar event is not proof it happened
- Commit trailer: Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
