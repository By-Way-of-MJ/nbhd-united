/**
 * Chart your galaxy — star placement. Pure, deterministic, no DOM.
 *
 * Every star is a real lesson from GET /api/v1/lessons/galaxy/. Clusters get a
 * centroid on a flattened spiral (x wide, y shallow — a galactic plane), each
 * lesson sits inside its cluster (real x/y when the backend has them, else a
 * seeded ring) and depth comes from a hash of the id, so the same galaxy always
 * lays out the same way and the corner map matches what you fly through.
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

export const Z_MIN = 200;
const GOLDEN_ANGLE = 2.399963;

export function stageSize(stage: string): number {
  return STAGE_SIZE[stage as StarStage] ?? STAGE_SIZE.proto;
}

export function stageGlow(stage: string): number {
  return STAGE_GLOW[stage as StarStage] ?? STAGE_GLOW.proto;
}

/** Corridor length grows with the galaxy so a big one never feels crowded. */
export function depthFor(count: number): number {
  return Math.max(3600, count * 20);
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

  const clusters: FlightCluster[] = keys.map((key, i) => {
    const members = groups.get(key) ?? [];
    const a = i * GOLDEN_ANGLE + 0.9;
    const r = 220 + 150 * Math.sqrt(i);
    return {
      key,
      id: key === "other" ? null : Number(key),
      label: members.find((m) => m.cluster_label)?.cluster_label || "Unsorted",
      rgb: key === "other" ? UNSORTED_TINT : CLUSTER_TINTS[i % CLUSTER_TINTS.length],
      cx: Math.cos(a) * r,
      cy: Math.sin(a) * r * 0.5,
      count: members.length,
    };
  });

  const depth = depthFor(stars.length);
  const placed: PlacedStar[] = [];
  clusters.forEach((c, ci) => {
    const members = [...(groups.get(c.key) ?? [])].sort((a, b) => a.id - b.id);
    const xs = members.map((m) => m.x).filter((v): v is number => v != null);
    const ys = members.map((m) => m.y).filter((v): v is number => v != null);
    const haveXY = xs.length === members.length && ys.length === members.length && members.length >= 2;
    const minX = haveXY ? Math.min(...xs) : 0, maxX = haveXY ? Math.max(...xs) : 1;
    const minY = haveXY ? Math.min(...ys) : 0, maxY = haveXY ? Math.max(...ys) : 1;
    const spread = 150 + 14 * Math.sqrt(members.length);
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
      const z = Z_MIN + hsh(seed, ci, 3) * (depth - Z_MIN);
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
