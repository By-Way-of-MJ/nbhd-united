# OpenClaw 9.4 Tool Search argument investigation

## Finding and confidence

**The exact fleet root cause is not proven.** The supplied production logs show
target-schema rejection, but redact the call envelope. All required fields being
missing does **not** prove that the input was `{}`: `{params: {message: "…"}}`
also lacks the required top-level `message`. We reproduced both cases offline.
We did not reproduce a valid `args` object being emptied by 9.4, or establish a
5.28 → 9.4 argument-loss regression. No production model call was made.

The config change is a canary mitigation: only tenants allowlisted in
`TOOL_SEARCH_DIRECTORY_TENANT_IDS` **and** on >=2026.9.4 use Tool Search
`directory` mode, allowing direct calls to exact deferred tool names while
retaining the policy-filtered catalog and schema compaction. Generic `tool_call` remains
available, so the change cannot guarantee that every model avoids that envelope.
Confirm its effect with production shapes, direct-call success, and retry rates.

## Historical production evidence (reviewer-supplied)

Weekly counts supplied for Round 2, not independently queried in this task:

| Week starting (2026) | NBHD `missing required parameter(s)` | Core `tool_call failed: Invalid arguments` | Combined |
| --- | ---: | ---: | ---: |
| 09-14 | 894 | 11 | 905 |
| 09-21 | 2,684 | 309 | 2,993 |
| 09-28 | 58 | 748 | 806 |
| 10-05 | 46 | 2,126 | 2,172 |
| 10-12 | 24 | 3,677 | 3,701 |

The old message is our own required-field guard at
`runtime/openclaw/tool-logger.js:150`, already present on the 5.28 execution
path. The reviewer reports this missing-required-arguments pattern, usually all
required fields at once, has persisted for months. The supplied five weeks
show the same failure class moving from our guard to 9.4's earlier catalog
validator (`local-model-lean-Ct7p3GNL.mjs:1449`), rather than starting with 9.4.
This supports a longstanding model-side argument/envelope-shape mistake and
contradicts treating the new core message as evidence of a 9.4 argument-loss
regression. Counts are not traffic-normalized rates, and neither message proves
the exact raw envelope; empty args versus alternate nesting remains unconfirmed.

## Exact package evidence

Paths below are relative to the named npm package root. Both packages were
obtained with `npm pack openclaw@<version>` into git-ignored `.scratch/`.

| Package / file:line | Finding |
| --- | --- |
| 5.28 `dist/agent-tools-DkIWbsdu.js:1824` | `tool_call` advertises `{id: string, args?: Record<string, unknown>}`. |
| 5.28 `dist/agent-tools-DkIWbsdu.js:1406` | Reads `params.args ?? params.input ?? {}`; no `arguments` or `params` alias and no nested JSON-string parser. |
| 5.28 `dist/agent-tools-DkIWbsdu.js:1454` | Hands `input ?? {}` to `tool.execute`; no catalog input-schema validator here. |
| 9.4 `dist/local-model-lean-Ct7p3GNL.mjs:2183` | Same object-valued `args` schema, same `id`; no key rename. |
| 9.4 `dist/local-model-lean-Ct7p3GNL.mjs:1368` | Reads `args ?? input`; also accepts `args.*` fields and flattened target fields when nested input is absent/empty. Does not parse nested JSON strings or unwrap `params`/`arguments`. |
| 9.4 `dist/local-model-lean-Ct7p3GNL.mjs:1409` | `prepareArguments` repairs an outer `args`/`input` wrapper containing the selector. |
| 9.4 `dist/local-model-lean-Ct7p3GNL.mjs:1449` | New catalog input-schema check emits the exact `Invalid arguments for tool ... Expected input ...` error. |
| 9.4 `dist/local-model-lean-Ct7p3GNL.mjs:1566` | Carries `input ?? {}` into execution; `:1626` validates final post-hook input. |
| 9.4 `dist/agent-tools.before-tool-call-WtmCO7BO.mjs:3344` | Final input validation runs before the underlying implementation at `:3397`. NBHD tool-body logging cannot see rejected calls. |
| 9.4 `dist/zod-schema.agent-runtime-Ca6cjqf9.mjs:469` | Supported config modes: `code`, `tools`, `directory`; no per-tool direct list in this config object. 5.28's `dist/zod-schema.agent-runtime-BMFszaAv.js:440` accepts only `code`/`tools`. |
| 9.4 `dist/local-model-lean-Ct7p3GNL.mjs:1901` | Directory mode compacts target schemas; `:1951` resolves unique exact names from the authorized catalog. |
| 9.4 `dist/builtin-openclaw-B-H-7lKk.mjs:18208` | Wires deferred name resolution to normal tool adapters and logs `tool-search: hydrated deferred directory tool <name>`. |
| 9.4 `dist/agent-core-B_87jlHI.mjs:984` | Resolves deferred tools before admission; `:1079` validates their actual schemas. |
| 9.4 `dist/agent-tool-definition-adapter-DhccrrY9.mjs:101` | Failure log captures raw and changed effective params. Preview is capped at 600 characters (`:22`, `:48`) before NBHD redaction. |

