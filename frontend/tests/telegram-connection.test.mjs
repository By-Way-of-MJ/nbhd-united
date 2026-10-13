import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
function load(file, mocks = {}) {
  const js = ts.transpileModule(readFileSync(new URL(file, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", js)((name) => mocks[name] ?? {}, mod, mod.exports);
  return mod.exports;
}
const errors = load("../lib/errors.ts");
const unavailable = Object.assign(new Error(JSON.stringify({ code: "telegram_unavailable", detail: "Telegram replies are no longer available. Use the iPhone app or LINE." })), { status: 409 });
test("409 detail is shown inline, with a readable fallback for code-only errors", () => {
  assert.match(errors.getErrorMessage(unavailable), /Telegram replies are no longer available/);
  assert.doesNotMatch(errors.getErrorMessage(new Error('{"code":"telegram_unavailable"}')), /telegram_unavailable/);
});
test("regeneration 409 clears old link, stops polling, and blocks further generation", async () => {
  const states = [];
  let cursor = 0;
  let statusArgs;
  let attempts = 0;
  const hook = load("../components/channel/use-channel-connection.ts", {
    react: { useState(initial) { const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], (next) => { states[i] = next; }]; } },
    "@/lib/errors": errors,
    "@/lib/queries": {
      useTenantQuery: () => ({ data: { telegram_available: true } }),
      useTelegramStatusQuery: (...args) => { statusArgs = args; return { data: { linked: false }, isFetchedAfterMount: true }; },
      useGenerateTelegramLinkMutation: () => ({ reset() {}, async mutateAsync() { attempts++; if (attempts === 1) return { deep_link: "old" }; throw unavailable; } }),
    },
  }).useTelegramConnection;
  const render = () => { cursor = 0; return hook(true); };
  await render().generate();
  assert.equal(render().link.deep_link, "old");
  await render().generate();
  const result = render();
  assert.equal(result.link, null);
  assert.equal(result.unavailable, true);
  assert.equal(statusArgs[0], false);
  assert.match(result.generationError, /Telegram replies are no longer available/);
  await result.generate();
  assert.equal(attempts, 2);
});
test("status query does not retry 409 or poll errors, linked, or retired channels", () => {
  const { useTelegramStatusQuery } = load("../lib/queries.ts", {
    "@tanstack/react-query": { useQuery: (options) => options },
    "@/lib/auth": { isLoggedIn: () => true },
  });
  const options = useTelegramStatusQuery(true, true);
  assert.equal(options.retry, false);
  assert.equal(options.refetchInterval({ state: { status: "error" } }), false);
  assert.equal(options.refetchInterval({ state: { data: { linked: true } } }), false);
  assert.equal(options.refetchInterval({ state: { data: { linked: false } } }), 3000);
  assert.equal(useTelegramStatusQuery(true, false, false).refetchInterval, false);
});
