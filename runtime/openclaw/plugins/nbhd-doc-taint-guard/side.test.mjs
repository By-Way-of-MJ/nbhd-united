import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import register, { EXFIL_TOOL_IDS } from "./index.js";

// Captured by scripts/capture_keep_chatting_hooks.mjs, invoking the real pinned
// gateway resolver and runBeforeToolCallHook. Both direct and toolSearch calls.
const fixture = JSON.parse(readFileSync(new URL("./fixtures/keep-chatting-hooks.json", import.meta.url)));
for (const mode of ["enforce", "log_only"]) {
  test(`real 2026.9.4 hook shapes: side-session policy (${mode})`, () => {
    const hooks = new Map();
    register({ pluginConfig: { mode }, on: (name, fn) => hooks.set(name, fn),
      logger: { warn() {}, info() {} } });
    for (const { event, ctx } of fixture.calls) {
      const realId = event.toolName === "tool_call" ? event.params.id : event.toolName;
      const side = ctx.sessionKey.endsWith(":side:aabbccddeeff");
      const expected = side && (realId === "sessions_spawn" || (mode === "enforce" && EXFIL_TOOL_IDS.has(realId)));
      const result = hooks.get("before_tool_call")(event, ctx);
      assert.equal(result?.block === true, expected, `${ctx.sessionKey}: ${realId}`);
      if (expected) assert.match(result.blockReason, /side conversation/i);
    }
  });
}
