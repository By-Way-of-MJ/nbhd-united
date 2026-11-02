// Timeout-transport tests for nbhd-agenda-tools.
//   node --test runtime/openclaw/plugins/nbhd-agenda-tools/index.test.mjs
//
// A write that timed out may still have committed on the server. The thrown
// message must tell the assistant to check current state before reporting
// failure, and not to repeat the write blindly.
import test from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

const WRITE_TIMEOUT =
  "NBHD runtime request timed out after 20000ms. The server may still have saved this change. Check the current state before telling the user it failed, and do not repeat it blindly.";

function collectTools() {
  const tools = {};
  register({
    pluginConfig: { requestTimeoutMs: 20000 },
    registerTool(definition) {
      tools[definition.name] = definition;
    },
    registerHook() {},
    on() {},
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  return tools;
}

function setRuntimeEnv(t) {
  const previous = {
    base: process.env.NBHD_API_BASE_URL,
    tenant: process.env.NBHD_TENANT_ID,
    key: process.env.NBHD_INTERNAL_API_KEY,
  };
  process.env.NBHD_API_BASE_URL = "https://nbhd.test";
  process.env.NBHD_TENANT_ID = "tenant-test";
  process.env.NBHD_INTERNAL_API_KEY = "test-key";
  t.after(() => {
    for (const [name, value] of [
      ["NBHD_API_BASE_URL", previous.base],
      ["NBHD_TENANT_ID", previous.tenant],
      ["NBHD_INTERNAL_API_KEY", previous.key],
    ]) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });
}

function abortFetch(t) {
  t.mock.method(globalThis, "fetch", async () => {
    const error = new Error("mock abort");
    error.name = "AbortError";
    throw error;
  });
}

test("commitment write timeout says the server may still have saved the change", async (t) => {
  setRuntimeEnv(t);
  abortFetch(t);
  const tool = collectTools().nbhd_record_commitment;
  await assert.rejects(
    tool.execute("commitment-timeout", {
      about: "check in on the new running plan",
      surface_after: "2026-11-10T09:00:00Z",
      why: "user asked to revisit it next week",
    }),
    (error) => {
      assert.equal(error.message, WRITE_TIMEOUT);
      return true;
    },
  );
});
