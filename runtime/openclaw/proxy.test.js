"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const { afterEach, test } = require("node:test");

const {
  createProxyServer,
  stripForwardedHeaders,
  GATEWAY_HEALTH_TIMEOUT_MS,
} = require("./proxy.js");

const openServers = new Set();

function listen(server) {
  openServers.add(server);
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });
}

function close(server) {
  openServers.delete(server);
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function requestJson(port) {
  const response = await fetch(`http://127.0.0.1:${port}/proxy-health`);
  return { status: response.status, body: await response.json() };
}

afterEach(async () => {
  await Promise.all([...openServers].map((server) => close(server)));
});

test("proxy health reports a healthy gateway", async () => {
  const gateway = http.createServer((_req, res) => {
    res.writeHead(200);
    res.end("ok");
  });
  const gatewayPort = await listen(gateway);
  const proxy = createProxyServer({ gatewayPort });
  const proxyPort = await listen(proxy);

  const result = await requestJson(proxyPort);

  assert.equal(result.status, 200);
  assert.deepEqual(result.body, {
    status: "ok",
    proxy: true,
    gateway: true,
  });
});

test("proxy health returns 503 when the gateway is unreachable", async () => {
  const unused = http.createServer();
  const gatewayPort = await listen(unused);
  await close(unused);
  const proxy = createProxyServer({ gatewayPort });
  const proxyPort = await listen(proxy);

  const result = await requestJson(proxyPort);

  assert.equal(result.status, 503);
  assert.equal(result.body.gateway, false);
});

test("proxy health times out a stalled gateway", async () => {
  const gateway = http.createServer();
  const gatewayPort = await listen(gateway);
  const proxy = createProxyServer({ gatewayPort, healthTimeoutMs: 25 });
  const proxyPort = await listen(proxy);

  const result = await requestJson(proxyPort);

  assert.equal(result.status, 503);
  assert.equal(result.body.gateway, false);
  assert.equal(GATEWAY_HEALTH_TIMEOUT_MS, 2000);
});

// ── OpenClaw 2026.9.4 proxy-attribution fix ───────────────────────────────
// The gateway 403s Bearer-authed routes when a request carries proxy-forwarded
// client headers it can't attribute. The proxy must strip them on the gateway
// hop so the gateway sees a clean loopback request; the webhook hop is left
// untouched.

test("stripForwardedHeaders drops forwarded headers, keeps auth", () => {
  const cleaned = stripForwardedHeaders({
    "X-Forwarded-For": "1.2.3.4",
    "x-forwarded-proto": "https",
    "X-Forwarded-Host": "example",
    "x-real-ip": "5.6.7.8",
    Forwarded: "for=1.2.3.4",
    Authorization: "Bearer tok",
    Host: "keep-me",
    "content-type": "application/json",
  });
  assert.deepEqual(cleaned, {
    Authorization: "Bearer tok",
    Host: "keep-me",
    "content-type": "application/json",
  });
});

async function headersSeenBy(server, proxyPort, path, headers) {
  const seen = { value: null };
  server.on("request", (req, res) => {
    seen.value = req.headers;
    res.writeHead(200);
    res.end("ok");
  });
  await fetch(`http://127.0.0.1:${proxyPort}${path}`, { headers });
  return seen.value;
}

test("gateway hop strips forwarded headers but preserves Authorization", async () => {
  const gateway = http.createServer();
  const gatewayPort = await listen(gateway);
  const proxy = createProxyServer({ gatewayPort });
  const proxyPort = await listen(proxy);

  const seen = await headersSeenBy(gateway, proxyPort, "/tools/invoke", {
    "x-forwarded-for": "1.2.3.4",
    "x-real-ip": "5.6.7.8",
    forwarded: "for=1.2.3.4",
    authorization: "Bearer tok",
  });

  assert.equal(seen["x-forwarded-for"], undefined);
  assert.equal(seen["x-real-ip"], undefined);
  assert.equal(seen["forwarded"], undefined);
  assert.equal(seen["authorization"], "Bearer tok");
});

test("webhook hop leaves forwarded headers intact", async () => {
  const webhook = http.createServer();
  const webhookPort = await listen(webhook);
  const gateway = http.createServer();
  const gatewayPort = await listen(gateway);
  const proxy = createProxyServer({ gatewayPort, webhookPort });
  const proxyPort = await listen(proxy);

  const seen = await headersSeenBy(webhook, proxyPort, "/telegram-webhook", {
    "x-forwarded-for": "1.2.3.4",
  });

  assert.equal(seen["x-forwarded-for"], "1.2.3.4");
});

test("chat retries and replacement drains share the same in-flight call and reply", async () => {
  let calls = 0;
  let release;
  const started = new Promise((resolve) => { release = resolve; });
  const gateway = http.createServer((req, res) => {
    calls++;
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", async () => {
      assert.equal(body, '{"user":"thread:fixture"}');
      await started;
      res.setHeader("Content-Type", "application/json");
      res.end('{"choices":[{"message":{"content":"once"}}]}');
    });
  });
  const proxyPort = await listen(createProxyServer({ gatewayPort: await listen(gateway) }));
  const options = { method: "POST", headers: { "X-NBHD-Chat-Request-Id": "11111111-2222-4333-8444-555555555555", Authorization: "Bearer offline" }, body: '{"user":"thread:fixture"}' };
  const first = fetch(`http://127.0.0.1:${proxyPort}/v1/chat/completions`, options);
  const retry = fetch(`http://127.0.0.1:${proxyPort}/v1/chat/completions`, options);
  release();
  const replies = await Promise.all([first, retry]);
  assert.deepEqual(await replies[0].json(), await replies[1].json());
  const replay = await fetch(`http://127.0.0.1:${proxyPort}/v1/chat/completions`, options);
  assert.equal(replay.status, 200);
  assert.equal(calls, 1);
  // Another batch and another authenticated caller cannot consume this reply.
  await fetch(`http://127.0.0.1:${proxyPort}/v1/chat/completions`, { ...options, headers: { ...options.headers, Authorization: "Bearer another-offline" } });
  assert.equal(calls, 2);
});