The pinned TypeBox versions (1.1.38 in 5.28, 1.3.18 in 9.4) produce identical
JSON schemas for this `Type.Record` declaration. The 9.4 OpenRouter/DeepSeek
normalizer preserves that schema and valid `args` objects in the offline probe.
`@openclaw/ai@2026.9.4`, `dist/openai-tool-schema-CzjyYXun.mjs:826`, and
`dist/openai-completions-stream-Da2vvl-S.mjs:954` contain that provider path.
This does not establish what an upstream OpenRouter endpoint generated.

Although the dispatcher itself does not parse strings, 9.4's earlier admission
validator does: `@openclaw/ai` `dist/validation-BDzVDnTs.mjs:90` parses a JSON
object string, with a bounded length, and `:185` applies that coercion before
validation. The probe confirms serialized `args` objects survive and malformed/
array/null strings reject. 5.28 `dist/llm-D4M0S4vd.js:61` lacks this object-string
coercion. This change does not explain an object becoming empty.

## Our code

- `runtime/openclaw/plugins/nbhd-journal-tools/index.js:2`: `wrap` delegates to
  the shared logger; `:30`'s `asObject` preserves objects and rejects strings/
  arrays. It runs inside the target implementation, after the failing validator.
- `runtime/openclaw/tool-logger.js:150` checks required fields, then `:165`
  invokes `originalExecute.apply(this, args)` unchanged. No envelope rewrite.
- Journal `registerTool(..., {optional: true})` (`index.js:215`) controls
  availability, not argument conversion. 9.4 `dist/tools-CulcXFDi.mjs:566`
  filters optional tools through policy; `:630` attaches metadata and retains
  the tool object.
- `apps/orchestrator/config_generator.py` keeps structured mode for 5.28–9.3
  and all unlisted tenants. Directory mode requires both >=9.4 and the tenant
  allowlist. No tool policy expansion; the off-path Tool Search object remains
  byte-identical to main: `{"enabled": true, "mode": "tools"}`.
- Defaults use DeepSeek Pro (`config_generator.py:1467`), with DeepSeek Flash
  for heartbeat (`:1740`); OpenRouter provider config is emitted in
  `generate_openclaw_config`. Tenant overrides can select other models.

No core patch or plugin argument coercion is justified by the available
evidence. Code mode requires executable JavaScript; disabling discovery would
restore the full per-turn schema cost. Directory mode is supported by the
deployed binary and requires no new image capability gate.

## Shape telemetry and production confirmation

`runtime/openclaw/redact-stdout.js` extracts shape from existing core invalid-
argument previews before masking them. Example suffixes:

```text
raw_params=<redacted> argument_shape=raw:object(id:string,args:object);raw.args:object()
raw_params=<redacted> argument_shape=raw:object(id:string,params:object);raw.params:object(message:string)
```

