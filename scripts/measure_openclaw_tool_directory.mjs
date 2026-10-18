// Offline prompt measurement using the exact 9.4 formatter and real NBHD
// registrations. No tool execution, model calls, or tenant data. This sample
// approximates the ~86-tool catalog; per-tenant policy/feature gates vary.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

assert.ok(process.argv[2], "Usage: node scripts/measure_openclaw_tool_directory.mjs <installed openclaw root>");
assert.ok(process.env.OPENCLAW_STATE_DIR, "Set OPENCLAW_STATE_DIR to an isolated scratch directory");
const root = path.resolve(process.argv[2]);
assert.equal(JSON.parse(readFileSync(path.join(root, "package.json"), "utf8")).version, "2026.9.4");
const load = (file) => import(pathToFileURL(path.join(root, "dist", file)));
const {
  a: applySearch, s: applyDirectory, v: createCatalogRef,
  o: createControls, c: buildDirectory,
} = await load("local-model-lean-Ct7p3GNL.mjs");
const { a: setPluginMeta } = await load("tool-metadata-DYFFWrkm.mjs");
const tools = [];
const counts = {};
const log = console.log;
// Registration logs contain only tool names, but keep stdout machine-readable.
try {
  console.log = () => {};
  for (const plugin of ["nbhd-journal-tools", "nbhd-finance-tools", "nbhd-fuel-tools", "nbhd-insights-tools"]) {
    const { default: register } = await import(new URL(`../runtime/openclaw/plugins/${plugin}/index.js`, import.meta.url));
    counts[plugin] = 0;
    register({
      pluginConfig: {},
      registerTool(definition) {
        const tool = typeof definition === "function" ? definition({}) : definition;
        setPluginMeta(tool, { pluginId: plugin });
        tools.push(tool);
        counts[plugin] += 1;
      },
    });
  }
} finally {
  console.log = log;
}
const measurements = [];
for (const contextTokenBudget of [262144, 131072, 32768]) {
  const surfaces = [];
  for (const mode of ["tools", "directory"]) {
    const config = { tools: { toolSearch: { enabled: true, mode } } };
    const catalogRef = createCatalogRef();
    const ctx = { config, catalogRef };
    const controls = createControls(ctx);
    const surface = (mode === "tools" ? applySearch : applyDirectory)({
      config, catalogRef, tools: [...controls, ...tools],
    });
    surfaces.push(surface.tools.map(({ name, description, parameters }) => ({ name, description, parameters })));
    assert.equal(surface.catalogToolCount, tools.length);
    const prompt = buildDirectory(ctx, { contextTokenBudget });
    measurements.push({
      contextTokenBudget, mode, toolCount: tools.length,
      listedRows: prompt.split("\n").filter(line => line.startsWith("- ")).length,
      chars: prompt.length, prompt,
    });
  }
  assert.deepEqual(surfaces[0], surfaces[1], "Control schemas must match between modes");
}
console.log(JSON.stringify({ counts, measurements }, null, 2));
