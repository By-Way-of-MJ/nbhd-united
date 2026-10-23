// Registration + schema contract for nbhd-project-tools.
//   node --test runtime/openclaw/plugins/nbhd-project-tools/index.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import register from "./index.js";

function tools() {
  const out = {};
  register({ pluginConfig: {}, registerTool(tool) { out[tool.name] = tool; } });
  return out;
}

test("registers exactly the three read/draft/propose tools", () => {
  assert.deepEqual(Object.keys(tools()).sort(), ["nbhd_project_context", "nbhd_project_draft", "nbhd_project_propose_change"]);
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