Diagnostics remain fleet-wide, independent of the directory-mode gate. Only
fixed-vocabulary schema key names and JS types (with explicit `array`/`null`
labels) survive. Unknown names become `other`; no values, nested content, tool selector values, or hashes are logged.
The outer envelope and the immediate target-argument object's top-level keys
are reported. The explicit user-text-key test checks `{"Tokyo trip": "x"}`
and its nested `args` form become `other:string`, with neither key text nor
value surviving. Existing tests cover values, deeper content, unknown keys,
output bounds, and malformed previews. Changed `effective_params` gets a
separate shape. Missing, truncated, or already-redacted previews report `unavailable`, never `{}`.
This is the adapter's input, not the original provider wire payload or the
target's final post-hook input; it cannot by itself localize every loss.

To canary exactly one tenant, set the Django/control-plane environment to:

```sh
TOOL_SEARCH_DIRECTORY_TENANT_IDS="<full-tenant-uuid>"
```

Replace the placeholder with that tenant's full UUID, with no other IDs. IDs
are comma-separated, whitespace-trimmed and case-insensitive; `*` opts in every
tenant, and unset/empty opts in nobody. The version check still excludes every
runtime below 2026.9.4, even with `*`. Apply through the normal tenant config
refresh; merely changing the setting does not mutate a running container.

For rollout, the usual image update is needed for new redactor diagnostics;
the directory config already works on unpatched 9.4. Verify
`tool-search: hydrated deferred directory tool nbhd_send_to_user` followed by
`[nbhd:tools] ok nbhd_send_to_user`, and measure whether blank-argument retries
per heartbeat fall. Those lines prove use/success of the bypass, not the exact
historical cause. Remaining `tool_call` failures should now distinguish empty
args from alternate nesting. No deployment was performed during this task.

## Per-turn prompt cost: directory versus tools on 9.4

**Both modes already include a compact directory in the system prompt.**
Switching to `directory` does not newly add an ~86-tool listing to 9.4's
`tools` mode. Source tracing in the exact package:

- `dist/tool-surface-plan-xPHTiYsA.mjs:1905` enables Tool Search controls
  independently of `tools` versus `directory` mode.
- `dist/builtin-openclaw-B-H-7lKk.mjs:20393` enables the directory prompt for
  either mode when the catalog is registered; `:19070` calls the formatter
  with the run's context-token budget.
- `dist/system-prompt-params-BOlFEPMI.mjs:804` inserts it under
  `### Deferred Tool Schemas`. This is per-request prompt context, not merely
  a one-time search response. An in-process formatting cache avoids rebuilding
  text; it does not remove it from subsequent model inputs.
- `dist/local-model-lean-Ct7p3GNL.mjs:1960–2015` formats alphabetically sorted,
  unique, policy-visible deferred OpenClaw tool names, plugin/core owner names,
  and whitespace-normalized descriptions. Example row from our real plugin:
  `- nbhd_constellation_notes (nbhd-journal-tools): Read the enriched context behind the user's constellation sta...`
- Full parameter schemas remain deferred in both modes; the visible
  `tool_search`, `tool_describe`, and `tool_call` schemas are identical in the
  measurement. Already-direct tools keep their normal schemas. MCP/client
  entries are not rendered as directory rows; a notice points to search.

The directory budget is **characters**, not tokens:
`min(18000, max(768, floor(contextTokenBudget / 10)))`, or 18,000 without a
positive context budget. Descriptions start at 180 characters, shrink globally
to 64, then disappear if needed. If names/owners still exceed the budget, rows
are removed from the end and an omitted-count/search hint is added. Duplicate
names and invalid identifiers are also excluded. Therefore it does not
unconditionally list every tool, and it does not send every full description.

Measured offline with `scripts/measure_openclaw_tool_directory.mjs`, invoking
the package's real catalog/formatter on **87 real plugin registrations** as a
representative sample for the ~86-tool fleet catalog: journal 51, finance 9,
Fuel 15, insights 12. No invented descriptions or production tenant data.
This sample is not an exact tenant inventory: feature gates, policy, core
primitives and optional integrations change the effective catalog.

