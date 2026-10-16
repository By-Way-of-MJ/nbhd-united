# Background helper closure

The bridge now registers a Postgres `HelperRun` **before** allowing
`sessions_spawn`. Registration failure blocks the spawn and tells the parent
to answer inline. No task text or result is stored in the admission record.
Admission retries use a tenant/session/parent-run/tool-call hash and do not
extend the deadline. Helpers are restricted to the app-thread sessions that
the completion bridge can route.

`subagent_ended` provides an immediate failure notice when available. The
minute-level `close_expired_helpers` QStash system task covers missing hooks,
missing parent announcements, transcript persistence errors, and container
crashes/restarts. The deadline is 300 seconds plus 180 seconds of grace
(120 seconds for the configured announcement budget and 60 for delivery).
Absent an earlier failure hook, a lost completion normally closes within
8–9 minutes of admission. Backend/database/scheduler outages delay delivery;
pending records retry on later sweeps. Inactive users/tenants remain pending
until eligible again; deleting the tenant/thread deletes its obligations.

The runtime-generated label carries `[nbhd-helper:<48 hex digits>]` before
the original title. OpenClaw preserves that label in the completion event's
`task:` line, allowing a new parent run or restarted bridge to recover the same
occurrence key without in-memory mappings. The bridge injects **both**
`occurrence_key` and `job_name=_subagent_result` into model sends. Older
in-flight helpers without the admission label retain the legacy delivery path;
they cannot retroactively receive the new watchdog guarantee.

Result delivery, hook failure, and watchdog failure all lock the same
`HelperRun` row. The existing proactive app-feed writer and `closed_at`/`closed_by` updates
commit in one transaction. A crash rolls back both; a lost HTTP response after
commit is a duplicate on retry. A failure notice closes the row as `failure`;
one later result is still delivered, without a prefix, and changes closure to
`result`. Once a result is delivered, all further deliveries are duplicates.
Failure notices after either kind of closure create no message. Concurrent
sends follow the same rules under the row lock: if failure wins, the answer
can follow it; if the result wins, the failure is suppressed.
APNs dispatch is deferred until commit;
the app-feed row is the durable user-visible delivery, independent of push
permission or linked Telegram/LINE accounts. This avoids relying on the
older `DeliveryAttempt` transport claim, which cannot atomically commit an
external network send with its acknowledgement. Eval sinks remain transport-free.

## Pinned runtime evidence

Examined the local extracted **npm `openclaw@2026.9.4`** package. The source
archive URL, exact excerpts, line numbers, and SHA-256 digests of the original
bundle files are preserved in
[`helper_runtime_94.json`](../../apps/orchestrator/fixtures/helper_runtime_94.json).
Relevant bundled sources and their original source regions:

- `dist/agent-harness-runtime-BvaKEkqR.d.ts:1800–1881`: actual
  `PluginHookSubagentContext`, `PluginHookSubagentSpawnedEvent`, and
  `PluginHookSubagentEndedEvent`. `subagent_ended` carries `runId`,
  `targetSessionKey`, `targetKind`, and `outcome`; context includes
  `requesterSessionKey`. It has no successful result text.
- `dist/subagent-completion-admission.store-9z9LjJoT.mjs:118–152`:
  `emitSubagentEndedHookOnce` invokes the hook and persists
  `endedHookEmittedAt` in OpenClaw's registry. This is useful for fast closure
  but cannot independently cover failure/loss of that registry or a container
  that never returns. Hence the backend watchdog supplements the proven hook.
- `dist/sessions-spawn-tool-2GiVSHqU.mjs:2068–2088`:
  `src/agents/subagents/spawn/subagent-spawn-lifecycle.ts` emits the actual
  label, child run ID, and requester session context.
- `dist/subagent-announce-Demk1EmD.mjs:234` chooses
  `params.label || params.task || "task"`; and
  `dist/subagent-announce-delivery-Dcn7g21F.mjs:196–216` formats it as `task:`.
