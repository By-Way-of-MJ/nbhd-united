# Jev background routing

App chat decisions run in `apps/router/pending_queue.py::_drain_ios_batch`,
after context annotation and before the container POST. No Jev work runs in
`enqueue_tenant_turn`, so the ingress POST does not wait for classification.
The existing queue carries the full redacted message in `PendingMessage.user_text`
(`chat_views.py:827`); it is separate from displayed `AppChatMessage.user_text`
(`chat_views.py:669`). The existing attachment framing/redaction separation is
documented at `chat_views.py:728`.

`apps/router/background_route.py` follows the snapshot, bounded admission,
deadline, checked ephemeral redaction, and fail-safe pattern in
`apps/router/talk_route.py:124` and `:150`. Only `subagents_enabled(tenant)`
tenants can invoke Jev. One typed choice question receives only `latest_message`.
The selected background probability must be at least 0.7; the total budget is
1.2 seconds, including redaction. Errors, saturation, unconfirmed redaction,
and low background probability yield inline. Logs contain only tenant prefix,
effective decision, selected-choice probability, latency, and a fixed reason.

For coalesced turns, only the latest message is classified; earlier messages
remain context. The note explicitly targets the latest request. Any image or
document flag in the batch bypasses Jev, including upload failures. Internal
turns without a client message ID bypass it too. Retries classify again without
mutating the queue payload, so notes cannot accumulate across attempts.

The exact note, appended after all PII processing, is:

```text
[NBHD routing: run the latest request as a background helper — call sessions_spawn first with a bounded task, reply with the short acknowledgement, and end your turn.]
```

It is model-bound framing only, never a stored user message or app-feed field.
The gated persona follows this note and explicitly requested background work,
while preserving helper bounds, immediate acknowledgement, and closure rules.

## Tool visibility evidence (OpenClaw 2026.9.4)

Source: extracted npm package at
`/tmp/nbhd-spawn-budget-openclaw-2026.9.4/package/dist`.

- `zod-schema.agent-runtime-Ca6cjqf9.mjs:469–479`: strict `ToolSearchSchema`
  accepts only enabled, mode, codeTimeoutMs, searchDefaultLimit, maxSearchLimit.
  There is no per-tool pin/always-expose setting.
- `tool-surface-plan-xPHTiYsA.mjs:1910–1921`: `applyAgentToolSurfaceCatalog`
  constructs `directToolNames` internally as `["message"]` or `[]`; it does
  not read a configurable list of tool names.
- `local-model-lean-Ct7p3GNL.mjs:244–246`: direct visibility requires a trusted
  OpenClaw tool and either that internal list or a core coding primitive.
- `local-model-lean-Ct7p3GNL.mjs:2093–2101`: structured Tool Search uses this
  visibility predicate when compacting the catalog.
- `builtin-openclaw-B-H-7lKk.mjs:19528`: the observed "cataloged ... tools
  behind compact prompt surface" boot log comes from this compaction path.

Therefore config-only pinning of `sessions_spawn` is unsupported. Tool Search
configuration is unchanged; the persona still says to find `sessions_spawn`
via tool search if it is not loaded. No runtime patch is needed for this change.

## Evaluation and rollout

Run `python manage.py jev_background_eval` with `OPENROUTER_API_KEY` configured
and the normal PII detector available. This is a live network command, separate
from unit tests. The fixture contains 20 English and 10 Japanese cases, evenly
split between background and inline, including own-data comparisons, reminders,
attachments, weather, and injection wording. It prints case labels, accuracy,
expected/actual confusion counts, p50 latency, and reason counts, without prompt
contents. Unavailable decisions count as incorrect rather than inflating inline
accuracy. Two attachment cases test the bypass; the other 28 reach Jev when
redaction succeeds. Redactor warmup is outside measured routing latency.

Rollout requires the Django/control-plane deploy plus a tenant config apply
(or targeted `refresh_persona_agents_md --tenant <uuid>`) to refresh the gated
AGENTS.md text. The refresh command documents this delivery path at
`apps/orchestrator/management/commands/refresh_persona_agents_md.py:11–29`.
No OpenClaw image roll is required, assuming the tenant already has the helper
closure runtime and existing `SUBAGENT_TENANT_IDS` configuration.

Parity manifests `automatic_paths_main.json` and `cron_request_paths_main.json`
do not include any source file changed here and are intentionally unchanged.
Gated persona assertions are updated; the ungated prompt hash remains unchanged.

## Verification (2026-10-17)

358 tests passed via:

```sh
NBHD_TEST_ENV=/Users/michaeljones/Projects/nbhd-united/.env scripts/test-local.sh \
  apps.router.test_background_route apps.router.test_pending_queue \
  apps.router.test_ios_chat apps.router.test_chat_shape apps.router.test_talk_route \
  apps.orchestrator.test_subagent_offload apps.orchestrator.test_rules_delivery \
  apps.orchestrator.test_reminder_capability apps.orchestrator.test_personas_workspace_files \
  apps.orchestrator.test_workspace_rules apps.orchestrator.test_refresh_persona_agents_md \
  apps.orchestrator.test_openclaw_round_four apps.orchestrator.test_openclaw_round_nine
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Repository-wide lint and formatting passed (1,837 Python files). No runtime
files changed, so no Node test run was needed. No parity hashes were repinned.

The live eval warmed redaction successfully, but all 28 Jev requests were
unavailable; a status-only diagnostic confirmed HTTP 401. The two attachment
bypasses passed. The command reported 2/30 effective correct cases and 158.5 ms
p50, reflecting failed requests, **not classifier quality or successful Jev
latency**. Rerun with a valid API key before assessing semantic accuracy.
