// Registration + schema contract for nbhd-project-tools.
//   node --test runtime/openclaw/plugins/nbhd-project-tools/index.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import register from "./index.js";

function tools() {
  const out = {};
  register({ pluginConfig: {}, registerTool(tool) { if (typeof tool === "function") tool = tool({}); out[tool.name] = tool; } });
  return out;
}

test("registers project tools and the four private prep tools", () => {
  assert.deepEqual(Object.keys(tools()).sort(), ["nbhd_project_context", "nbhd_project_draft", "nbhd_project_propose_change", "nbhd_step_prep_next", "nbhd_step_prep_report", "nbhd_step_prep_save", "nbhd_step_prep_update_next"]);
});

test("manifest contract lists the same tools", () => {
  const manifest = JSON.parse(readFileSync(new URL("./openclaw.plugin.json", import.meta.url)));
  assert.deepEqual([...manifest.contracts.tools].sort(), Object.keys(tools()).sort());
});

test("no tool can apply, approve, publish, ask or complete", () => {
  const names = Object.keys(tools()).join(" ");
  for (const verb of ["approve", "publish", "complete", "respond", "invite", "message", "apply"]) {
    assert.ok(!names.includes(verb), `unexpected capability: ${verb}`);
  }
});

test("descriptions carry the untrusted-text rule", () => {
  const t = tools();
  assert.match(t.nbhd_project_context.description, /untrusted/);
  assert.match(t.nbhd_project_propose_change.description, /never approve/);
});

test("descriptions say drafts and suggestions live in the Mission tab", () => {
  const t = tools();
  assert.match(t.nbhd_project_draft.description, /Draft tile under Projects in the Mission tab/);
  assert.match(t.nbhd_project_draft.description, /NBHD app's Mission tab/);
  assert.match(t.nbhd_project_propose_change.description, /on the project in the Mission tab, or on the Projects card in chat/);
  assert.match(t.nbhd_project_propose_change.description, /NBHD app's Mission tab/);
});

test("context describes shared notes and excludes photo bytes and private member notes", () => {
  const description = tools().nbhd_project_context.description;
  assert.match(description, /newest three shared notes/);
  assert.match(description, /untrusted fenced text and photo counts/);
  assert.match(description, /photo bytes are never included/);
  assert.match(description, /Private notes from other members are not included/);
});

test("all tools register as optional without hooks or cron context", () => {
  const names = [];
  register({ pluginConfig: {}, registerTool(tool, options) {
    assert.equal(typeof tool, "object");
    assert.deepEqual(options, { optional: true });
    names.push(tool.name);
  } });
  assert.deepEqual(names.sort(), Object.keys(tools()).sort());
});

test("prep schemas separate reads and constrain saves and reports", () => {
  const t = tools();
  for (const name of ["nbhd_step_prep_next", "nbhd_step_prep_update_next"]) {
    assert.deepEqual(t[name].parameters, { type: "object", additionalProperties: false, properties: {} });
  }
  const save = t.nbhd_step_prep_save.parameters;
  assert.deepEqual(save.required, ["id", "run_token", "body"]);
  assert.deepEqual(Object.keys(save.properties), ["id", "run_token", "body"]);
  assert.equal(save.properties.body.maxLength, 50000);
  assert.equal(save.additionalProperties, false);
  assert.deepEqual(t.nbhd_step_prep_report.parameters.properties.status.enum, ["ready", "stuck", "needs_choice"]);
  assert.equal(t.nbhd_step_prep_report.parameters.properties.doc_slug, undefined);
  assert.equal(t.nbhd_step_prep_report.parameters.additionalProperties, false);
});

test("each prep tool forwards only to its authenticated endpoint without hook state", async (t) => {
  for (const [key, value] of Object.entries({ NBHD_API_BASE_URL: "https://test.invalid", NBHD_TENANT_ID: "test-tenant", NBHD_INTERNAL_API_KEY: "test-internal-key" })) {
    const previous = process.env[key];
    process.env[key] = value;
    t.after(() => { if (previous === undefined) delete process.env[key]; else process.env[key] = previous; });
  }
  const toolsByName = tools();
  for (const [name, suffix, body] of [
    ["nbhd_step_prep_next", "next", {}],
    ["nbhd_step_prep_update_next", "update-next", {}],
    ["nbhd_step_prep_save", "save", { id: "prep-test", run_token: "signed", body: "Private prep" }],
    ["nbhd_step_prep_report", "report", { id: "prep-test", run_token: "signed", status: "ready", last_seen_note_at: null }],
  ]) {
    t.mock.method(globalThis, "fetch", async (url, options) => {
      assert.equal(String(url), `https://test.invalid/api/v1/integrations/runtime/test-tenant/step-prep/${suffix}/`);
      assert.equal(options.headers["X-NBHD-Tenant-Id"], "test-tenant");
      assert.equal(options.headers["X-NBHD-Internal-Key"], "test-internal-key");
      assert.equal(options.method, "POST");
      assert.deepEqual(JSON.parse(options.body), body);
      return { ok: true, text: async () => JSON.stringify({ job: null }) };
    });
    const response = await toolsByName[name].execute("test-call", body);
    assert.deepEqual(response.details.json, { job: null });
    t.mock.restoreAll();
  }
});
