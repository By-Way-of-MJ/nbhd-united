// Spec for the "Your people" star-cluster model. Pure functions only —
// runnable with Node's built-in runner after a tsc transpile.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildPeopleSky,
  closeness,
  findPeople,
  GLIMMER_BUDGET,
  glimmerCount,
  homePose,
  nearAngle,
  pickLabels,
  reachLine,
  reachTotalLine,
  type SkyClusterInput,
  type SkyPersonInput,
} from "./layout";

const BONDS = ["light", "steady", "strong"] as const;
function friends(n: number, sky = 0, reach: string | null = "10+"): SkyPersonInput[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `f-${i}`,
    name: `Friend ${i}`,
    inSky: i < sky,
    bond: BONDS[i % 3],
    hue: (i * 37) % 360,
    reach,
    clusters: [],
  }));
}
const dist = (p: number[]) => Math.hypot(p[0], p[1], p[2]);

test("layout is deterministic for the same input, in any order", () => {
  const input = friends(40, 5);
  const a = buildPeopleSky(input, []);
  const b = buildPeopleSky([...input].reverse(), []);
  const pos = (m: typeof a) => Object.fromEntries(m.people.map((p) => [p.id, p.pos.map((v) => v.toFixed(6)).join(",")]));
  assert.deepEqual(pos(a), pos(b));
  assert.deepEqual(a.glimmers.length, b.glimmers.length);
});

test("in-your-sky friends sit nearest, light bonds furthest", () => {
  const m = buildPeopleSky(friends(60, 8), []);
  const avg = (f: (p: (typeof m.people)[number]) => boolean) => {
    const xs = m.people.filter(f).map((p) => dist(p.pos));
    return xs.reduce((a, b) => a + b, 0) / xs.length;
  };
  const sky = avg((p) => p.inSky), strong = avg((p) => !p.inSky && p.bond === "strong"), light = avg((p) => !p.inSky && p.bond === "light");
  assert.ok(sky < strong, `sky ${sky} < strong ${strong}`);
  assert.ok(strong < light, `strong ${strong} < light ${light}`);
  assert.equal(closeness({ inSky: true, bond: "light" }), 1);
});

test("no two friends overlap and nobody sits on you", () => {
  for (const n of [1, 2, 30, 300]) {
    const m = buildPeopleSky(friends(n, Math.min(n, 10)), []);
    assert.equal(m.people.length, n);
    for (const p of m.people) assert.ok(dist(p.pos) >= 2.1, `friend ${p.id} too close to you`);
    for (let a = 0; a < m.people.length; a++) {
      for (let b = a + 1; b < m.people.length; b++) {
        const P = m.people[a].pos, Q = m.people[b].pos;
        assert.ok(Math.hypot(P[0] - Q[0], P[1] - Q[1], P[2] - Q[2]) > 0.6, `n=${n}: ${a},${b} overlap`);
      }
    }
    assert.ok(Number.isFinite(m.extent) && m.extent > 0);
    assert.ok(homePose(m).dist > m.extent, "home camera frames everyone");
  }
});

test("cluster members gather around their nebula; a friend in two clusters joins the bigger", () => {
  const people = friends(30, 4);
  const clusters: SkyClusterInput[] = [
    { id: "small", name: "Book swap", hue: 30, memberIds: ["f-1", "f-2", "f-3"] },
    { id: "big", name: "Building 3", hue: 210, memberIds: ["f-3", "f-4", "f-5", "f-6", "f-7", "ghost"] },
  ];
  const m = buildPeopleSky(people, clusters);
  assert.deepEqual(m.clusters.map((c) => c.id), ["big", "small"]);
  const f3 = m.people.find((p) => p.id === "f-3")!;
  assert.equal(m.clusters[f3.cluster].id, "big");
  for (const c of m.clusters) {
    for (const i of c.members) {
      const p = m.people[i].pos;
      const d = Math.hypot(p[0] - c.centre[0], p[1] - c.centre[1], p[2] - c.centre[2]);
      assert.ok(d <= c.radius + 1e-9, `${m.people[i].id} outside ${c.id}`);
    }
  }
  // Unknown member ids are ignored, empty clusters dropped.
  assert.equal(buildPeopleSky(people, [{ id: "none", name: "x", hue: 0, memberIds: ["nobody"] }]).clusters.length, 0);
});

