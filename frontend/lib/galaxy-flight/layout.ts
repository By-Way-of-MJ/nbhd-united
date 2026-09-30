/**
 * Chart your galaxy — star placement. Pure, deterministic, no DOM.
 *
 * Every star is a real lesson from GET /api/v1/lessons/galaxy/. The flight is a
 * looping corridor along z; each cluster owns a band of that corridor and sits
 * around the flight path (a centroid a little off the axis, stars spread across
 * it) so you fly beside and through one constellation after another. Within a
 * cluster a lesson sits at its real x/y when the backend has them, else on a
 * seeded ring; depth comes from a hash of the id. Deterministic: the same galaxy
 * always lays out the same way and the corner map matches what you fly through.
 */
import type { GalaxyData, GalaxyStar, StarStage } from "../constellation-game/encounter-logic";
import { hsh } from "../sky-art/noise";

export interface FlightCluster {
  key: string;
  id: number | null;
  label: string;
  /** "r,g,b" — pale tint used for the star sprite, the map dot and the panel label. */
  rgb: string;
  cx: number;
  cy: number;
  /** Centre of the cluster's band along the corridor. */
  cz: number;
  count: number;
}

export interface PlacedStar {
  star: GalaxyStar;
  index: number;
  cluster: number;
  x: number;
  y: number;
  z: number;
  /** Relative sprite size (1 = a proto star). */
  size: number;
  /** Brightness multiplier, 0..1. */
  glow: number;
}

export interface FlightGalaxy {
  stars: PlacedStar[];
  clusters: FlightCluster[];
  /** Length of the looping flight corridor along z. */
  depth: number;
  /** star id → index into `stars`. */
  byId: Map<number, number>;
}

/** Pale star tints, one per cluster (cycled). Pastel so they still read as light. */
export const CLUSTER_TINTS = [
  "196,230,255", // ice blue
  "255,232,205", // warm cream
  "221,215,255", // lavender (the accent's star cousin)
  "255,214,222", // rose
  "206,245,232", // mint
  "255,244,200", // pale gold
  "214,226,255", // periwinkle
  "246,220,255", // lilac
];
export const UNSORTED_TINT = "236,240,255";

/** How much a star has grown: size and brightness follow the real stage. */
export const STAGE_SIZE: Record<StarStage, number> = { proto: 1, ignited: 1.45, radiant: 2, supernova: 2.7 };
export const STAGE_GLOW: Record<StarStage, number> = { proto: 0.72, ignited: 0.86, radiant: 1, supernova: 1 };

const GOLDEN_ANGLE = 2.399963;
/** How far a cluster's centroid sits off the flight axis (min..max). */
const OFF_AXIS_MIN = 120, OFF_AXIS_MAX = 280;
/** A cluster's band overlaps its neighbours a little so there is never a gap. */
const BAND_OVERLAP = 1.25;

export function stageSize(stage: string): number {
  return STAGE_SIZE[stage as StarStage] ?? STAGE_SIZE.proto;
}

export function stageGlow(stage: string): number {
  return STAGE_GLOW[stage as StarStage] ?? STAGE_GLOW.proto;
}

/** Corridor length: room for every cluster to be flown through, more for big galaxies. */
export function depthFor(count: number, clusterCount = 1): number {
  return Math.max(3600, clusterCount * 900, count * 22);
}

function clusterKey(s: GalaxyStar): string {
  return s.cluster_id == null ? "other" : String(s.cluster_id);
}

