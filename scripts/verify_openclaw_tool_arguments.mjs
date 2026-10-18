// Offline contract probe against an installed, exact npm openclaw@2026.9.4.
// Use Node >=24.16 and OPENCLAW_STATE_DIR pointing at an isolated scratch dir.
// No model/API calls. Only the final tool implementation is a recording fake.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { wrapTool } from "../runtime/openclaw/tool-logger.js";

assert.ok(process.argv[2], "Usage: node scripts/verify_openclaw_tool_arguments.mjs <installed openclaw root>");
assert.ok(process.env.OPENCLAW_STATE_DIR, "Set OPENCLAW_STATE_DIR to an isolated scratch directory");
const root = path.resolve(process.argv[2]);
const pkg = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
assert.equal(pkg.version, "2026.9.4", "Re-audit pinned imports when upgrading");
const load = (file) => import(pathToFileURL(path.join(root, "dist", file)));
const {
  a: applyToolSearchCatalog, s: applyDirectory, v: createCatalogRef,
  o: createControls, l: resolveDeferred, c: buildDirectory, x: restrictCatalog,
} = await load("local-model-lean-Ct7p3GNL.mjs");
const { a: setPluginMeta } = await load("tool-metadata-DYFFWrkm.mjs");
const { a: toDefinitions } = await load("agent-tool-definition-adapter-DhccrrY9.mjs");
const { nt: wrapDefinition } = await load("resource-loader-Bu_pVD2t.mjs");
const requireFromRuntime = createRequire(path.join(root, "package.json"));
const { validateToolArguments } = await import(pathToFileURL(requireFromRuntime.resolve("@openclaw/ai/validation")));
const { normalizeToolParameterSchema } = await import(pathToFileURL(requireFromRuntime.resolve("@openclaw/ai/internal/tool-schema")));
const { c: ToolsSchema } = await load("zod-schema.agent-runtime-Ca6cjqf9.mjs");

const received = [];
const tool = wrapTool({
  name: "nbhd_platform_issue_report",
  description: "Report a platform issue",
  parameters: {
    type: "object", required: ["category", "summary"],
    properties: { category: { type: "string" }, summary: { type: "string" } },
  },
  async execute(_id, params) {
    received.push(params);
    return { content: [{ type: "text", text: "ok" }], details: { ok: true } };
  },
});
setPluginMeta(tool, { pluginId: "nbhd-journal-tools" });
const expected = { category: "synthetic", summary: "offline fixture" };
for (const mode of ["tools", "directory"]) {
  const config = { tools: { toolSearch: { enabled: true, mode } } };
  assert.equal(ToolsSchema.safeParse(config.tools).success, true);
  const catalogRef = createCatalogRef();
  const ctx = { config, catalogRef };
  const controls = createControls(ctx);
  const surface = (mode === "tools" ? applyToolSearchCatalog : applyDirectory)({
    config, catalogRef, tools: [...controls, tool],
  });
  assert.equal(surface.compacted, true);
  assert.equal(surface.catalogToolCount, 1);
  assert.deepEqual(surface.tools.map(t => t.name).sort(), ["tool_call", "tool_describe", "tool_search"]);
  assert.ok(!surface.tools.includes(tool), "Target schemas must stay deferred");

  const call = controls.find(t => t.name === "tool_call");
  const id = "openclaw:nbhd-journal-tools:nbhd_platform_issue_report";
  const providerSchema = normalizeToolParameterSchema(call.parameters, {
    modelProvider: "openrouter", modelId: "deepseek/deepseek-v4-pro",
  });
  assert.equal(providerSchema.properties.args.type, "object");
  const stringArgs = validateToolArguments({ ...call, parameters: providerSchema }, {
    name: "tool_call", arguments: { id, args: JSON.stringify(expected) },
  });
  assert.deepEqual(stringArgs.args, expected, "9.4 admission parses object-valued JSON strings without losing fields");
  await call.execute("synthetic-string", stringArgs);
  assert.deepEqual(received.at(-1), expected);
  for (const args of ["bad", "", "[]", "null"]) {
    assert.throws(() => validateToolArguments({ ...call, parameters: providerSchema }, {
      name: "tool_call", arguments: { id, args },
    }), /Validation failed/);
  }
  for (const envelope of [{ id, args: expected }, { id, input: expected }, { id, ...expected }, { id, args: {}, ...expected }]) {
    const args = validateToolArguments({ ...call, parameters: providerSchema }, { name: "tool_call", arguments: envelope });
    await call.execute("synthetic-wrapper", args);
    assert.deepEqual(received.at(-1), expected);
  }
  for (const envelope of [{ id }, { id, args: {} }, { id, arguments: expected }, { id, params: expected }]) {
    const before = received.length;
    await assert.rejects(call.execute("synthetic-invalid", envelope), /Invalid arguments for tool/);
    assert.equal(received.length, before, "Core validation must reject before the NBHD wrapper executes");
  }
  if (mode === "directory") {
    assert.match(buildDirectory(ctx), /Call a unique deferred tool name directly/);
    // The same resolver, adapters, and argument validator used by the embedded
    // runtime's resolveDeferredTool path, with no generic call envelope.
    const hydrated = wrapDefinition(toDefinitions([resolveDeferred(ctx, tool.name)])[0]);
    const args = validateToolArguments(hydrated, { name: tool.name, arguments: expected });
    await hydrated.execute("synthetic-direct", args);
    assert.deepEqual(received.at(-1), expected);
    assert.throws(() => validateToolArguments(hydrated, { name: tool.name, arguments: {} }), /Validation failed/);
    assert.equal(resolveDeferred(ctx, "gateway"), undefined);
    restrictCatalog({ catalogRef, allowedToolNames: new Set() });
    assert.equal(resolveDeferred(ctx, tool.name), undefined, "Policy-removed targets cannot be hydrated");
  }
}
console.log("PASS: 9.4 canonical args survive; missing/wrong envelopes reject; directory hydrates with validation and policy filtering; target schemas remain deferred.");
console.log("This synthetic probe does not establish the production model's argument shape or reproduce a 5.28-to-9.4 argument-loss regression.");
