// Spec for the constellation night-sky model. Pure functions only — runnable
// with Node's built-in runner after a tsc transpile.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ageToDistance,
  angDiff,
  assignDirections,
  buildSky,
  easeInOut,
  easeSpeed,
  filterSearchHits,
  HARD_MAX_MOVE_S,
  lerpPose,
  lightYearsFor,
  lightYearsLabel,
  LOOK_BELOW,
  LOOSE_KEY,
  matchLine,
  MAX_TURN_DEG_PER_S,
  minSeparation,
  MIN_MOVE_S,
  moveDuration,
  navHint,
  poseFor,
  sourceLine,
  startIndex,
  stepIndex,
  turnAngle,
  whenLabel,
  type Pose,
  type SkyInput,
} from "./model";

const NOW = new Date("2026-09-30T12:00:00Z");
const daysAgo = (d: number) => new Date(NOW.getTime() - d * 864e5).toISOString();

function fixture(): SkyInput {
  const nodes = [];
  let id = 1;
  const ages = [2, 20, 45, 120, 360];
  for (let c = 0; c < ages.length; c++) {
    for (let i = 0; i < 4 + c; i++) nodes.push({ id: id++, text: `Lesson ${id}`, cluster_id: c + 10, created_at: daysAgo(ages[c] + i * 3) });
  }
  nodes.push({ id: id++, text: "A lone one", cluster_id: null, created_at: daysAgo(9) });
  return { nodes, clusters: ages.map((_, c) => ({ id: c + 10, label: `Cluster ${c}` })) };
}

test("age → depth is monotonic: older is farther, this week is close", () => {
  let prev = -1;
  for (const d of [0, 1, 3, 7, 14, 30, 90, 180, 365, 730]) {
    const dist = ageToDistance(d);
    assert.ok(dist > prev, `distance must grow with age (${d}d)`);
    prev = dist;
  }
  assert.ok(ageToDistance(3) < 20, "this week is close");
  assert.ok(ageToDistance(365) > 70, "last year is deep");
});

test("light-years: a week each, never zero, non-decreasing, readable label", () => {
  assert.equal(lightYearsFor(0), 1);
  assert.equal(lightYearsFor(6), 1);
  assert.equal(lightYearsFor(14), 2);
  assert.equal(lightYearsFor(365), 52);
  let prev = 0;
  for (let d = 0; d < 800; d += 5) {
    const ly = lightYearsFor(d);
    assert.ok(ly >= prev);
    prev = ly;
  }
  assert.equal(lightYearsLabel(1), "1 light-year out");
  assert.equal(lightYearsLabel(12), "12 light-years out");
});

test("when labels read like a person would say them", () => {
  assert.equal(whenLabel(daysAgo(2), NOW), "this week");
  assert.equal(whenLabel(daysAgo(10), NOW), "last week");
  assert.equal(whenLabel("2026-07-10T09:00:00Z", NOW), "July");
  assert.equal(whenLabel("2025-12-03T09:00:00Z", NOW), "December 2025");
});

test("cluster directions are deterministic and independent of input order", () => {
  const a = assignDirections([3, 1, 2, 9, 40]);
  const b = assignDirections([40, 9, 2, 1, 3]);
  for (const k of [1, 2, 3, 9, 40]) assert.deepEqual(a.get(k), b.get(k));
});

test("cluster directions are spread apart", () => {
  const keys = Array.from({ length: 12 }, (_, i) => i * 7 + 1);
  const dirs = assignDirections(keys);
  const sep = minSeparation(keys.length);
  const pts = [...dirs.values()];
  for (let i = 0; i < pts.length; i++)
    for (let j = i + 1; j < pts.length; j++) {
      const cosAlt = Math.cos((((pts[i].alt + pts[j].alt) / 2) * Math.PI) / 180);
      const d = Math.hypot(angDiff(pts[i].az, pts[j].az) * cosAlt, pts[j].alt - pts[i].alt);
      assert.ok(d > sep * 0.8, `clusters ${i},${j} overlap (${d.toFixed(1)}° < ${sep.toFixed(1)}°)`);
    }
});

test("buildSky: deterministic placement, older clusters deeper, loose lessons grouped", () => {
  const one = buildSky(fixture(), NOW);
  const two = buildSky({ ...fixture(), nodes: [...fixture().nodes].reverse() }, NOW);
  assert.deepEqual(one, two);
  const byKey = new Map(one.clusters.map((c) => [c.key, c]));
  assert.ok(byKey.get(10)!.dist < byKey.get(11)!.dist);
  assert.ok(byKey.get(13)!.dist < byKey.get(14)!.dist);
  assert.equal(byKey.get(LOOSE_KEY)!.lessons.length, 1);
  assert.equal(byKey.get(14)!.when, "October 2025");
  for (const c of one.clusters) {
    // Lessons step oldest → newest; the figure is a tree over every star.
    for (let i = 1; i < c.lessons.length; i++) assert.ok(c.lessons[i - 1].createdAt <= c.lessons[i].createdAt);
    assert.equal(c.links.length, Math.max(0, c.lessons.length - 1));
  }
});

