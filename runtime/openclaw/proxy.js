/**
 * Lightweight reverse proxy for OpenClaw container.
 *
 * Routes a single ingress port to the two internal HTTP servers:
 *   /telegram-webhook  -> localhost:8787  (webhook server)
 *   everything else    -> localhost:18789 (Gateway API)
 *
 * Uses only built-in Node.js modules — no npm install required.
 */

const http = require("http");
const { createHash } = require("node:crypto");

const PROXY_PORT = parseInt(process.env.PROXY_PORT || "8080", 10);
const GATEWAY_PORT = 18789;
const WEBHOOK_PORT = 8787;
const GATEWAY_HEALTH_TIMEOUT_MS = 2000;

// OpenClaw 2026.9.4's gateway REJECTS Bearer-authenticated routes (e.g.
// /tools/invoke — every chat turn + cron tool call) with
// "proxy_attribution_required" when a request carries proxy-forwarded client
// headers it can't attribute to a trusted proxy. Azure Container Apps ingress
// stamps X-Forwarded-* / Forwarded on the hop to this proxy; passing them
// through to the loopback gateway makes 9.4 treat us as an untrusted proxy and
// 403 the request (silently breaks all chat on 9.4). gateway.trustedProxies
// alone does NOT fix it — the forwarded client IP resolves to loopback, still
// "unattributable". The fix the gateway itself recommends is to have the proxy
// rebuild the forwarded headers: strip them so the gateway sees a clean
// loopback request (attributed "direct-local") and applies the Bearer token
// check normally. 9.4's hasForwardedRequestHeaders() keys on `forwarded`,
// `x-real-ip`, and any `x-forwarded-*`, so drop exactly those. Only for the
// gateway hop; the Telegram webhook server (:8787) is left untouched.
function stripForwardedHeaders(headers) {
  const cleaned = { ...headers };
  for (const name of Object.keys(cleaned)) {
    const n = name.toLowerCase();
    if (n === "forwarded" || n === "x-real-ip" || n.startsWith("x-forwarded-")) {
      delete cleaned[name];
    }
  }
  return cleaned;
}

function proxyRequest(req, res, targetPort, stripForwarded = false) {
  const options = {
    hostname: "127.0.0.1",
    port: targetPort,
    path: req.url,
    method: req.method,
    headers: stripForwarded ? stripForwardedHeaders(req.headers) : req.headers,
  };

  const upstream = http.request(options, (upstreamRes) => {
    res.writeHead(upstreamRes.statusCode, upstreamRes.headers);
    upstreamRes.pipe(res);
  });

  upstream.on("error", (err) => {
    console.error(
      `[proxy] upstream error port=${targetPort} path=${req.url}: ${err.message}`
    );
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "application/json" });
    }
    res.end(
      JSON.stringify({
        error: "bad_gateway",
        detail: `upstream ${targetPort} unreachable`,
      })
    );
  });

  req.pipe(upstream);
}

