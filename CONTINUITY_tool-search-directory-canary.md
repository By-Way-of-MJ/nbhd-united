# Task Ledger: Tool Search Directory Canary
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: CONTINUITY_blank-tool-args.md
Owner: codex

## Goal
- Gate directory mode by tenant and >=2026.9.4, retain fleet-wide shape diagnostics, measure prompt cost, document historical evidence, commit locally.
## Constraints / Assumptions
- Work only in blank-tool-args on fix/blank-tool-args; no push/PR, no scratch staging, explicit paths and normal hooks.
- User-supplied weekly production counts are evidence of pre-existing missing-required-argument failures, not a proven 9.4 regression.
## Key decisions
- Empty TOOL_SEARCH_DIRECTORY_TENANT_IDS means off; comma-separated IDs or * mirror existing gates.
## State
- Done: master/task ledgers and prior commit read; clean branch at 4686a326, no locks.
- Done: tenant/version gate, explicit user-text-key test, historical evidence and prompt measurement documented.
- Now: validated and ready for local commit handback.
- Next: handback; canary deployment remains separate release work.
## Links
- Upstream: CONTINUITY.md
- Related: docs/reference/openclaw-tool-arguments.md
- Downstream: none
## Open questions
- None blocking; production envelope remains UNCONFIRMED.
## Working set
- apps/orchestrator/config_generator.py; config/settings/base.py; apps/orchestrator/tests.py
- runtime/openclaw/redact-stdout.test.mjs; docs/reference/openclaw-tool-arguments.md
## Notes
- Exact 2026.9.4 package and Node24 from Round 1 are available in ignored .scratch.
- Gate mirrors USAGE_HOOKS_TENANT_IDS parsing/logging; off path keeps main's tools mode.
- Privacy recheck: production redactor unchanged and ungated; only fixed keys/types, no values. Tokyo trip key masks to other:string at outer and args levels.
- Exact 9.4 source shows BOTH modes get the directory system-prompt block; description caps 180 -> 64 -> 0, then row omission; 18K-character maximum with context-based shrinkage.
- Real 87-tool plugin sample at 128K/256K context: tools 9,928 chars / 2,408 cl100k tokens; directory 9,813 / 2,388. Delta -115 chars / ~20 tokens saved. At 32K, delta +21 proxy tokens from three more names fitting. Tokenizer is a proxy, not DeepSeek billing.
- Measurement script: scripts/measure_openclaw_tool_directory.mjs; imports real plugins and exact formatter; control schemas equal. Initial exploratory registration stub lacked api.logger for agenda; narrowed final sample to four documented plugin sets (no execution/API calls).
- PASS: DATABASE_URL=postgres://michaeljones@127.0.0.1:5432/postgres make test-local TESTS='apps.orchestrator.tests apps.orchestrator.test_tool_policy apps.orchestrator.test_config_security apps.orchestrator.test_config_write_validation apps.orchestrator.test_openclaw_round_four apps.orchestrator.test_openclaw_round_nine' — 198 tests.
- PASS: NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL=1 node --test runtime/openclaw/redact-stdout.test.mjs runtime/openclaw/tool-logger.test.js — 152 tests. Updated only test-file path hash.
- PASS: OPENCLAW_STATE_DIR="$PWD/.scratch/contract-state" .scratch/node24/node_modules/.bin/node scripts/verify_openclaw_tool_arguments.mjs .scratch/runtime/node_modules/openclaw.
- PASS: same isolated Node24 invocation of scripts/measure_openclaw_tool_directory.mjs; cl100k_base counts via local tiktoken.
- PASS: .venv/bin/ruff check .; .venv/bin/ruff format --check . (1837 files); git diff --check.