test("friends-of-friends come only from a known reach bucket, within a budget", () => {
  assert.equal(glimmerCount(null), 0);
  assert.equal(glimmerCount(undefined), 0);
  assert.equal(glimmerCount("7"), 0);
  assert.equal(buildPeopleSky(friends(10, 2, null), []).glimmers.length, 0);
  const few = buildPeopleSky(friends(2, 1, "10+"), []);
  assert.equal(few.glimmers.length, 16);
  assert.ok(few.glimmers.every((g) => g.via === 0 || g.via === 1));
  const many = buildPeopleSky(friends(300, 10, "100+"), []);
  assert.ok(many.glimmers.length <= GLIMMER_BUDGET + 300, `${many.glimmers.length} glimmers`);
  assert.ok(many.glimmers.length > 300);
});

test("label placement caps, prefers priority and never overlaps", () => {
  const cands = Array.from({ length: 30 }, (_, i) => ({ key: `k${i}`, x: 50 + (i % 6) * 120, y: 40 + Math.floor(i / 6) * 60, w: 90, h: 20, priority: i }));
  const out = pickLabels(cands, 10, 800, 400);
  assert.equal(out.length, 10);
  assert.equal(out[0], "k29");
  // Two labels on the same spot: only the higher priority survives.
  assert.deepEqual(pickLabels([{ key: "a", x: 100, y: 100, w: 80, h: 20, priority: 1 }, { key: "b", x: 110, y: 104, w: 80, h: 20, priority: 2 }], 10, 400, 400), ["b"]);
  // A forced label (selection) always shows, even off-canvas or over another.
  assert.deepEqual(pickLabels([{ key: "a", x: 100, y: 100, w: 80, h: 20, priority: 9 }, { key: "sel", x: 104, y: 100, w: 80, h: 20, priority: 0, force: true }], 10, 400, 400), ["sel"]);
  // Alternatives: the first of a group that fits wins, the rest are skipped.
  assert.deepEqual(pickLabels([{ key: "block", x: 100, y: 100, w: 80, h: 20, priority: 9 }, { key: "c:0", group: "c", x: 104, y: 100, w: 80, h: 20, priority: 5 }, { key: "c:1", group: "c", x: 100, y: 200, w: 80, h: 20, priority: 4 }, { key: "c:2", group: "c", x: 100, y: 300, w: 80, h: 20, priority: 3 }], 10, 400, 400), ["block", "c:1"]);
  // Off the canvas edge: dropped.
  assert.deepEqual(pickLabels([{ key: "edge", x: 5, y: 100, w: 80, h: 20, priority: 1 }], 10, 400, 400), []);
});

test("search: first-name matches first, then word starts, then anywhere / handle", () => {
  const ppl = [{ name: "Noor Haddad", handle: "noor" }, { name: "Aiko", handle: "aiko" }, { name: "Mateo García", handle: "mg" }, { name: "Kiho", handle: "garcia_k" }];
  assert.deepEqual(findPeople(ppl, "gar").map((p) => p.name), ["Mateo García", "Kiho"]);
  assert.deepEqual(findPeople(ppl, "  ").length, 0);
  assert.deepEqual(findPeople(ppl, "ai").map((p) => p.name), ["Aiko"]);
});

test("reach copy only for known buckets", () => {
  assert.equal(reachLine("10+", "Kiho Tanaka"), "10+ people you don’t know are one step away through Kiho.");
  assert.equal(reachLine(null, "Kiho"), "");
  assert.equal(reachLine("12", "Kiho"), "");
  assert.equal(reachTotalLine("50+"), "Through them, 50+ more people are one step away.");
  assert.equal(reachTotalLine(undefined), "");
});

test("yaw flights take the short way round", () => {
  assert.ok(Math.abs(nearAngle(0.1, Math.PI * 2 - 0.1) - -0.1) < 1e-9);
  assert.ok(Math.abs(nearAngle(3, 3.5) - 3.5) < 1e-9);
});