// A Django drain can die or lose its response while the gateway continues.
// Stable batch IDs let its replacement attach to that same call/result. Only
// flagged app drains send this header. No files or transcripts are persisted.
const CHAT_REPLAY_TTL_MS = 15 * 60 * 1000;
// Covers the unchanged 300 sends/hour limit plus their internal recap turns.
const CHAT_REPLAY_MAX_ENTRIES = 512;
const CHAT_REPLAY_MAX_BYTES = 1024 * 1024;
// These are explicit rejections, rather than completed or ambiguous work.
// Credit strings match apps/router/pending_queue.py's credit-limit detector.
function retryableChatRejection(reply) {
  if ([400, 401, 403, 404, 405, 413, 415, 422, 429, 503].includes(reply.status)) return true;
  if (reply.status === 402) return true;
  if (reply.status < 400 || reply.status >= 600) return false;
  const text = reply.body.toString("utf8").toLowerCase();
  return ["insufficient credit", "insufficient_credit", "credit limit", "credit_limit", "quota exceeded", "quota_exceeded"].some((needle) => text.includes(needle));
}
function proxyChatOnce(req, res, targetPort, calls) {
  const requestId = req.headers["x-nbhd-chat-request-id"];
  const identity = createHash("sha256").update(String(req.headers.authorization || "") + ":" + requestId).digest("hex");
  for (const [key, value] of calls) {
    if (value.done && Date.now() - value.done > CHAT_REPLAY_TTL_MS) calls.delete(key);
  }
  let entry = calls.get(identity);
  if (entry) {
    req.resume(); // only the first request body reaches the gateway
    entry.promise.then((reply) => sendReplay(res, reply));
    return;
  }
  if (calls.size >= CHAT_REPLAY_MAX_ENTRIES) {
    // Do not evict a still-retriable result: that could re-run a mutation.
    req.resume();
    sendReplay(res, { status: 503, headers: {}, body: Buffer.from('{"error":"chat_replay_capacity"}') });
    return;
  }
  let finish;
  entry = { done: null, promise: new Promise((resolve) => { finish = resolve; }) };
  calls.set(identity, entry);
  entry.promise.then((reply) => sendReplay(res, reply));
  let settled = false;
  function complete(reply, rejected = false) {
    if (settled) return;
    settled = true;
    entry.done = Date.now();
    if (rejected && calls.get(identity) === entry) calls.delete(identity);
    finish(reply);
  }
  const headers = stripForwardedHeaders(req.headers);
  const upstream = http.request({ hostname: "127.0.0.1", port: targetPort, path: req.url, method: req.method, headers }, (response) => {
    const chunks = [];
    let size = 0;
    response.on("data", (chunk) => {
      size += chunk.length;
      if (size <= CHAT_REPLAY_MAX_BYTES) chunks.push(chunk);
    });
    response.on("end", () => {
      if (size > CHAT_REPLAY_MAX_BYTES) {
        complete({ status: 502, headers: {}, body: Buffer.from('{"error":"chat_reply_too_large"}') });
      } else {
        const reply = { status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks) };
        complete(reply, retryableChatRejection(reply));
      }
    });
    response.on("error", fail);
  });
  function fail(error) {
    const refused = error?.code === "ECONNREFUSED";
    const incomplete = error?.code === "CHAT_BODY_INCOMPLETE";
    // An incomplete body or a refused connection cannot have started a turn.
    // Keep the existing bad_gateway shape for the router's boot-grace retry.
    const body = refused
      ? { error: "bad_gateway", detail: `upstream ${targetPort} unreachable` }
      : { error: incomplete ? "chat_request_interrupted" : "chat_gateway_connection_lost" };
    complete({ status: 502, headers: {}, body: Buffer.from(JSON.stringify(body)) }, refused || incomplete);
  }
  upstream.on("error", fail);
  const abortIncomplete = () => {
    if (!req.complete) upstream.destroy(Object.assign(new Error("incomplete chat body"), { code: "CHAT_BODY_INCOMPLETE" }));
  };
  req.on("aborted", abortIncomplete);
  req.on("error", abortIncomplete);
  req.pipe(upstream);
}
function sendReplay(res, reply) {
  if (res.destroyed) return;
  res.writeHead(reply.status, reply.headers);
  res.end(reply.body);
}

function proxyHealth(res, gatewayPort, timeoutMs) {
  let settled = false;
  const respond = (statusCode, payload) => {
    if (settled) return;
    settled = true;
    res.writeHead(statusCode, { "Content-Type": "application/json" });
    res.end(JSON.stringify(payload));
  };

  const upstream = http.get(
    {
      hostname: "127.0.0.1",
      port: gatewayPort,
      path: "/healthz",
    },
    (upstreamRes) => {
      upstreamRes.resume();
      if (upstreamRes.statusCode >= 200 && upstreamRes.statusCode < 400) {
        respond(200, { status: "ok", proxy: true, gateway: true });
      } else {
        respond(503, {
          status: "unavailable",
          proxy: true,
          gateway: false,
        });
      }
    }
  );

  upstream.setTimeout(timeoutMs, () => {
    upstream.destroy(new Error("gateway health probe timed out"));
  });
  upstream.on("error", () => {
    respond(503, {
      status: "unavailable",
      proxy: true,
      gateway: false,
    });
  });
}

function createProxyServer({
  gatewayPort = GATEWAY_PORT,
  webhookPort = WEBHOOK_PORT,
  healthTimeoutMs = GATEWAY_HEALTH_TIMEOUT_MS,
} = {}) {
  const chatCalls = new Map();
  return http.createServer((req, res) => {
    if (req.url === "/proxy-health") {
      proxyHealth(res, gatewayPort, healthTimeoutMs);
      return;
    }

    if (req.method === "POST" && req.url === "/v1/chat/completions" &&
        /^[0-9a-f-]{36}$/.test(String(req.headers["x-nbhd-chat-request-id"] || ""))) {
      proxyChatOnce(req, res, gatewayPort, chatCalls);
      return;
    }

    if (req.url.startsWith("/telegram-webhook")) {
      proxyRequest(req, res, webhookPort);
    } else {
      // Strip proxy-forwarded client headers on the gateway hop (see
      // stripForwardedHeaders) so 2026.9.4 doesn't 403 the Bearer-authed route.
      proxyRequest(req, res, gatewayPort, true);
    }
  });
}

if (require.main === module) {
  const server = createProxyServer();
  server.listen(PROXY_PORT, () => {
    console.log(`[proxy] listening on :${PROXY_PORT}`);
    console.log(`[proxy]   /telegram-webhook -> :${WEBHOOK_PORT}`);
    console.log(`[proxy]   /*                -> :${GATEWAY_PORT}`);
  });

  // Graceful shutdown
  function shutdown(signal) {
    console.log(`[proxy] received ${signal}, shutting down`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(1), 10000);
  }

  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
}

module.exports = { createProxyServer, stripForwardedHeaders, GATEWAY_HEALTH_TIMEOUT_MS };