export function layoutGalaxy(data: GalaxyData): FlightGalaxy {
  const stars = Array.isArray(data.stars) ? data.stars : [];
  const groups = new Map<string, GalaxyStar[]>();
  for (const s of stars) {
    const k = clusterKey(s);
    groups.set(k, [...(groups.get(k) ?? []), s]);
  }
  // Stable order: by cluster id, unsorted last.
  const keys = [...groups.keys()].sort((a, b) => (a === "other" ? 1 : b === "other" ? -1 : Number(a) - Number(b)));

  const depth = depthFor(stars.length, keys.length);
  const band = keys.length ? depth / keys.length : depth;
  const clusters: FlightCluster[] = keys.map((key, i) => {
    const members = groups.get(key) ?? [];
    const a = i * GOLDEN_ANGLE + 0.9;
    const r = OFF_AXIS_MIN + (OFF_AXIS_MAX - OFF_AXIS_MIN) * hsh(i, 3, 9);
    return {
      key,
      id: key === "other" ? null : Number(key),
      label: members.find((m) => m.cluster_label)?.cluster_label || "Unsorted",
      rgb: key === "other" ? UNSORTED_TINT : CLUSTER_TINTS[i % CLUSTER_TINTS.length],
      cx: Math.cos(a) * r,
      cy: Math.sin(a) * r * 0.7,
      cz: (i + 0.5) * band,
      count: members.length,
    };
  });

  const placed: PlacedStar[] = [];
  clusters.forEach((c, ci) => {
    const members = [...(groups.get(c.key) ?? [])].sort((a, b) => a.id - b.id);
    const xs = members.map((m) => m.x).filter((v): v is number => v != null);
    const ys = members.map((m) => m.y).filter((v): v is number => v != null);
    const haveXY = xs.length === members.length && ys.length === members.length && members.length >= 2;
    const minX = haveXY ? Math.min(...xs) : 0, maxX = haveXY ? Math.max(...xs) : 1;
    const minY = haveXY ? Math.min(...ys) : 0, maxY = haveXY ? Math.max(...ys) : 1;
    // Wide enough to reach across the axis, so the cluster surrounds you as you pass.
    const spread = 170 + 16 * Math.sqrt(members.length);
    members.forEach((m, mi) => {
      const seed = m.id | 0;
      let ox: number, oy: number;
      if (haveXY && maxX > minX && maxY > minY && m.x != null && m.y != null) {
        ox = (((m.x - minX) / (maxX - minX)) * 2 - 1) * spread;
        oy = (((m.y - minY) / (maxY - minY)) * 2 - 1) * spread * 0.6;
      } else {
        const ang = hsh(seed, ci, 5) * Math.PI * 2;
        const rad = Math.sqrt(hsh(seed, ci, 7)) * spread;
        ox = Math.cos(ang) * rad;
        oy = Math.sin(ang) * rad * 0.6;
      }
      // A little seeded jitter so grid-like inputs still read as a cloud.
      ox += (hsh(seed, mi, 11) - 0.5) * 50;
      oy += (hsh(seed, mi, 13) - 0.5) * 30;
      const z = (((c.cz + (hsh(seed, ci, 3) - 0.5) * band * BAND_OVERLAP) % depth) + depth) % depth;
      placed.push({
        star: m,
        index: placed.length,
        cluster: ci,
        x: c.cx + ox,
        y: c.cy + oy,
        z,
        size: stageSize(m.star_stage) * (0.9 + 0.2 * hsh(seed, 1, 17)),
        glow: stageGlow(m.star_stage),
      });
    });
  });

  const byId = new Map<number, number>();
  placed.forEach((p) => byId.set(p.star.id, p.index));
  return { stars: placed, clusters, depth, byId };
}

/** Real edges touching a star, as the other star's index (deduped, strongest first). */
export function connectedIndices(galaxy: FlightGalaxy, edges: GalaxyData["edges"], starId: number, limit = 4): number[] {
  const seen = new Set<number>();
  const out: { idx: number; sim: number }[] = [];
  for (const e of Array.isArray(edges) ? edges : []) {
    const other = e.source === starId ? e.target : e.target === starId ? e.source : null;
    if (other == null || other === starId) continue;
    const idx = galaxy.byId.get(other);
    if (idx == null || seen.has(idx)) continue;
    seen.add(idx);
    out.push({ idx, sim: typeof e.similarity === "number" ? e.similarity : 0 });
  }
  return out.sort((a, b) => b.sim - a.sim).slice(0, limit).map((o) => o.idx);
}
