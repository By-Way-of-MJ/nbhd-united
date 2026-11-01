// Run inside the pinned, network-disabled OpenClaw 2026.9.4 image with NODE_OPTIONS=.
// Generates fixture payloads through the actual gateway resolver and tool hook
// invoker; no hand-written event/ctx envelopes are used by the plugin tests.
import { readFileSync, writeFileSync } from "node:fs";
const root = "/usr/local/lib/node_modules/openclaw/dist/";
const { d: resolveGatewayRequestContext } = await import(root + "http-utils-DhJ41GBs.mjs");
const { i: initializeGlobalHookRunner } = await import(root + "hook-runner-global-BhDCl4qm.mjs");
const { y: runBeforeToolCallHook } = await import(root + "agent-tools.before-tool-call-WtmCO7BO.mjs");
const captured = [];
initializeGlobalHookRunner({ hooks: [], typedHooks: [{
  pluginId: "capture", hookName: "before_tool_call", handler: (event, ctx) => {
    captured.push({ event, ctx });
    return undefined;
  },
}], trustedToolPolicies: [] });
const user = "thread:11111111-2222-4333-8444-555555555555";
for (const suffix of ["", ":side:aabbccddeeff"]) {
  const resolved = resolveGatewayRequestContext({ req: { headers: {} }, model: "openclaw", user: user + suffix,
    sessionPrefix: "openai", defaultMessageChannel: "webchat", useMessageChannelHeader: true });
  for (const tool of ["publish_portfolio_image", "site_publish", "shop_confirm", "nbhd_reddit_post", "nbhd_reddit_reply", "web_fetch", "sessions_spawn", "nbhd_daily_note_get"]) {
    for (const dispatch of [false, true]) {
      const result = await runBeforeToolCallHook({
        toolName: dispatch ? "tool_call" : tool,
        params: dispatch ? { id: tool, args: {} } : {},
        toolCallId: `call-${captured.length}`,
        ctx: { agentId: resolved.agentId, sessionKey: resolved.sessionKey, sessionId: "fixture-session", runId: "fixture-run" },
      });
      if (result.blocked) throw Error("Fixture capture was blocked: " + result.reason);
    }
  }
}
if (captured.length !== 32) throw Error(`Expected 32 real hook calls, got ${captured.length}`);
// Execute the literal llm_output invocation from the pinned runner too; the
// event and ctx are built by its source rather than copied into these tests.
const { runInNewContext } = await import("node:vm");
const runner = "builtin-openclaw-B-H-7lKk.mjs";
const source = readFileSync(root + runner, "utf8");
const start = source.indexOf("hookRunner.runLlmOutput({");
const end = source.indexOf(".catch((err)", start);
if (start < 0 || end < 0) throw Error("Pinned llm_output call site changed");
const outputCalls = [];
for (const [suffix, runId, text] of [["", "main-run", "main first"], [":side:aabbccddeeff", "side-run", "side"], ["", "main-run", "main second"]]) {
  const sessionKey = resolveGatewayRequestContext({ req: { headers: {} }, model: "openclaw", user: user + suffix, sessionPrefix: "openai", defaultMessageChannel: "webchat" }).sessionKey;
  runInNewContext(source.slice(start, end), {
    hookRunner: { runLlmOutput(event, ctx) { outputCalls.push({ event, ctx }); return Promise.resolve(); } },
    attempt: { runId, sessionId: runId, provider: "offline", modelId: "offline", sessionKey },
    contextWindow: {}, assistantTexts: [text], state: {}, hookAgentId: "main",
    freezeDiagnosticTraceContext: (value) => value,
    buildAgentHookContextChannelFields: () => ({}),
    buildAgentHookContextIdentityFields: () => ({}),
  });
}
writeFileSync(process.argv[2], JSON.stringify({ version: "2026.9.4", sources: ["http-utils-DhJ41GBs.mjs", "agent-tools.before-tool-call-WtmCO7BO.mjs", runner], calls: captured, outputCalls }, null, 2) + "\n");
