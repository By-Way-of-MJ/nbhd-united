// Spec for the constellation night-sky model. Pure functions only — runnable
// with Node's built-in runner after a tsc transpile.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ageToDistance,
  ALT_MAX,
  ALT_MIN,
  angDiff,
  angularGap,
  assignDirections,
  brightestStars,
  buildSky,
  coilSeparation,
  detailTarget,
  FEATURED_STARS,
  fogFor,
  FULL_NEIGHBORS,
  MAX_LABELS,
  MIN_DEPTH_GAP,
  NEAR_DEPTH,
  orderMatches,
  pairStep,
  pickLabels,
  rankDepths,
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

test("rank depths: nearest first, evenly spaced, never closer than the minimum gap", () => {
  const recent = Array.from({ length: 53 }, (_, i) => Math.floor(i / 4)); // many from the same few weeks
  const d = rankDepths(recent);
  assert.equal(d[0], NEAR_DEPTH);
  for (let i = 1; i < d.length; i++) assert.ok(d[i] - d[i - 1] >= MIN_DEPTH_GAP - 1e-9, `gap ${i}: ${(d[i] - d[i - 1]).toFixed(2)}`);
  assert.ok(d[d.length - 1] >= NEAR_DEPTH * 10, "a big sky reaches ~10× deeper than the nearest");
  // A sparse sky spreads over the whole depth range, and a year-old one still sits far out.
  const six = rankDepths([1, 30, 60, 120, 200, 365]);
  assert.ok(six[5] >= NEAR_DEPTH * 6 && six[5] >= ageToDistance(365));
  for (let i = 1; i < six.length; i++) assert.ok(six[i] > six[i - 1]);
  // Big constellations are never so close they fill the sky.
  assert.ok(rankDepths([0], [10])[0] >= 16);
});

for (const n of [6, 53, 150]) {
  test(`directions for ${n} constellations: inside the dome band, no overlaps, gentle turns`, () => {
    const keys = Array.from({ length: n }, (_, i) => i * 7 + 1);
    const dirs = assignDirections(keys);
    const sep = coilSeparation(n);
    assert.ok(sep >= (n <= 6 ? 28 : n <= 60 ? 12 : 6), `spacing ${sep.toFixed(1)}° for ${n}`);
    for (const d of dirs) assert.ok(d.alt >= ALT_MIN && d.alt <= ALT_MAX);
    for (let i = 0; i < n; i++)
      for (let j = i + 1; j < n; j++) {
        const g = angularGap(dirs[i], dirs[j]);
        assert.ok(g > sep * 0.5, `constellations ${i},${j} overlap (${g.toFixed(1)}° < half of ${sep.toFixed(1)}°)`);
      }
    // Next is a small turn, never a swing across the sky.
    for (let i = 1; i < n; i++) assert.ok(angularGap(dirs[i - 1], dirs[i]) < 45);
    assert.deepEqual(assignDirections(keys), dirs, "deterministic");
  });
}

