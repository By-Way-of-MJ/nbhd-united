import test from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

function buildApi() {
  const tools = new Map();
  const api = {
    pluginConfig: { apiBaseUrl: "https://nbhd.test" },
    registerTool(tool) {
      tools.set(tool.name, tool);
    },
  };
  return { api, tools };
}

function mockResponse({ status = 200, payload = {} } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return JSON.stringify(payload);
    },
  };
}

test("nbhd_gmail_list_messages uses GET with tenant-scoped headers", async () => {
  process.env.NBHD_TENANT_ID = "tenant-1";
  process.env.NBHD_INTERNAL_API_KEY = "internal-key";

  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { messages: [], result_size_estimate: 0 } });
  };

  register(api);
  const tool = tools.get("nbhd_gmail_list_messages");
  assert.ok(tool, "tool should be registered");

  await tool.execute("1", { q: "in:inbox", max_results: 3 });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[0].options.headers["X-NBHD-Internal-Key"], "internal-key");
  assert.equal(calls[0].options.headers["X-NBHD-Tenant-Id"], "tenant-1");
  assert.match(calls[0].url, /\/api\/v1\/integrations\/runtime\/tenant-1\/gmail\/messages\/\?q=in%3Ainbox&max_results=3$/);
});

test("registers exactly 4 Google tools (no journal duplicates)", () => {
  process.env.NBHD_TENANT_ID = "tenant-check";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  register(api);

  const expected = [
    "nbhd_gmail_list_messages",
    "nbhd_gmail_get_message_detail",
    "nbhd_calendar_list_events",
    "nbhd_calendar_get_freebusy",
  ];

  assert.equal(tools.size, expected.length, `expected ${expected.length} tools, got ${tools.size}`);
  for (const name of expected) {
    assert.ok(tools.has(name), `missing tool: ${name}`);
  }
});

test("calendar schemas enumerate all server-supported window kinds without requiring a value", () => {
  const { api, tools } = buildApi();
  register(api);

  const expectedKinds = [
    "today",
    "yesterday",
    "tomorrow",
    "all",
    "last_n_days",
    "next_n_days",
    "last_n_weeks",
    "last_n_months",
    "this_week",
    "last_week",
    "month_to_date",
    "last_month",
    "year_to_date",
    "last_year",
    "since",
    "between",
  ];

  for (const name of ["nbhd_calendar_list_events", "nbhd_calendar_get_freebusy"]) {
    const schema = tools.get(name).parameters;
    assert.deepEqual(schema.properties.window_kind.enum, expectedKinds);
    assert.ok(!schema.required?.includes("window_value"));
  }
});

test("runtime error payloads are surfaced with error code/detail", async () => {
  process.env.NBHD_TENANT_ID = "tenant-err";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 400,
      payload: { error: "invalid_request", detail: "bad query" },
    });

  register(api);
  const tool = tools.get("nbhd_gmail_list_messages");
  assert.ok(tool, "gmail tool should be registered");

  await assert.rejects(
    () => tool.execute("3", { q: "invalid" }),
    /invalid_request \(bad query\)/,
  );
});

test("gmail resolves calmly when the Google connection is inactive", async () => {
  process.env.NBHD_TENANT_ID = "tenant-inactive";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 409,
      payload: { error: "integration_inactive", detail: "x" },
    });

  register(api);
  const tool = tools.get("nbhd_gmail_list_messages");
  assert.ok(tool, "gmail tool should be registered");

  const result = await tool.execute("4", { q: "in:inbox" });
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.status, "google_unavailable");
  assert.equal(payload.reason, "integration_inactive");
  assert.equal(payload.integration_status, "unknown");
  assert.match(payload.message, /temporary problem/);
});

test("calendar resolves calmly when Google is not connected", async () => {
  process.env.NBHD_TENANT_ID = "tenant-missing";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 404,
      payload: { error: "integration_not_connected" },
    });

  register(api);
  const tool = tools.get("nbhd_calendar_list_events");
  assert.ok(tool, "calendar tool should be registered");

  const result = await tool.execute("5", { window_kind: "today" });
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.status, "google_unavailable");
  assert.equal(payload.integration_status, "not_connected");
});

test("gmail reports a server-side Google error as temporarily unavailable", async () => {
  process.env.NBHD_TENANT_ID = "tenant-google-error";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 409,
      payload: {
        error: "integration_inactive",
        detail: "Integration status is error for provider=google",
      },
    });

  register(api);
  const tool = tools.get("nbhd_gmail_list_messages");
  assert.ok(tool, "gmail tool should be registered");

  const result = await tool.execute("6", { q: "in:inbox" });
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.status, "google_unavailable");
  assert.equal(payload.integration_status, "error");
  assert.match(payload.message, /temporary problem/);
});

test("calendar reports an expired Google integration as not connected", async () => {
  process.env.NBHD_TENANT_ID = "tenant-google-expired";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 409,
      payload: {
        error: "integration_inactive",
        detail: "Integration status is expired for provider=google",
      },
    });

  register(api);
  const tool = tools.get("nbhd_calendar_list_events");
  assert.ok(tool, "calendar tool should be registered");

  const result = await tool.execute("7", { window_kind: "today" });
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.status, "google_unavailable");
  assert.equal(payload.integration_status, "expired");
  assert.match(payload.message, /not connected/);
});

test("404 without an integration detail falls back to not_connected", async () => {
  process.env.NBHD_TENANT_ID = "tenant-google-404";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 404,
      payload: { error: "integration_not_connected" },
    });

  register(api);
  const tool = tools.get("nbhd_gmail_list_messages");
  assert.ok(tool, "gmail tool should be registered");

  const result = await tool.execute("8", { q: "in:inbox" });
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.status, "google_unavailable");
  assert.equal(payload.integration_status, "not_connected");
  assert.match(payload.message, /not connected/);
});

test("non-connection runtime errors still reject", async () => {
  process.env.NBHD_TENANT_ID = "tenant-validation";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 400,
      payload: { error: "validation_failed" },
    });

  register(api);
  const tool = tools.get("nbhd_gmail_list_messages");
  assert.ok(tool, "gmail tool should be registered");

  await assert.rejects(
    () => tool.execute("6", { q: "invalid" }),
    /NBHD runtime error 400: validation_failed/,
  );
});

test("calendar plans require independent completion evidence", () => {
  const { api, tools } = buildApi();
  register(api);
  const description = tools.get("nbhd_calendar_list_events").description;
  assert.ok(description.includes("Calendar entries are plans, even after their end time"));
  assert.ok(description.includes('never write "done", "banked", "already done", or ✅ unless'));
  assert.ok(description.includes("Core meditation, Fuel workout, or task is marked done"));
  assert.ok(description.includes("for that activity, or the user confirms it"));
});