test("navigation order: nearer depth bands first, then by direction; wraps", () => {
  const sky = buildSky(fixture(), NOW);
  for (let i = 1; i < sky.clusters.length; i++) {
    const a = sky.clusters[i - 1], b = sky.clusters[i];
    assert.ok(Math.floor((a.lightYears - 1) / 4) <= Math.floor((b.lightYears - 1) / 4));
  }
  assert.equal(stepIndex(0, -1, 5), 4);
  assert.equal(stepIndex(4, 1, 5), 0);
});

test("the sky opens on the nearest constellation", () => {
  assert.equal(startIndex([{ dist: 30 }, { dist: 14 }, { dist: 20 }]), 1);
  assert.equal(startIndex([]), 0);
});

test("hints say where the camera will go", () => {
  const here = { az: 10, dist: 20 };
  assert.equal(navHint(here, { az: 15, dist: 40 }), "deeper");
  assert.equal(navHint(here, { az: 5, dist: 12 }), "closer");
  assert.equal(navHint(here, { az: 80, dist: 20.5 }), "turn right");
  assert.equal(navHint(here, { az: 300, dist: 20 }), "turn left");
  assert.equal(navHint(here, { az: 90, dist: 60 }), "turn right, deeper");
  assert.equal(navHint({ az: 350, dist: 30 }, { az: 20, dist: 14 }), "turn right, closer");
  assert.equal(navHint(here, { az: 12, dist: 21 }), "right beside");
});

test("easing: ease-in-out with no overshoot and zero speed at the ends", () => {
  assert.equal(easeInOut(0), 0);
  assert.equal(easeInOut(1), 1);
  assert.equal(easeInOut(0.5), 0.5);
  let prev = 0;
  for (let t = 0; t <= 1.0001; t += 0.01) {
    const e = easeInOut(t);
    assert.ok(e >= prev - 1e-12 && e <= 1, "monotonic, never past 1");
    prev = e;
  }
  assert.equal(easeSpeed(0), 0);
  assert.equal(easeSpeed(1), 0);
  assert.ok(Math.abs(easeSpeed(0.5) - 1) < 1e-9);
});

test("move duration: 2.5–4 s by distance, stretched to cap the turn rate", () => {
  const at = (az: number, alt: number, p: [number, number, number] = [0, 0, 0]): Pose => ({ p, az, alt });
  assert.equal(moveDuration(at(0, 20), at(0, 20)), MIN_MOVE_S);
  const far = moveDuration(at(0, 20), at(0, 20, [0, 0, 80]));
  assert.ok(far > 3.9 && far <= 4 + 1e-9);
  for (const turn of [10, 30, 60, 90]) {
    const a = at(0, 20), b = at(turn, 20);
    const d = moveDuration(a, b);
    const peak = (1.5 * turnAngle(a, b)) / d;
    assert.ok(peak <= MAX_TURN_DEG_PER_S + 1e-9, `${turn}° turn peaks at ${peak.toFixed(1)}°/s`);
  }
  assert.ok(moveDuration(at(0, 20), at(180, 20)) <= HARD_MAX_MOVE_S);
});

test("lerpPose takes the short way round and lands exactly", () => {
  const a: Pose = { p: [0, 0, 0], az: 350, alt: 10 };
  const b: Pose = { p: [10, 0, 0], az: 20, alt: 30 };
  assert.equal(lerpPose(a, b, 0.5).az, 5);
  assert.deepEqual(lerpPose(a, b, 1), { p: [10, 0, 0], az: 20, alt: 30 });
});

test("poseFor stands back from the constellation and looks slightly down at it", () => {
  const p = poseFor({ az: 0, alt: 0, dist: 40, view: 12 });
  assert.deepEqual(p.p.map((v) => Math.round(v * 1e6) / 1e6), [0, 0, 28]);
  assert.equal(p.alt, -LOOK_BELOW);
});

test("search: scored hits keep only close matches near the best", () => {
  const known = new Set([1, 2, 3, 4, 5, 6, 7]);
  assert.deepEqual(
    filterSearchHits([{ id: 1, similarity: 0.45 }, { id: 2, similarity: 0.4 }, { id: 3, similarity: 0.3 }, { id: 4, similarity: 0.2 }, { id: 99, similarity: 0.5 }], known),
    [1, 2],
  );
  assert.deepEqual(filterSearchHits([{ id: 1, similarity: 0.2 }, { id: 2, similarity: 0.1 }], known), []);
  assert.deepEqual(filterSearchHits([1, 2, 3, 4, 5, 6, 7].map((id) => ({ id })), known), [1, 2, 3, 4, 5]);
  assert.deepEqual(filterSearchHits([], known), []);
});

test("match line copy", () => {
  assert.equal(matchLine(0, 0, 0), "Nothing close to that yet");
  assert.equal(matchLine(1, 1, 0), "1 lesson in 1 constellation");
  assert.equal(matchLine(4, 2, 1), "4 lessons in 2 constellations · 2 of 4");
});

test("source line", () => {
  assert.equal(sourceLine("journal", "2026-09-12T10:00:00"), "From your journal · 12 Sep 2026");
  assert.equal(sourceLine("", "2026-09-12T10:00:00"), "12 Sep 2026");
});
