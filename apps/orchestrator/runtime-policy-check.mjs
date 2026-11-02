// Offline package contract test. Python supplies the real built-in seed jobs on
// stdin; import the published runtime's matcher, rather than imitating it.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const root = process.argv[2];
assert.equal(JSON.parse(readFileSync(join(root, "package.json"))).version, "2026.9.4");
const dist = join(root, "dist");
const find = (prefix) => join(dist, readdirSync(dist).find((name) => name.startsWith(prefix) && name.endsWith(".mjs")));
const { i: isRuntimeToolAllowed } = await import(pathToFileURL(find("tool-policy-match-")));
const executor = readFileSync(find("run-executor.runtime-"), "utf8");
// This is the common built-in / typed isolated-job execution path. The matcher
// above receives the explicit job payload cap without a pattern discriminator.
assert.match(executor, /toolsAllow: params\.agentPayload\?\.toolsAllow/u);
const jobs = JSON.parse(readFileSync(0, "utf8"));
assert.equal(jobs.length, 2);
for (const job of jobs) {
  const allow = job.payload.toolsAllow;
  for (const name of allow) assert.equal(isRuntimeToolAllowed(name, allow), true, name);
  for (const name of ["nbhd_document_get", "nbhd_document_put", "nbhd_send_to_user", "nbhd_task_create", "nbhd_memory_update", "nbhd_project_propose_change", "cron", "sessions_spawn", "exec", "write", "nbhd_gmail_send", "nbhd_calendar_create_event"]) {
    assert.equal(isRuntimeToolAllowed(name, allow), false, `${job.name}: ${name}`);
  }
  assert.equal(isRuntimeToolAllowed("web_search", allow), job.name === "Step Prep");
  assert.equal(isRuntimeToolAllowed("nbhd_step_prep_next", allow), job.name === "Step Prep");
  assert.equal(isRuntimeToolAllowed("nbhd_step_prep_update_next", allow), job.name === "Step Prep Update");
}

console.log("OpenClaw 2026.9.4 toolsAllow matcher: PASS — Step Prep and Step Prep Update allow their exact tools and deny document, send, task, memory, project-write, cron, session, exec, file, Gmail and calendar tools; web_search is research-only.");
