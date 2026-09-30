// Spec for the flight's star placement. Pure functions only — runnable with
// Node's built-in runner after a tsc transpile (mirrors journal-date.test.ts).
import { test } from "node:test";
import assert from "node:assert/strict";

import type { GalaxyData, GalaxyStar, StarStage } from "../constellation-game/encounter-logic";
import { connectedIndices, depthFor, layoutGalaxy, stageGlow, stageSize, Z_MIN } from "./layout";

function star(id: number, cluster: number | null, stage: StarStage = "proto", xy?: [number, number]): GalaxyStar {
  return {
    id,
    text: `Lesson ${id}`,
    tags: [],
    cluster_id: cluster,
    cluster_label: cluster == null ? "" : `Cluster ${cluster}`,
    star_stage: stage,
    x: xy ? xy[0] : null,
    y: xy ? xy[1] : null,
    journal_count: 0,
    connection_count: 0,
    last_tutored_at: null,
    last_visited_at: null,
    galaxy_note: "",
    source_type: "journal",
    created_at: "2026-09-01T00:00:00Z",
  };
}

function galaxy(): GalaxyData {
  const stars: GalaxyStar[] = [];
  for (let i = 1; i <= 60; i++) stars.push(star(i, (i % 5) + 1, (["proto", "ignited", "radiant", "supernova"] as StarStage[])[i % 4], [i * 7, (i * 13) % 100]));
  stars.push(star(61, null));
  return { stars, edges: [{ source: 1, target: 6, similarity: 0.5, connection_type: "similar" }, { source: 11, target: 1, similarity: 0.9, connection_type: "builds_on" }] };
}

test("layoutGalaxy is deterministic and keeps every star", () => {
  const a = layoutGalaxy(galaxy()), b = layoutGalaxy(galaxy());
  assert.equal(a.stars.length, 61);
  assert.deepEqual(a.stars.map((s) => [s.x, s.y, s.z]), b.stars.map((s) => [s.x, s.y, s.z]));
  for (const s of a.stars) assert.equal(a.byId.get(s.star.id), s.index);
});

test("clusters get distinct centroids; unsorted stars come last with their own tint", () => {
  const g = layoutGalaxy(galaxy());
  assert.equal(g.clusters.length, 6);
  const seen = new Set(g.clusters.map((c) => `${c.cx.toFixed(1)},${c.cy.toFixed(1)}`));
  assert.equal(seen.size, 6);
  assert.equal(g.clusters[5].id, null);
  assert.equal(g.clusters[5].label, "Unsorted");
  assert.equal(g.clusters[0].label, "Cluster 1");
  assert.equal(g.clusters[0].count, 12);
});

test("stars sit inside their cluster and within the corridor depth", () => {
  const g = layoutGalaxy(galaxy());
  for (const s of g.stars) {
    const c = g.clusters[s.cluster];
    assert.ok(Math.abs(s.x - c.cx) < 260, `x offset too large for ${s.star.id}`);
    assert.ok(Math.abs(s.y - c.cy) < 200, `y offset too large for ${s.star.id}`);
    assert.ok(s.z >= Z_MIN && s.z <= g.depth, `z out of range for ${s.star.id}`);
  }
  assert.equal(g.depth, depthFor(61));
});

test("depth grows with the galaxy but never below the floor", () => {
  assert.equal(depthFor(10), 3600);
  assert.equal(depthFor(250), 5000);
});

test("stage drives size and brightness; unknown stages fall back to proto", () => {
  assert.ok(stageSize("supernova") > stageSize("radiant"));
  assert.ok(stageSize("radiant") > stageSize("ignited"));
  assert.ok(stageSize("ignited") > stageSize("proto"));
  assert.equal(stageSize("nonsense"), stageSize("proto"));
  assert.equal(stageGlow("nonsense"), stageGlow("proto"));
  const g = layoutGalaxy(galaxy());
  const proto = g.stars.find((s) => s.star.star_stage === "proto") as NonNullable<(typeof g.stars)[number]>;
  const nova = g.stars.find((s) => s.star.star_stage === "supernova") as NonNullable<(typeof g.stars)[number]>;
  assert.ok(nova.size > proto.size);
});

test("connectedIndices follows real edges in both directions, strongest first, deduped", () => {
  const g = layoutGalaxy(galaxy());
  const out = connectedIndices(g, galaxy().edges, 1);
  assert.deepEqual(out.map((i) => g.stars[i].star.id), [11, 6]);
  assert.deepEqual(connectedIndices(g, galaxy().edges, 2), []);
  assert.deepEqual(connectedIndices(g, [{ source: 1, target: 1, similarity: 1, connection_type: "self" }], 1), []);
});

test("an empty galaxy lays out to nothing without throwing", () => {
  const g = layoutGalaxy({ stars: [], edges: [] });
  assert.equal(g.stars.length, 0);
  assert.equal(g.clusters.length, 0);
});