| Context budget | Mode | Rows | Description cap | Directory chars | Estimated tokens (`cl100k_base`) |
| --- | --- | ---: | ---: | ---: | ---: |
| 262,144 or 131,072 | `tools` | 87 | 64 | 9,928 | 2,408 |
| 262,144 or 131,072 | `directory` | 87 | 64 | 9,813 | 2,388 |
| 32,768 | `tools` | 63 | 0 | 3,258 | 934 |
| 32,768 | `directory` | 66 | 0 | 3,273 | 955 |

At the larger budgets the rows are identical; only the footer changes from
mandatory `tool_call` envelope guidance to direct-name-or-wrapper guidance.
**Estimated incremental cost: -115 characters, approximately -20 tokens per
turn**, with roughly **2.4K directory tokens already present in either mode**.
At a tight budget, the shorter footer admits three more names in this sample,
producing +21 estimated tokens instead. This measures the catalog block,
excluding common system text, control schemas and conversation history.
`cl100k_base` is an explicitly approximate tokenizer proxy, not a DeepSeek
billing measurement. Actual provider tokenization, cache discounts, discovery
calls, tool-result history, and retries need canary usage data. Client-tool
surfaces can also differ between modes (`local-model-lean-Ct7p3GNL.mjs:2106`);
this estimate covers our native plugin catalog without client tools.

Reproduce the exact character counts and approximate token counts:

```sh
OPENCLAW_STATE_DIR="$PWD/.scratch/contract-state" .scratch/node24/node_modules/.bin/node scripts/measure_openclaw_tool_directory.mjs .scratch/runtime/node_modules/openclaw > .scratch/tool-directory-cost.json
.venv/bin/python - <<'PYTOKENS'
import json
from pathlib import Path
import tiktoken  # analysis-only local dependency; no application change
enc = tiktoken.get_encoding("cl100k_base")
for row in json.loads(Path(".scratch/tool-directory-cost.json").read_text())["measurements"]:
    print(row["contextTokenBudget"], row["mode"], row["listedRows"],
          row["chars"], len(enc.encode(row["prompt"])))
PYTOKENS
```

## Reproduce offline

Install the exact package in an ignored scratch directory with dependencies
(`npm install --ignore-scripts --omit=dev --prefix .scratch/runtime openclaw@2026.9.4`).
Use Node 24.16+ as required by this package:

```sh
OPENCLAW_STATE_DIR="$PWD/.scratch/contract-state" node scripts/verify_openclaw_tool_arguments.mjs .scratch/runtime/node_modules/openclaw
NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL=1 node --test runtime/openclaw/redact-stdout.test.mjs runtime/openclaw/tool-logger.test.js
```

The probe imports the real 9.4 catalog, schema validator, provider normalizer,
plugin metadata, and execution adapters; only the final tool implementation is
a recording fake. It proves valid canonical/alias/flat inputs survive, omitted/
wrong envelopes reject before plugin execution, and directory calls preserve
arguments while retaining validation, policy filtering, and schema compaction.
It intentionally does not claim to reproduce the unknown production envelope.

Validation on this branch:

```sh
DATABASE_URL=postgres://michaeljones@127.0.0.1:5432/postgres make test-local TESTS='apps.orchestrator.tests apps.orchestrator.test_tool_policy apps.orchestrator.test_config_security apps.orchestrator.test_config_write_validation apps.orchestrator.test_openclaw_round_four apps.orchestrator.test_openclaw_round_nine'
NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL=1 node --test runtime/openclaw/redact-stdout.test.mjs runtime/openclaw/tool-logger.test.js
OPENCLAW_STATE_DIR="$PWD/.scratch/contract-state" .scratch/node24/node_modules/.bin/node scripts/verify_openclaw_tool_arguments.mjs .scratch/runtime/node_modules/openclaw
.venv/bin/ruff check .
.venv/bin/ruff format --check .
git diff --check
```

Round 2 results: 198 Django tests pass; 152 Node tests pass; exact-runtime probe passes;
Ruff and diff checks pass. Round 2 updates only the redactor test-file hash in
`automatic_paths_main.json`; the production redactor stays unchanged and ungated.
The directory-cost probe also passes and confirms identical control schemas.
No plugin implementation was changed. No Docker build, model call, or production
verification was performed.