test("big neighbours turn far enough apart not to overlap", () => {
  assert.ok(pairStep(10, 8, 16, 22) > 45);
  assert.ok(pairStep(3, 3, 200, 206) < 5);
  const dirs = assignDirections([1, 2, 3], [60, 0, 0]);
  assert.ok(angularGap(dirs[0], dirs[1]) > 50);
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

test("navigation order: newest first, every Next is deeper; wraps", () => {
  const sky = buildSky(fixture(), NOW);
  for (let i = 1; i < sky.clusters.length; i++) {
    const a = sky.clusters[i - 1], b = sky.clusters[i];
    assert.ok(a.ageDays <= b.ageDays);
    assert.ok(b.dist - a.dist >= MIN_DEPTH_GAP - 1e-9);
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

function bigFixture(): SkyInput {
  const nodes = [];
  let id = 1;
  const sizes = [30, 24, 19, 16, 15, ...Array.from({ length: 48 }, (_, i) => [2, 3, 2, 4, 3, 6, 2, 3, 5, 2, 4, 3][i % 12])];
  for (let c = 0; c < sizes.length; c++) {
    const newest = c % 10 < 7 ? (c * 13) % 90 : 90 + ((c * 37) % 275);
    for (let i = 0; i < sizes[c]; i++) nodes.push({ id: id++, text: `L${id}`, cluster_id: c + 1, created_at: daysAgo(newest + i * 2) });
  }
  return { nodes, clusters: sizes.map((_, c) => ({ id: c + 1, label: `C${c}` })) };
}

test("a real-sized sky (~260 lessons, 53 constellations) spreads out in depth and direction", () => {
  const sky = buildSky(bigFixture(), NOW);
  assert.equal(sky.clusters.length, 53);
  assert.equal(sky.lessonCount, 260);
  const d = sky.clusters.map((c) => c.dist);
  assert.ok(d[d.length - 1] / d[0] >= 10);
  for (const c of sky.clusters) {
    assert.ok(c.featured.length === Math.min(FEATURED_STARS, c.lessons.length));
    assert.equal(c.featuredLinks.length, Math.max(0, c.featured.length - 1));
    for (const [a, b] of c.featuredLinks) assert.ok(c.featured.includes(a) && c.featured.includes(b));
  }
});

test("brightest stars: capped, brightest first, stable ties", () => {
  assert.deepEqual(brightestStars([1, 3, 2, 3, 0.5], 2), [1, 3]);
  assert.deepEqual(brightestStars([1, 2], 12), [0, 1]);
  assert.equal(brightestStars(Array.from({ length: 30 }, (_, i) => i % 7), FEATURED_STARS).length, FEATURED_STARS);
});

test("level of detail: the chosen constellation and its nearest neighbours in the Next order are full", () => {
  const full = Array.from({ length: 53 }, (_, i) => detailTarget(i, 10)).filter((v) => v === 1).length;
  assert.equal(full, 2 * FULL_NEIGHBORS + 1);
  assert.equal(detailTarget(10, 10), 1);
  assert.equal(detailTarget(10 + FULL_NEIGHBORS + 1, 10), 0);
  assert.equal(detailTarget(0, -1), 0);
  assert.equal(Array.from({ length: 53 }, (_, i) => detailTarget(i, 0)).filter((v) => v === 1).length, FULL_NEIGHBORS + 1);
});

test("depth haze: clear up close, fading with distance, never gone", () => {
  assert.equal(fogFor(10), 1);
  let prev = 2;
  for (const d of [20, 40, 80, 160, 320]) {
    const f = fogFor(d);
    assert.ok(f < prev && f >= 0.12);
    prev = f;
  }
});

test("labels: capped, chosen first, then matches, then nearest", () => {
  const cands = Array.from({ length: 20 }, (_, i) => ({ index: i, camDist: 100 - i, match: i === 3 }));
  const picked = pickLabels(cands, 7);
  assert.equal(picked.length, MAX_LABELS);
  assert.deepEqual(picked, [7, 3, 19, 18, 17]);
  assert.deepEqual(pickLabels([], 0), []);
});

test("search matches step along the path: best, its constellation, deeper ones, then back nearer", () => {
  const where = new Map([
    [1, { rank: 5, lesson: 2 }],
    [2, { rank: 5, lesson: 0 }],
    [3, { rank: 9, lesson: 1 }],
    [4, { rank: 2, lesson: 0 }],
    [5, { rank: 7, lesson: 0 }],
    [6, { rank: 4, lesson: 0 }],
  ]);
  assert.deepEqual(orderMatches([1, 3, 4, 5, 6, 2, 99], where), [1, 2, 5, 3, 6, 4]);
  assert.deepEqual(orderMatches([4], where), [4]);
  assert.deepEqual(orderMatches([], where), []);
});
