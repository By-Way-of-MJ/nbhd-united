# OpenClaw 9.4 Tool Search argument investigation

## Finding and confidence

**The exact fleet root cause is not proven.** The supplied production logs show
target-schema rejection, but redact the call envelope. All required fields being
missing does **not** prove that the input was `{}`: `{params: {message: "…"}}`
also lacks the required top-level `message`. We reproduced both cases offline.
We did not reproduce a valid `args` object being emptied by 9.4, or establish a
5.28 → 9.4 argument-loss regression. No production model call was made.

The config change is a mitigation: on >=2026.9.4 use Tool Search `directory`
mode, allowing direct calls to exact deferred tool names while retaining the
policy-filtered catalog and schema compaction. Generic `tool_call` remains
available, so the change cannot guarantee that every model avoids that envelope.
Confirm its effect with production shapes, direct-call success, and retry rates.

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
- `apps/orchestrator/config_generator.py:2411` keeps structured mode for
  5.28–9.3 and selects directory mode for >=9.4. No tool policy expansion.
- Defaults use DeepSeek Pro (`config_generator.py:1452`), with DeepSeek Flash
  for heartbeat (`:1725`); OpenRouter provider config is emitted in
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

Only allowlisted schema key names and types survive. Unknown names become
`other`; no values, nested content, tool selector values, or hashes are logged.
The outer envelope and the immediate target-argument object's top-level keys
are reported. Changed `effective_params` gets a separate shape. Missing,
truncated, or already-redacted previews report `unavailable`, never `{}`.
This is the adapter's input, not the original provider wire payload or the
target's final post-hook input; it cannot by itself localize every loss.

For rollout, the usual image update is needed for new redactor diagnostics;
the directory config already works on unpatched 9.4. Verify
`tool-search: hydrated deferred directory tool nbhd_send_to_user` followed by
`[nbhd:tools] ok nbhd_send_to_user`, and measure whether blank-argument retries
per heartbeat fall. Those lines prove use/success of the bypass, not the exact
historical cause. Remaining `tool_call` failures should now distinguish empty
args from alternate nesting. No deployment was performed during this task.

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

Results: 196 Django tests pass; 151 Node tests pass; exact-runtime probe passes;
Ruff and diff checks pass. The initial Django run identified the two modified
redactor path hashes; only those entries in `automatic_paths_main.json` changed.
No plugin implementation was changed. No Docker build, model call, or production
verification was performed.
