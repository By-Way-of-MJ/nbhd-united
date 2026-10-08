# Task Ledger: Reminder list and cancel
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: apps/cron; apps/integrations; runtime/openclaw/plugins
Owner: codex

## Goal
- Assistant lists and soft-cancels its tenant’s user/agent reminders; system/internal jobs protected.
## Constraints / Assumptions
- Work only in this worktree, commit on feat/assistant-cancel-reminder; no push or PR.
- No AGENTS.md or morning prompt changes; no docker-gate. Refresh changed runtime SHA pins.
- Use existing classifiers, migration fence, runtime auth/PII/origin conventions and version-specific propagation.
## Key decisions
- One-shot reminders remain cancellable despite managed=False; classify reserved names using existing helpers.
## State
- Done: reminder service, runtime endpoints, plugin tools, origin handling, tests, docs and both fixture pins.
- Now: implementation and verification complete; local commit handoff.
- Next: user review/deployment separately; no push or PR from this task.
## Links
- Upstream: CONTINUITY.md
- Downstream: none
- Related: apps/cron/share_cron_sync.py; apps/orchestrator/cron_reconcile.py
## Open questions
- None blocking.
## Working set
- apps/cron/services.py; apps/integrations/runtime_views.py; apps/integrations/urls.py; runtime/openclaw/plugins/nbhd-automation-tools
## Notes
- USER and AGENT are the only eligible source choices. 9.4 uses signed-file publication.
- Shell has python3, not python.

- Implemented tenant-scoped list/cancel, shared ownership classifier, human schedule rendering, migration/document guards and PII response protection.
- 9.4 rewrites signed file; pre-9.4 managed canonical rows explicitly enqueue existing debounced reconcile (with error reporting); other rows use existing gateway removal by id/name.
- Gated-create history receives record_action_audit(CANCELLED); all successful cancellations emit metadata-only telemetry with verified origin.
- Plugin group:plugins/config gate already exposes the automation plugin by default; manifest includes both new tools. Cron fire-time restricted toolsAllow lists stay restricted.
- Initial focused backend command (apps.cron.tests.test_cancel_user_cron apps.integrations.test_runtime_cron_reminders): 17 passed. Fixed telemetry outcome to accepted and added persistence coverage afterward.
- node --test automation-tools/test.js automation-tools/schema.test.mjs cron-enforcement/test.js: 100 passed.
- Existing automation test wrongly expected _nbhd_origin absent despite strict schema contract; corrected assertion.

- Full requested backend command: 2911 tests, 1 stale runtime_views AST pin failure plus the two known environment errors (AF_UNIX socket path / genai SpeechMetadata).
- Refreshed runtime_views entry in fixtures/cron_request_paths_main.json with existing WithoutFence AST normalization; rerunning full command.
- Ruff check and format --check pass for all 7 changed Python files. git diff --check passes; automatic runtime SHA pins match.
- Error-transport plugin regression suite: 138 passed.

- Final full backend run: 2911 tests; 2908 passed, 1 AST pin mismatch, and only the 2 user-identified environment errors. Root cause: system python3 is 3.14, repo test interpreter is 3.12; regenerated AST digest with repo .venv/bin/python.
- Focused final pin replay: `DATABASE_URL=postgres://michaeljones@127.0.0.1:5432/postgres make test-local TESTS="apps.orchestrator.test_openclaw_round_nine.MainRequestPathParityTests apps.orchestrator.test_openclaw_round_four.AutomaticPathParityTests"` — 2 passed; no remaining task-related failures. No production code changed after full run.
- Final plugin command: `node --test runtime/openclaw/plugins/nbhd-automation-tools/test.js runtime/openclaw/plugins/nbhd-automation-tools/schema.test.mjs runtime/openclaw/plugins/nbhd-cron-enforcement/test.js` — 100 passed.
- `node --test runtime/openclaw/plugins/error-transport.test.js` — 138 passed.
- Final Ruff check/format and git diff --check pass. No docker-gate, push, PR, or production mutation performed.
- Endpoints: GET /api/v1/integrations/runtime/<tenant_id>/crons/reminders/ (include_disabled=true optional), POST /api/v1/integrations/runtime/<tenant_id>/crons/cancel/.
- Tools: nbhd_cron_list_reminders; nbhd_cron_cancel_reminder. Runtime propagation remains eventual; already-running fires can finish.
