// Spec for the Neighborhood page's pure helpers — runnable with Node's
// built-in runner after a tsc transpile.
import { test } from "node:test";
import assert from "node:assert/strict";

import { buildNeeds, countsLine, crewLabel, groupKeeps, keepMeta, keepsSummary, messageRows, projectLine } from "./neighborhood";
import type { AbsorbedItem, ChatThread, HomeNeighbor, MissionDetail } from "./types";

test("needs you: one list, people first, newest first within a kind", () => {
  const needs = buildNeeds({
    shares: [
      { id: "s1", lesson_id: 1, lesson_preview: "Plan for the hunger window.", proposed_by: "agent", friendship_id: "f1", audience: "Kiho", created_at: "2026-09-01T10:00:00Z" },
      { id: "s2", lesson_id: 2, lesson_preview: "Carve out an hour.", proposed_by: "agent", friendship_id: "f1", audience: "Kiho", created_at: "2026-09-03T10:00:00Z" },
    ],
    waves: [{ friendship_id: "w1", display_name: "Dudley", handle: "dud", avatar_hue: 1, note: "", created_at: "2026-08-01T00:00:00Z" }],
    asks: [
      { mission_id: "m1", title: "Help Aiko move", status: "active", target: {}, target_date: "2026-10-04", my_commitment: "", version: 1, my_status: "invited", my_role: "member" },
      { mission_id: "m2", title: "Joined already", status: "active", target: {}, target_date: null, my_commitment: "", version: 1, my_status: "active", my_role: "member" },
      { mission_id: "m3", title: "Finished", status: "achieved", target: {}, target_date: null, my_commitment: "", version: 1, my_status: "invited", my_role: "member" },
    ],
    actions: [{ id: "g1", mission_id: "m9", mission_title: "Morning walks", suggested: { title: "Buy shoes", description: "", due_date: null }, created_at: "2026-09-02T00:00:00Z" }],
  });
  assert.deepEqual(needs.map((n) => n.key), ["wave:w1", "share:s2", "share:s1", "ask:m1", "action:g1"]);
  assert.equal(needs[0].text, "Dudley waved at you.");
  assert.equal(needs[0].action, "Wave back");
  assert.equal(needs[0].dismiss, "Not now");
  assert.match(needs[1].text, /^Your assistant wants to share “Carve out an hour\.” with Kiho\.$/);
  assert.match(needs[3].text, /Help Aiko move/);
  assert.equal(buildNeeds({}).length, 0);
});

test("needs you: a wave note is quoted; a circle share without audience still reads", () => {
  const [w] = buildNeeds({ waves: [{ friendship_id: "w", display_name: "Tomo", handle: "t", avatar_hue: 0, note: "We met at the running club!", created_at: "" }] });
  assert.equal(w.text, "Tomo waved at you: “We met at the running club!”");
  const [s] = buildNeeds({ shares: [{ id: "s", lesson_id: 1, lesson_preview: "x", proposed_by: "a", friendship_id: null, circle_id: "c", audience: null, created_at: "" }] });
  assert.equal(s.tag, "Share to a cluster");
});

test("counts line", () => {
  assert.equal(countsLine(2, 2, 1), "2 neighbors · 2 clusters · 1 project");
  assert.equal(countsLine(1, 0, 0), "1 neighbor · 0 clusters · 0 projects");
});

const neighbor = (id: string, name: string, sky = false): HomeNeighbor => ({ friendship_id: id, display_name: name, handle: name.toLowerCase(), avatar_hue: 10, spark_count: 0, in_my_sky: sky, bond: "light", friends_since: "2025-01-01", has_unread_thread: false, thread_id: null });
const thread = (id: string, friendship: string | null, last: string, at: string | null): ChatThread => ({ thread_id: id, friendship_id: friendship, display_name: "Neighbor", handle: null, avatar_hue: 210, unread: 0, last_message: last, last_message_at: at, muted: false, agent_absorb_enabled: false });

