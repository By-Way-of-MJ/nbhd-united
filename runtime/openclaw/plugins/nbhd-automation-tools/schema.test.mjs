// Schema-contract tests for nbhd-automation-tools cron-create tools.
//   node --test runtime/openclaw/plugins/nbhd-automation-tools/schema.test.mjs
//
// Pins the 2026.9.4 fix: every origin-stamped cron-create tool must DECLARE
// _nbhd_origin in its schema. 2026.9.4 strict-validates tool input against the
// tool schema AFTER nbhd-cron-enforcement's before_tool_call hook injects
// _nbhd_origin, so an undeclared property fails the whole call with
// "must not have additional properties: _nbhd_origin" — reminders / scheduled
// tasks then silently never get created. It must stay OPTIONAL: the hook, not
// the model, supplies it (and overwrites any model-provided value).
import { test } from "node:test";
import assert from "node:assert/strict";
import register from "./index.js";

const ORIGIN_STAMPED_TOOLS = [
  "nbhd_cron_create_pure_reminder",
  "nbhd_cron_create_quote_user_intent",
  "nbhd_cron_create_domain_summary",
  "nbhd_cron_cancel_reminder",
];

function collectTools(context = {}) {
  const tools = {};
  const api = {
    registerTool(def) {
      if (typeof def === "function") def = def(context);
      tools[def.name] = def;
    },
    registerHook() {},
    on() {},
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  };
  register(api);
  return tools;
}

test("every origin-stamped cron tool declares _nbhd_origin as an optional schema property (2026.9.4 strict validation)", () => {
  const tools = collectTools();
  for (const name of ORIGIN_STAMPED_TOOLS) {
    assert.ok(tools[name], `${name} should be registered`);
    const params = tools[name].parameters;
    assert.equal(params.additionalProperties, false, `${name} keeps additionalProperties:false`);
    assert.equal(
      Object.hasOwn(params.properties, "_nbhd_origin"),
      true,
      `${name} must declare _nbhd_origin or 2026.9.4 rejects the hook-stamped call`,
    );
    assert.equal(
      (params.required || []).includes("_nbhd_origin"),
      false,
      `${name} must keep _nbhd_origin optional (the hook supplies it)`,
    );
  }
});

test("reminder list and cancel schemas match manifest contracts", async () => {
  const { readFile } = await import("node:fs/promises");
  const manifest = JSON.parse(await readFile(new URL("./openclaw.plugin.json", import.meta.url), "utf8"));
  const tools = collectTools();
  assert.deepEqual(Object.keys(tools).sort(), manifest.contracts.tools.slice().sort());
  assert.equal(tools.nbhd_cron_list_reminders.parameters.properties.include_disabled.type, "boolean");
  assert.deepEqual(tools.nbhd_cron_cancel_reminder.parameters.required, ["cron_id"]);
  assert.equal(tools.nbhd_cron_cancel_reminder.parameters.properties.cron_id.type, "integer");
});