test("a disconnected drain can recover the completed gateway reply", async () => {
  let calls = 0;
  let send;
  const reached = new Promise((resolve) => {
    send = resolve;
  });
  let reply;
  const gateway = http.createServer((req, res) => {
    calls++;
    req.resume();
    reply = () => res.end('{"answer":"kept"}');
    send();
  });
  const proxyPort = await listen(createProxyServer({ gatewayPort: await listen(gateway) }));
  const headers = { "X-NBHD-Chat-Request-Id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" };
  const abort = new AbortController();
  const first = fetch(`http://127.0.0.1:${proxyPort}/v1/chat/completions`, { method: "POST", headers, body: "{}", signal: abort.signal }).catch(() => null);
  await reached;
  abort.abort();
  await first;
  reply();
  const recovered = await fetch(`http://127.0.0.1:${proxyPort}/v1/chat/completions`, { method: "POST", headers, body: "{}" });
  assert.equal(await recovered.text(), '{"answer":"kept"}');
  assert.equal(calls, 1);
});

test("replay retention allows a burst beyond 64 normal chat requests", async () => {
  let calls = 0;
  const gateway = http.createServer((req, res) => {
    req.resume();
    calls++;
    res.end('{"answer":"ok"}');
  });
  const proxyPort = await listen(createProxyServer({ gatewayPort: await listen(gateway) }));
  for (let i = 0; i < 70; i++) {
    const id = `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
    const response = await fetch(`http://127.0.0.1:${proxyPort}/v1/chat/completions`, {
      method: "POST", headers: { "X-NBHD-Chat-Request-Id": id }, body: "{}",
    });
    assert.equal(response.status, 200);
    await response.text();
  }
  assert.equal(calls, 70);
});

async function retryAfterRejection(status, body) {
  let calls = 0;
  const gateway = http.createServer((req, res) => {
    req.resume();
    calls++;
    res.writeHead(calls === 1 ? status : 200, { "Content-Type": "application/json" });
    res.end(calls === 1 ? JSON.stringify(body) : '{"answer":"recovered"}');
  });
  const port = await listen(createProxyServer({ gatewayPort: await listen(gateway) }));
  const options = { method: "POST", headers: { "X-NBHD-Chat-Request-Id": "11111111-2222-4333-8444-555555555555" }, body: "{}" };
  const url = `http://127.0.0.1:${port}/v1/chat/completions`;
  const rejected = await fetch(url, options);
  assert.equal(rejected.status, status);
  await rejected.text();
  assert.equal(await (await fetch(url, options)).text(), '{"answer":"recovered"}');
  await (await fetch(url, options)).text();
  assert.equal(calls, 2);
}

test("an explicit unavailable response permits the next retry", async () => {
  await retryAfterRejection(503, { error: "unavailable" });
});

test("a raised credit ceiling can retry the rejected batch", async () => {
  await retryAfterRejection(402, { error: "payment_required" });
  await retryAfterRejection(500, { error: { message: "Quota exceeded" } });
});

test("a refused gateway connection is not retained as ambiguous work", async () => {
  const placeholder = http.createServer();
  const gatewayPort = await listen(placeholder);
  await close(placeholder);
  const port = await listen(createProxyServer({ gatewayPort }));
  const options = { method: "POST", headers: { "X-NBHD-Chat-Request-Id": "11111111-2222-4333-8444-555555555555" }, body: "{}" };
  const url = `http://127.0.0.1:${port}/v1/chat/completions`;
  const refused = await fetch(url, options);
  assert.equal(refused.status, 502);
  assert.equal((await refused.json()).error, "bad_gateway");
  let calls = 0;
  const gateway = http.createServer((req, res) => { req.resume(); calls++; res.end("recovered"); });
  openServers.add(gateway);
  await new Promise((resolve) => gateway.listen(gatewayPort, "127.0.0.1", resolve));
  assert.equal(await (await fetch(url, options)).text(), "recovered");
  await (await fetch(url, options)).text();
  assert.equal(calls, 1);
});

test("a drain crash during upload permits one complete replacement request", { timeout: 3000 }, async () => {
  let accepted = 0;
  let sawChunk, sawAbort;
  const chunk = new Promise((resolve) => { sawChunk = resolve; });
  const aborted = new Promise((resolve) => { sawAbort = resolve; });
  const gateway = http.createServer((req, res) => {
    req.on("data", () => sawChunk());
    req.on("aborted", () => sawAbort());
    req.on("error", () => {});
    req.on("end", () => { accepted++; res.end("complete"); });
  });
  const port = await listen(createProxyServer({ gatewayPort: await listen(gateway) }));
  const id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
  const original = http.request({ hostname: "127.0.0.1", port, path: "/v1/chat/completions", method: "POST", headers: { "Content-Length": 2, "X-NBHD-Chat-Request-Id": id } });
  original.on("error", () => {});
  original.write("{");
  await chunk;
  original.destroy();
  await aborted;
  const options = { method: "POST", headers: { "X-NBHD-Chat-Request-Id": id }, body: "{}" };
  const url = `http://127.0.0.1:${port}/v1/chat/completions`;
  assert.equal(await (await fetch(url, options)).text(), "complete");
  await (await fetch(url, options)).text();
  assert.equal(accepted, 1);
});
