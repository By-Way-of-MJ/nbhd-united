/** Real app-turn envelopes captured on the pinned 2026.9.4 staging image.
 * See fixture.provenance for the emitting runtime source, not just its types.
 */
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import register, { finalQuickReplyLabels } from "./index.js";

const fixture = JSON.parse(readFileSync(new URL("./fixtures/button-carry-2026.9.4.json", import.meta.url), "utf8"));

function harness() {
  const handlers = {};
  const warnings = [];
  register({
    pluginConfig: { mode: "enforce" },
    on(name, handler) { handlers[name] = handler; },
    logger: { warn(line) { warnings.push(line); }, info() {} },
  });
  return { handlers, warnings };
}

// Change test values inside captured envelopes, retaining every real field.
function frame(which, run, session) {
  const result = structuredClone(fixture[which]);
  result.ctx.runId = run;
  result.ctx.sessionKey = session;
  if ("runId" in result.event) result.event.runId = run;
  return result;
}

function invoke(h, which, run, session, prompt) {
  const f = frame(which, run, session);
  if (prompt !== undefined) f.event.prompt = prompt;
  // The emitter's stable identity is ctx.runId, even without event.runId.
  if (f.hook === "agent_end") delete f.event.runId;
  return h.handlers[f.hook](f.event, f.ctx);
}

function offer(h, session, { flagged = true } = {}) {
  invoke(h, "before", session + "-offer", session);
  if (flagged) invoke(h, "read", session + "-offer", session);
  invoke(h, "end", session + "-offer", session);
}

function exfil(h, run, session) {
  const f = frame("read", run, session);
  f.event.toolName = "web_fetch";
  f.ctx.toolName = "web_fetch";
  return h.handlers.before_tool_call(f.event, f.ctx);
}

