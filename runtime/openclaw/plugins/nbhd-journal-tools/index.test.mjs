// Timeout-transport tests for nbhd-journal-tools.
//   node --test runtime/openclaw/plugins/nbhd-journal-tools/index.test.mjs
//
// A write that timed out may still have committed on the server. The thrown
// message must tell the assistant to check current state before reporting
// failure; read timeouts keep the older, shorter text.
import test from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

const GET_TIMEOUT = "NBHD runtime request timed out after 20000ms";
const WRITE_TIMEOUT =
  "NBHD runtime request timed out after 20000ms. The server may still have saved this change. Check the current state before telling the user it failed, and do not repeat it blindly.";

function collectTools() {
  const tools = {};
  register({
    pluginConfig: {},
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

test("PATCH timeout says the server may still have saved the change", async (t) => {
  setRuntimeEnv(t);
  abortFetch(t);
  const tool = collectTools().nbhd_update_profile;
  await assert.rejects(
    tool.execute("patch-timeout", { display_name: "New Name" }),
    (error) => {
      assert.equal(error.message, WRITE_TIMEOUT);
      return true;
    },
  );
});

test("GET timeout keeps the old message", async (t) => {
  setRuntimeEnv(t);
  abortFetch(t);
  const tool = collectTools().nbhd_document_get;
  await assert.rejects(
    tool.execute("get-timeout", { kind: "daily", slug: "2026-11-01" }),
    (error) => {
      assert.equal(error.message, GET_TIMEOUT);
      return true;
    },
  );
});
