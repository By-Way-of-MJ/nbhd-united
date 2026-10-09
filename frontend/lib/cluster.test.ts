// Spec for the cluster page's pure helpers — runnable with Node's built-in
// runner after a tsc transpile.
import { test } from "node:test";
import assert from "node:assert/strict";

import { AID_INTENTS, aidIntent, clusterEyebrow, memberRelation, memberRole, messageAuthor, messageDay, messagesByDay, ringPoints, sortedMembers, withReply, withStarter } from "./cluster";

test("starters: ask, offer, learn — the same three as the iPhone", () => {
  assert.deepEqual(AID_INTENTS.map((i) => i.title), ["Ask for support", "Offer a hand", "Learn together"]);
  assert.equal(aidIntent("offer")?.subtitle, "Time, a skill, or a listening ear.");
  assert.equal(aidIntent("nope"), undefined);
  assert.equal(aidIntent(null), undefined);
  const ask = AID_INTENTS[0];
  assert.equal(withStarter("", ask), ask.draft);
  assert.equal(withStarter("   \n", ask), ask.draft, "whitespace isn't a message");
  assert.equal(withStarter("Hello all\n", ask), `Hello all\n\n${ask.draft}`, "added under what you've already written");
});

test("reply: names who you're answering and quotes them briefly", () => {
  assert.equal(withReply("", "Maya", "I could use  a hand\nwith meals."), "Replying to Maya: “I could use a hand with meals.”\n\n");
  assert.equal(withReply("Thanks!", "Maya", "ok"), "Thanks!\n\nReplying to Maya: “ok”\n\n");
  assert.equal(withReply("", "", "x"), "Replying to a cluster member: “x”\n\n");
  const long = withReply("", "Jordan", "a".repeat(400));
  assert.equal(long.length, "Replying to Jordan: “”\n\n".length + 180, "long messages are cut at 180 characters");
});

test("conversation: who wrote it, and the day it was written", () => {
  assert.equal(messageAuthor({ mine: true, author: { handle: "y", display_name: "Yuki", avatar_hue: 1 } }), "You");
  assert.equal(messageAuthor({ mine: false, author: { handle: "m", display_name: "Maya", avatar_hue: 1 } }), "Maya");
  assert.equal(messageAuthor({ mine: false }), "Cluster member", "older servers send no author");

  const now = new Date(2026, 9, 1, 15, 0);
  assert.equal(messageDay(new Date(2026, 9, 1, 0, 5).toISOString(), now), "Today");
  assert.equal(messageDay(new Date(2026, 8, 30, 23, 55).toISOString(), now), "Yesterday");
  assert.equal(messageDay(new Date(2026, 8, 28, 9, 0).toISOString(), now), "Mon, Sep 28");
  assert.equal(messageDay(new Date(2025, 11, 31, 9, 0).toISOString(), now), "Wed, Dec 31, 2025");
  assert.equal(messageDay("not a date", now), "");

  const at = (d: Date) => ({ created_at: d.toISOString() });
  const runs = messagesByDay([at(new Date(2026, 8, 28, 9)), at(new Date(2026, 8, 28, 10)), at(new Date(2026, 8, 30, 8)), at(new Date(2026, 9, 1, 8))], now);
  assert.deepEqual(runs.map((r) => [r.day, r.messages.length]), [["Mon, Sep 28", 2], ["Yesterday", 1], ["Today", 1]]);
  assert.deepEqual(messagesByDay([], now), []);
});

test("people: the heading, roles, order, and what sharing a cluster does not mean", () => {
  assert.equal(clusterEyebrow(7, true), "Cluster · 7 people · you host");
  assert.equal(clusterEyebrow(1, false), "Cluster · 1 person");
  assert.equal(memberRole({ role: "admin" }), "Cluster host");
  assert.equal(memberRole({ role: "member" }), "Cluster member");

  const m = (display_name: string, role: "admin" | "member", is_me = false) => ({ display_name, role, is_me, handle: display_name.toLowerCase(), avatar_hue: 0 });
  assert.deepEqual(sortedMembers([m("Taro", "member"), m("Kiho", "admin"), m("Yuki", "member", true), m("Emi", "member")]).map((x) => x.display_name), ["Yuki", "Kiho", "Emi", "Taro"]);

  const neighbors = [{ handle: "Kiho" }];
  assert.equal(memberRelation(m("Yuki", "member", true), neighbors), "me");
  assert.equal(memberRelation(m("Kiho", "admin"), neighbors), "neighbor", "handles compare without case");
  assert.equal(memberRelation(m("Emi", "member"), neighbors), "member", "sharing a cluster isn't being neighbors");
  assert.equal(memberRelation({ is_me: false, handle: null }, neighbors), "member");
});

test("map: members sit evenly on a ring, the first at the top", () => {
  const points = ringPoints(4, 100, 100, 50);
  const near = (a: number, b: number) => Math.abs(a - b) < 1e-9;
  assert.ok(near(points[0].x, 100) && near(points[0].y, 50));
  assert.ok(near(points[1].x, 150) && near(points[1].y, 100));
  assert.ok(near(points[2].x, 100) && near(points[2].y, 150));
  assert.ok(near(points[3].x, 50) && near(points[3].y, 100));
  assert.deepEqual(ringPoints(0, 0, 0, 10), []);
  // Nobody shares a spot, whatever the size.
  for (const n of [1, 2, 3, 5, 6]) {
    const ring = ringPoints(n, 0, 0, 100);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) assert.ok(Math.hypot(ring[i].x - ring[j].x, ring[i].y - ring[j].y) > 50);
  }
});