describe("captured button carry", () => {
  beforeEach(() => { process.env.NBHD_TAINT_CARRY_BARE_CONFIRM = "1"; });
  afterEach(() => { delete process.env.NBHD_TAINT_CARRY_BARE_CONFIRM; });

  it("finds the final assistant's marker despite later nested-tool activity", () => {
    assert.equal(fixture.end.event.success, true);
    assert.equal(typeof fixture.end.ctx.runId, "string");
    assert.equal(fixture.end.event.messages.at(-1).role, "custom");
    assert.deepEqual(finalQuickReplyLabels(fixture.end.event), ["post it", "skip"]);
    // A later run's full snapshot still contains the older offer. Its actual
    // final assistant message has no marker and must not re-arm those labels.
    assert.deepEqual(finalQuickReplyLabels(fixture.tapEnd.event), []);
  });

  it("uses the router's Unicode code-point label length limit", () => {
    const event = structuredClone(fixture.end.event);
    const assistant = event.messages.findLast(m => m.role === "assistant");
    const block = assistant.content.find(b => b.type === "text");
    const label = "👍".repeat(20);
    block.text = block.text.replace(/\[\[quick-replies:[^\n]*\]\]/, `[[quick-replies: ${label} | Skip]]`);
    assert.deepEqual(finalQuickReplyLabels(event), [label, "skip"]);
  });

  it("keeps the carry across a server handoff until the next real tap", () => {
    const h = harness(), session = "handoff-session";
    offer(h, session);
    invoke(h, "before", "recap", session, "[earlier-from-you: side conversation]\nStatus only.");
    assert.equal(exfil(h, "recap", session)?.block, true);
    invoke(h, "tapEnd", "recap", session);
    invoke(h, "before", "real-tap", session, "Post it");
    assert.equal(exfil(h, "real-tap", session)?.block, true);
    assert.ok(h.warnings.some(line => line === "taint_carried run=real-tap"));
  });

  it("carries a real tapped label, logs only the run id, and blocks exfil", () => {
    const h = harness();
    offer(h, "tap");
    invoke(h, "tap", "tap-next", "tap");
    const decision = exfil(h, "tap-next", "tap");
    assert.equal(decision?.block, true);
    assert.match(decision.blockReason, /in your own words/);
    assert.deepEqual(h.warnings.filter(x => x.startsWith("taint_carried")), ["taint_carried run=tap-next"]);
    invoke(h, "tapEnd", "tap-next", "tap");
  });

  it("consumes stored labels once, even when the next snapshot includes the old offer", () => {
    const h = harness();
    offer(h, "once");
    invoke(h, "tap", "once-2", "once");
    assert.equal(exfil(h, "once-2", "once")?.block, true);
    invoke(h, "tapEnd", "once-2", "once");
    invoke(h, "tap", "once-3", "once");
    assert.equal(exfil(h, "once-3", "once"), undefined);
    assert.equal(h.warnings.filter(x => x.startsWith("taint_carried")).length, 1);
  });

  it("uses the same batched-line, case, and punctuation normalization as bare yes", () => {
    const h = harness();
    offer(h, "batch");
    invoke(h, "tap", "batch-2", "batch", "Context\n[2] (14:03:22) POST IT!!\n");
    assert.equal(exfil(h, "batch-2", "batch")?.block, true);
    invoke(h, "tapEnd", "batch-2", "batch");
  });

  it("carries minted placeholders in either or both sides of a captured label tap", () => {
    for (const [label, tap] of [
      ["Email Bob", "Email [PERSON_7]"],
      ["Email [PERSON_7]", "Email Bob"],
      ["Email [PERSON_7]", "Email [PERSON_8]"],
      ["Ask [PERSON_7] now", "Ask Bob [LOCATION_2]"],
    ]) {
      const h = harness(), session = `minted-${label}-${tap}`;
      invoke(h, "before", session + "-offer", session);
      invoke(h, "read", session + "-offer", session);
      const end = frame("end", session + "-offer", session);
      const block = end.event.messages.findLast(m => m.role === "assistant").content.find(b => b.type === "text");
      block.text = block.text.replace(/\[\[quick-replies:[^\n]*\]\]/, `[[quick-replies: ${label} | Skip]]`);
      h.handlers.agent_end(end.event, end.ctx);
      invoke(h, "tap", session + "-tap", session, `Context\n[2] (14:03:22) ${tap}!!`);
      assert.equal(exfil(h, session + "-tap", session)?.block, true, `${label} -> ${tap}`);
      assert.equal(h.warnings.filter(x => x.startsWith("taint_carried")).length, 1);
      invoke(h, "tapEnd", session + "-tap", session);
      invoke(h, "tap", session + "-again", session, tap);
      assert.equal(exfil(h, session + "-again", session), undefined);
    }
  });

  it("a real sentence with different literal words still clears a placeholder label", () => {
    const h = harness();
    invoke(h, "before", "wild-offer", "wild");
    invoke(h, "read", "wild-offer", "wild");
    const end = frame("end", "wild-offer", "wild");
    const block = end.event.messages.findLast(m => m.role === "assistant").content.find(b => b.type === "text");
    block.text = block.text.replace(/\[\[quick-replies:[^\n]*\]\]/, "[[quick-replies: Email [PERSON_7] | Skip]]");
    h.handlers.agent_end(end.event, end.ctx);
    invoke(h, "tap", "wild-next", "wild", "Please explain the tax bill instead.");
    assert.equal(exfil(h, "wild-next", "wild"), undefined);
  });

  it("a real sentence consumes and clears the stored label", () => {
    const h = harness();
    offer(h, "sentence");
    invoke(h, "tap", "sentence-2", "sentence", "Please post the plan in the project discussion.");
    assert.equal(exfil(h, "sentence-2", "sentence"), undefined);
    invoke(h, "tapEnd", "sentence-2", "sentence");
    invoke(h, "tap", "sentence-3", "sentence");
    assert.equal(exfil(h, "sentence-3", "sentence"), undefined);
    assert.equal(h.warnings.some(x => x.startsWith("taint_carried")), false);
  });

  it("carry env off preserves the existing current-turn guard but stores no labels", () => {
    delete process.env.NBHD_TAINT_CARRY_BARE_CONFIRM;
    const h = harness();
    invoke(h, "before", "off-1", "off");
    invoke(h, "read", "off-1", "off");
    assert.equal(exfil(h, "off-1", "off")?.block, true);
    invoke(h, "end", "off-1", "off");
    invoke(h, "tap", "off-2", "off");
    assert.equal(exfil(h, "off-2", "off"), undefined);
    assert.equal(h.warnings.some(x => x.startsWith("taint_carried")), false);
  });

  it("an unflagged turn offering the same labels stores nothing", () => {
    const h = harness();
    offer(h, "unflagged", { flagged: false });
    invoke(h, "tap", "unflagged-2", "unflagged");
    assert.equal(exfil(h, "unflagged-2", "unflagged"), undefined);
    assert.equal(h.warnings.some(x => x.startsWith("taint_carried")), false);
  });

  it("expires labels at the existing thirty-minute boundary", () => {
    const actualNow = Date.now;
    let now = actualNow();
    Date.now = () => now;
    try {
      const h = harness();
      offer(h, "expired");
      now += 30 * 60 * 1000;
      invoke(h, "tap", "expired-2", "expired");
      assert.equal(exfil(h, "expired-2", "expired"), undefined);
    } finally { Date.now = actualNow; }
  });

  it("keeps the existing two-thousand-session cap for labels", () => {
    const h = harness();
    for (let i = 0; i <= 2000; i++) offer(h, `cap-${i}`);
    invoke(h, "tap", "cap-first", "cap-0");
    assert.equal(exfil(h, "cap-first", "cap-0"), undefined);
    invoke(h, "tap", "cap-last", "cap-2000");
    assert.equal(exfil(h, "cap-last", "cap-2000")?.block, true);
    invoke(h, "tapEnd", "cap-last", "cap-2000");
  });
});