- `dist/zod-schema-Q1KXOooO.mjs:267–279`: the strict subagents schema accepts
  `runTimeoutSeconds` (nonnegative integer) and `announceTimeoutMs` (positive
  integer). Generated `agents.defaults.subagents.runTimeoutSeconds` changes
  from 600 to **300**. Admission also caps per-call `runTimeoutSeconds` at 300;
  model-supplied zero (unlimited) cannot bypass the deadline.
- `dist/zod-schema.agent-runtime-Ca6cjqf9.mjs:468`:
  `ToolLoopDetectionSchema = object({ enabled: boolean().optional() }).strict().optional()`.
  **No configurable warning/critical/circuit-breaker threshold exists in 9.4.**
  `dist/legacy-fR_P797G.mjs:2976–3030` lists the removed threshold fields.
  Keep generated `tools.loopDetection = {enabled: true}`; do not emit invalid
  threshold keys. `src/agents/tool-loop-detection.ts`, bundled in
  `dist/tool-loop-detection-CJSZExqJ.mjs`, hardcodes history=30, critical=20,
  circuit breaker=30. Critical generic/ping-pong paths require recorded
  no-progress outcome evidence; warning counts alone do not prove that
  condition. Changing escalation for the observed missing-argument loop
  requires an upstream runtime fix, outside this config change.

## Rollout and verification

1. Deploy Django and migrate `router.0037_helperrun`.
2. Run the existing `register_system_crons` deployment step to register the
   every-minute watchdog. The existing daily reconciliation repairs drift.
3. Roll the OpenClaw **image** for the changed bridge plugin and apply the
   generated configuration for the 300-second default. Config apply alone
   cannot install the plugin code. Keep `SUBAGENT_TENANT_IDS` as the rollout gate.

Node tests cover pre-launch persistence, unavailable admission, retries,
wrapped tool calls, timeout caps, actual hook payloads, and a restarted parent
recovering the same key. Django tests cover admission authentication/scoping,
missing runtime events, retries, late results, concurrent delivery, rollback
between feed write and closure, and post-commit notifications. The automatic
runtime parity fixture is repinned only for the changed bridge files and
orchestrator task wrapper; other historical pins are preserved.

Validation completed locally:

- 185 Node tests passed across the bridge, journal-tool schema, cron
  enforcement, and document-taint guard suites (34 bridge tests).
- 217 Django tests passed via `scripts/test-local.sh`: helper runs, cron
  delivery, proactive push/context, placeholder-at-rest delivery, subagent
  config/usage, config write/schema validation, automatic-path parity, and
  system-cron registry suites.
- `ruff check` and `ruff format --check`: all 12 changed Python files clean.
- `manage.py makemigrations --check --dry-run`: no changes detected.
- `git diff --check`: clean. No commit, push, or deployment.

Round-2 validation (late results after failure closure):

- `scripts/test-local.sh apps.router.test_helper_runs apps.router.test_cron_delivery apps.router.test_cron_delivery_placeholder_at_rest`:
  70 tests passed on a fresh isolated local Postgres database, including the
  amended migration, concurrent late results, and rollback recovery.
- `ruff check .` and `ruff format --check .`: passed (1833 Python files formatted).
- `manage.py makemigrations --check --dry-run`: no changes detected.
- `git diff --check`: clean. Round-1 changes retained; no commit or push.

Changed files:

- `runtime/openclaw/plugins/nbhd-journal-tools/subagent-bridge/{index.js,test.js}`
- `apps/router/{helper_runs.py,models.py,cron_delivery.py,proactive_context.py,test_helper_runs.py}`
- `apps/router/migrations/0037_helperrun.py`
- `apps/integrations/urls.py`
- `apps/orchestrator/{config_generator.py,tasks.py,test_subagent_offload.py}`
- `apps/orchestrator/fixtures/{automatic_paths_main.json,helper_runtime_94.json}`
- `apps/cron/views.py`
- `apps/cron/management/commands/register_system_crons.py`
- `docs/reference/helper-closure.md`