test("messages: one row per person, talked-to first, then say hello (sky first, A–Z)", () => {
  const ns = [neighbor("a", "Zed"), neighbor("b", "Kiho", true), neighbor("c", "Aiko"), neighbor("d", "Dudley")];
  const rows = messageRows(
    [thread("t1", "c", "Old one", "2026-09-01T00:00:00Z"), thread("t2", "b", "See you Sunday", "2026-09-10T00:00:00Z"), thread("t3", "d", "", null), thread("tc", null, "Circle chat", "2026-09-11T00:00:00Z")],
    ns,
  );
  assert.deepEqual(rows.map((r) => r.name), ["Kiho", "Aiko", "Dudley", "Zed"]);
  assert.equal(rows[0].last, "See you Sunday");
  assert.equal(rows[2].last, "");
  assert.equal(rows[2].thread?.thread_id, "t3", "an empty thread is reused, not reopened");
  assert.equal(rows[3].thread, null);
});

test("keeps: grouped per person, duplicates collapsed, own shares and unknowns last", () => {
  const item = (id: string, from: string | null, label: string, at: string, extra: Partial<AbsorbedItem & { group_key: string; kind_label: string }> = {}) => ({ id, source_kind: "spark", source_id: id, from_handle: from, label, absorbed_at: at, ...extra });
  const items = [
    item("1", "kiho", "Chat summary", "2026-09-12T00:00:00Z", { group_key: "chat:kiho", kind_label: "Chat summary" }),
    item("2", "kiho", "Chat summary (Sep 20)", "2026-09-20T00:00:00Z", { group_key: "chat:kiho", kind_label: "Chat summary" }),
    item("3", "kiho", "Prefers mornings.", "2026-09-09T00:00:00Z"),
    item("4", "kiho", "prefers mornings.", "2026-09-10T00:00:00Z"),
    item("5", "me", "Deep work needs discipline.", "2026-09-24T00:00:00Z"),
    item("6", null, "Orphan", "2026-09-01T00:00:00Z"),
    item("7", "dud", "Likes tea", "2026-09-02T00:00:00Z"),
  ];
  const groups = groupKeeps(items, new Map([["kiho", "Kiho"], ["dud", "Dudley"]]), "me");
  assert.deepEqual(groups.map((g) => g.name), ["Kiho", "Dudley", "You shared back", "Other"]);
  const kiho = groups[0];
  assert.equal(kiho.count, 4);
  assert.equal(kiho.items.length, 2);
  const chat = kiho.items.find((i) => i.key === "g:chat:kiho")!;
  assert.deepEqual(chat.ids, ["1", "2"]);
  assert.match(keepMeta(chat), /Sep 12 – Sep 20 · Chat summary · kept 2 times/);
  assert.deepEqual(kiho.items.find((i) => i.key.startsWith("s:"))!.ids, ["3", "4"]);
  assert.equal(keepsSummary(groups), "7 notes from 2 people");
  assert.equal(keepsSummary([]), "");
});

test("projects: crew and this-week line", () => {
  const detail: MissionDetail = {
    mission_id: "m", title: "t", status: "active", cadence: "daily", window_days: 7, target: {}, overall_pct: 10, description: "", version: 1, my_commitment: "", my_role: "owner",
    members: [
      { handle: "me", showed_up: 0, window_days: 7, streak: 0, last_activity: null, next_step: null, commitment: "", is_creator: true },
      { handle: "kiho", showed_up: 3, window_days: 7, streak: 1, last_activity: null, next_step: "Talk about an AI workflow", commitment: "", is_creator: false },
    ],
  };
  const names = new Map([["kiho", "Kiho Tanaka"]]);
  assert.equal(crewLabel(detail, "me", names), "You + Kiho");
  assert.equal(projectLine(detail, "me"), "0 of 7 days this week · next: talk about an AI workflow");
  assert.equal(crewLabel({ members: [...detail.members, { ...detail.members[1], handle: "x" }] }, "me", names), "You + 2");
  assert.equal(crewLabel(undefined, "me", names), "");
});
