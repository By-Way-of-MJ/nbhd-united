// Offline proof using the actual OpenClaw 2026.9.4 gateway/queue implementation.
import assert from "node:assert/strict";
const root = "/usr/local/lib/node_modules/openclaw/dist/";
const { d: resolveContext } = await import(root + "http-utils-DhJ41GBs.mjs");
const { n: applyConcurrency, r: resolveConcurrency } = await import(root + "hook-client-ip-config-DIqpATxS.mjs");
const { r: enqueue } = await import(root + "command-queue-CwgsqkaH.mjs");
const { n: globalLane, r: sessionLane } = await import(root + "lanes-CVttd5qX.mjs");
const concurrency = resolveConcurrency({ agents: { defaults: { maxConcurrent: 2 } } });
assert.equal(concurrency.main, 2);
applyConcurrency(concurrency);
const user = "thread:11111111-2222-4333-8444-555555555555";
const context = (suffix) => resolveContext({ req: { headers: {} }, model: "openclaw", user: user + suffix, sessionPrefix: "openai", defaultMessageChannel: "webchat" });
const main = context("").sessionKey;
const side = context(":side:aabbccddeeff").sessionKey;
assert.notEqual(main, side);
let release;
const pending = new Promise((resolve) => { release = resolve; });
const started = [];
let allStarted;
const threeStarted = new Promise((resolve) => { allStarted = resolve; });
const run = (id, key, lane) => enqueue(sessionLane(key), () => enqueue(globalLane(lane), async () => {
  started.push(id);
  if (started.length === 3) allStarted();
  await pending;
}));
const tasks = [run("main", main), run("side", side), enqueue("cron", () => run("cron", "agent:main:cron:fixture:run:fixture", "cron"))];
let timer;
try {
  await Promise.race([threeStarted, new Promise((_, reject) => { timer = setTimeout(() => reject(Error("three concurrent jobs did not start")), 2000); })]);
  assert.deepEqual(new Set(started), new Set(["main", "side", "cron"]));
  const sameSession = run("same-session", main);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(started.includes("same-session"), false);
  release();
  await Promise.all([...tasks, sameSession]);
  console.log("PASS: maxConcurrent=2 runs main + side + cron concurrently; same-session overlap stays queued");
} finally {
  clearTimeout(timer);
  release();
}
