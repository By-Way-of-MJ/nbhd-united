/**
 * "Your people" as a star cluster — the pure model (no DOM). You sit at the
 * origin; friends are placed in 3D so that closer and brighter means you share
 * more ("in your sky" nearest, then the strong / steady / light bond buckets);
 * clusters (the product name for Circles) pull their members together so a
 * soft nebula can be drawn around them; friends-of-friends are nameless
 * glimmers just beyond the friend they come through.
 *
 * Everything is deterministic from stable ids, so the same neighborhood always
 * draws the same sky, and adding one friend never reshuffles the rest of a shell.
 */

import { seeded } from "../sky-art/noise";

export type Vec3 = [number, number, number];
export type Bond = "light" | "steady" | "strong";

/** Bucketed friends-of-friends count from the API (never an exact number). */
export const REACH_BUCKETS = ["3+", "5+", "10+", "25+", "50+", "100+"] as const;
export type Reach = (typeof REACH_BUCKETS)[number];

export interface SkyPersonInput {
  id: string;
  name: string;
  handle?: string;
  inSky: boolean;
  bond: Bond;
  hue: number;
  reach?: string | null;
  /** Ids of the clusters this friend shares with you. */
  clusters: string[];
}

export interface SkyClusterInput {
  id: string;
  name: string;
  hue: number;
  /** Friend ids (only friends you can see; other members aren't placed). */
  memberIds: string[];
}

export interface SkyPerson extends SkyPersonInput {
  pos: Vec3;
  /** 0..1 — drives brightness, size and the thread from you. */
  close: number;
  /** Index into `clusters` of the nebula this friend sits in, or -1. */
  cluster: number;
}

export interface SkyCluster {
  id: string;
  name: string;
  hue: number;
  centre: Vec3;
  radius: number;
  /** Indexes into `people`. */
  members: number[];
}

export interface Glimmer {
  /** Index into `people` of the friend this glimmer is reached through. */
  via: number;
  pos: Vec3;
  /** 0..1 magnitude. */
  m: number;
}

export interface PeopleSkyModel {
  people: SkyPerson[];
  clusters: SkyCluster[];
  glimmers: Glimmer[];
  /** Radius that holds every friend — the home camera frames this. */
  extent: number;
}

const CLOSE: Record<Bond, number> = { strong: 0.72, steady: 0.5, light: 0.3 };
/** [min, max] shell radius per closeness bucket, before crowd scaling. */
const SHELL: Record<"sky" | Bond, [number, number]> = {
  sky: [2.8, 4.0],
  strong: [4.6, 6.0],
  steady: [6.0, 8.0],
  light: [8.0, 10.6],
};
/** How many glimmers a reach bucket draws (a hint of scale, not a census). */
const GLIMMERS: Record<Reach, number> = { "3+": 3, "5+": 5, "10+": 8, "25+": 12, "50+": 16, "100+": 22 };
export const GLIMMER_BUDGET = 1600;
const MIN_GAP = 0.95;
const FLAT = 0.62;
const GOLDEN = Math.PI * (3 - Math.sqrt(5));

/** FNV-1a — a stable 31-bit seed from an id. */
export function hashId(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return ((h >>> 0) % 2147483646) + 1;
}

export function isReach(v: unknown): v is Reach {
  return typeof v === "string" && (REACH_BUCKETS as readonly string[]).includes(v);
}

export function glimmerCount(reach: string | null | undefined): number {
  return isReach(reach) ? GLIMMERS[reach] : 0;
}

export function closeness(p: Pick<SkyPersonInput, "inSky" | "bond">): number {
  return p.inSky ? 1 : CLOSE[p.bond] ?? CLOSE.light;
}

/** Outer shells spread out as the crowd grows so density stays calm. */
export function crowdScale(n: number): number {
  return Math.min(2.4, Math.max(1, Math.sqrt(n / 30)));
}

function len(v: Vec3): number {
  return Math.hypot(v[0], v[1], v[2]);
}

function fib(i: number, n: number, turn: number): Vec3 {
  const y = n <= 1 ? 0 : 1 - (2 * (i + 0.5)) / n;
  const r = Math.sqrt(Math.max(0, 1 - y * y));
  const t = i * GOLDEN + turn;
  return [Math.cos(t) * r, y, Math.sin(t) * r];
}

function inBall(r: () => number): Vec3 {
  for (;;) {
    const v: Vec3 = [r() * 2 - 1, r() * 2 - 1, r() * 2 - 1];
    if (len(v) <= 1) return v;
  }
}

function byStable(a: { id: string }, b: { id: string }): number {
  return hashId(a.id) - hashId(b.id) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

export function buildPeopleSky(peopleIn: SkyPersonInput[], clustersIn: SkyClusterInput[]): PeopleSkyModel {
  const n = peopleIn.length;
  const s = crowdScale(n);

  // Clusters: biggest first (a friend in two clusters sits in the bigger nebula).
  const known = new Set(peopleIn.map((p) => p.id));
  const clusterOrder = clustersIn
    .map((c) => ({ ...c, memberIds: c.memberIds.filter((id) => known.has(id)) }))
    .filter((c) => c.memberIds.length > 0)
    .sort((a, b) => b.memberIds.length - a.memberIds.length || byStable(a, b));
  const primary = new Map<string, number>();
  clusterOrder.forEach((c, ci) => c.memberIds.forEach((id) => primary.has(id) || primary.set(id, ci)));

  // Friends: an even golden spiral over everyone, in stable hash order, so the
  // directions never bunch; each friend's shell radius comes from closeness.
  const ordered = [...peopleIn].sort(byStable);
  const people: SkyPerson[] = ordered.map((p, i) => {
    const r = seeded(hashId(p.id));
    const bucket = p.inSky ? "sky" : p.bond in SHELL ? p.bond : "light";
    const [lo, hi] = SHELL[bucket];
    const scale = p.inSky ? 1 : s;
    const radius = (lo + r() * (hi - lo)) * scale;
    const d = fib(i, n, 0.7);
    const jitter = 0.18;
    const dir: Vec3 = [d[0] + (r() - 0.5) * jitter, d[1] + (r() - 0.5) * jitter, d[2] + (r() - 0.5) * jitter];
    const l = len(dir) || 1;
    return {
      ...p,
      pos: [(dir[0] / l) * radius, (dir[1] / l) * radius * FLAT, (dir[2] / l) * radius],
      close: closeness(p),
      cluster: primary.get(p.id) ?? -1,
    };
  });
  const index = new Map(people.map((p, i) => [p.id, i]));

  // Nebulae: each cluster gets its own direction; its members gather there.
  const clusters: SkyCluster[] = clusterOrder.map((c, ci) => {
    const members = people.map((p, i) => (p.cluster === ci ? i : -1)).filter((i) => i >= 0);
    const d = fib(ci, Math.max(clusterOrder.length, 2), 2.1);
    const flat = Math.hypot(d[0], d[2]) > 0.2 ? d : ([d[0] + 0.6, d[1] * 0.4, d[2] + 0.4] as Vec3);
    const l = len(flat) || 1;
    const reach = (5.4 + 0.35 * Math.sqrt(members.length)) * Math.sqrt(s);
    const radius = 1.3 + 0.42 * Math.sqrt(members.length);
    const centre: Vec3 = [(flat[0] / l) * reach, (flat[1] / l) * reach * FLAT * 0.7, (flat[2] / l) * reach];
    for (const i of members) {
      const p = people[i];
      const r = seeded(hashId(`${p.id}:${c.id}`));
      const off = inBall(r);
      // Mostly in the nebula, a little toward where closeness put them.
      const k = 0.22;
      p.pos = [
        centre[0] + off[0] * radius * (1 - k) + p.pos[0] * k * 0.35,
        centre[1] + off[1] * radius * FLAT * (1 - k) + p.pos[1] * k * 0.35,
        centre[2] + off[2] * radius * (1 - k) + p.pos[2] * k * 0.35,
      ];
    }
    return { id: c.id, name: c.name, hue: c.hue, centre, radius, members };
  });

  relax(people);

  // Recentre each nebula on where its members ended up.
  for (const c of clusters) {
    const m = c.members.map((i) => people[i].pos);
    c.centre = [0, 1, 2].map((k) => m.reduce((a, p) => a + p[k], 0) / m.length) as Vec3;
    c.radius = Math.max(1.2, ...m.map((p) => len([p[0] - c.centre[0], p[1] - c.centre[1], p[2] - c.centre[2]]))) + 0.6;
  }

  // Friends of friends: a few glimmers beyond each friend, within a budget.
  const want = people.map((p) => glimmerCount(p.reach));
  const total = want.reduce((a, b) => a + b, 0);
  const k = total > GLIMMER_BUDGET ? GLIMMER_BUDGET / total : 1;
  const glimmers: Glimmer[] = [];
  people.forEach((p, i) => {
    const count = want[i] ? Math.max(1, Math.round(want[i] * k)) : 0;
    if (!count) return;
    const r = seeded(hashId(`${p.id}:fof`));
    const l = len(p.pos) || 1;
    const out: Vec3 = [p.pos[0] / l, p.pos[1] / l, p.pos[2] / l];
    for (let j = 0; j < count; j++) {
      const d = 1.2 + r() * 2.6;
      glimmers.push({
        via: i,
        pos: [p.pos[0] + out[0] * d + (r() - 0.5) * 2, p.pos[1] + out[1] * d + (r() - 0.5) * 1.4, p.pos[2] + out[2] * d + (r() - 0.5) * 2],
        m: r(),
      });
    }
  });

  const extent = Math.max(4, ...people.map((p) => len(p.pos)));
  return { people, clusters, glimmers, extent };
}

/** Push apart any two friends closer than MIN_GAP, and everyone off "you". */
function relax(people: SkyPerson[]): void {
  const n = people.length;
  for (let it = 0; it < 14; it++) {
    let moved = false;
    for (let a = 0; a < n; a++) {
      const A = people[a].pos;
      const la = len(A);
      if (la < 2.2) {
        const k = (2.2 / (la || 1e-6));
        people[a].pos = la ? [A[0] * k, A[1] * k, A[2] * k] : [2.2, 0, 0];
        moved = true;
      }
      for (let b = a + 1; b < n; b++) {
        const P = people[a].pos, Q = people[b].pos;
        const dx = Q[0] - P[0], dy = Q[1] - P[1], dz = Q[2] - P[2];
        const d = Math.hypot(dx, dy, dz);
        if (d >= MIN_GAP) continue;
        moved = true;
        const push = (MIN_GAP - d) / 2;
        // Coincident points split along a stable axis.
        const [ux, uy, uz] = d > 1e-6 ? [dx / d, dy / d, dz / d] : [1, 0, 0];
        people[a].pos = [P[0] - ux * push, P[1] - uy * push, P[2] - uz * push];
        people[b].pos = [Q[0] + ux * push, Q[1] + uy * push, Q[2] + uz * push];
      }
    }
    if (!moved) break;
  }
}

// ── Camera ────────────────────────────────────────────────────────────────

export interface Pose {
  yaw: number;
  pitch: number;
  dist: number;
  target: Vec3;
}

export const HOME_PITCH = 0.24;
export const MIN_DIST = 4;

export function homePose(model: PeopleSkyModel, yaw = 0.4): Pose {
  return { yaw, pitch: HOME_PITCH, dist: model.extent * 1.3 + 3.2, target: [0, 0, 0] };
}

export function maxDist(model: PeopleSkyModel): number {
  return model.extent * 2.6 + 8;
}

/** Yaw that shows `v` side-on, to the left of you (so a card on the right never covers either). */
export function sideYaw(v: Vec3, fallback: number): number {
  if (Math.hypot(v[0], v[2]) < 1e-6) return fallback;
  return Math.atan2(-v[2], v[0]) + Math.PI;
}

/** Fly toward a friend: them on the left, you on the right, both in frame. */
export function personPose(p: SkyPerson, yaw: number, pitch: number): Pose {
  const l = Math.hypot(p.pos[0], p.pos[1], p.pos[2]);
  return { yaw: sideYaw(p.pos, yaw), pitch, dist: Math.max(7, l * 1.5 + 2.5), target: [p.pos[0] * 0.55, p.pos[1] * 0.55, p.pos[2] * 0.55] };
}

/** Fly into a cluster's nebula, keeping you at its edge of the frame. */
export function clusterPose(c: SkyCluster, yaw: number, pitch: number): Pose {
  const l = Math.hypot(c.centre[0], c.centre[1], c.centre[2]);
  return { yaw: sideYaw(c.centre, yaw), pitch, dist: Math.max(c.radius * 3.2 + 4, l * 1.35 + 3), target: [c.centre[0] * 0.7, c.centre[1] * 0.7, c.centre[2] * 0.7] };
}

/** Shortest-way yaw so a flight never spins the long way round. */
export function nearAngle(from: number, to: number): number {
  const tau = Math.PI * 2;
  let d = ((to - from) % tau + tau) % tau;
  if (d > Math.PI) d -= tau;
  return from + d;
}

export function lerpPose(a: Pose, b: Pose, t: number): Pose {
  const m = (x: number, y: number) => x + (y - x) * t;
  return {
    yaw: m(a.yaw, b.yaw),
    pitch: m(a.pitch, b.pitch),
    dist: m(a.dist, b.dist),
    target: [m(a.target[0], b.target[0]), m(a.target[1], b.target[1]), m(a.target[2], b.target[2])],
  };
}

/** Calm flights: longer for longer trips, never rushed, never tedious. */
export function flightMs(a: Pose, b: Pose): number {
  const travel = Math.hypot(b.target[0] - a.target[0], b.target[1] - a.target[1], b.target[2] - a.target[2]) + Math.abs(b.dist - a.dist) * 0.6;
  return Math.round(Math.min(2200, Math.max(900, 700 + travel * 90)));
}

// ── Labels ────────────────────────────────────────────────────────────────

export interface LabelCandidate {
  key: string;
  /** Centre of the label box, in CSS px. */
  x: number;
  y: number;
  w: number;
  h: number;
  /** Higher places first. */
  priority: number;
  /** Always placed (the selected or hovered star) and never counted out. */
  force?: boolean;
  /** Alternatives for one label: the first of a group that fits wins. */
  group?: string;
}

/**
 * Greedy label placement: forced labels first, then by priority; a label that
 * would overlap one already placed (or run off the canvas) is dropped. At most
 * `cap` labels in total.
 */
export function pickLabels(cands: LabelCandidate[], cap: number, width: number, height: number, pad = 6): string[] {
  const sorted = [...cands].sort((a, b) => Number(!!b.force) - Number(!!a.force) || b.priority - a.priority || (a.key < b.key ? -1 : 1));
  const placed: { x0: number; x1: number; y0: number; y1: number }[] = [];
  const out: string[] = [];
  const done = new Set<string>();
  for (const c of sorted) {
    if (out.length >= cap && !c.force) break;
    if (c.group && done.has(c.group)) continue;
    const box = { x0: c.x - c.w / 2 - pad, x1: c.x + c.w / 2 + pad, y0: c.y - c.h / 2 - pad / 2, y1: c.y + c.h / 2 + pad / 2 };
    if (!c.force && (box.x0 < 0 || box.x1 > width || box.y0 < 0 || box.y1 > height)) continue;
    if (!c.force && placed.some((p) => p.x0 < box.x1 && box.x0 < p.x1 && p.y0 < box.y1 && box.y0 < p.y1)) continue;
    placed.push(box);
    out.push(c.key);
    if (c.group) done.add(c.group);
  }
  return out;
}

// ── Words ─────────────────────────────────────────────────────────────────

export function firstName(name: string): string {
  return name.trim().split(/\s+/)[0] || name;
}

/** "10+ people you don't know are one step away through Kiho." — or nothing. */
export function reachLine(reach: string | null | undefined, name: string): string {
  if (!isReach(reach)) return "";
  return `${reach} people you don’t know are one step away through ${firstName(name)}.`;
}

/** The header line for the whole sky, from the top-level `reach_total`. */
export function reachTotalLine(reach: string | null | undefined): string {
  if (!isReach(reach)) return "";
  return `Through them, ${reach} more people are one step away.`;
}

export function bondLine(p: Pick<SkyPersonInput, "inSky" | "bond">): string {
  if (p.inSky || p.bond === "strong") return "You two share a lot.";
  return p.bond === "steady" ? "You share a fair bit." : "You share a little.";
}

/** Case- and accent-insensitive match; names first, then handles. */
export function normalise(s: string): string {
  return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
}

export function findPeople<T extends { name: string; handle?: string }>(people: T[], query: string): T[] {
  const q = normalise(query);
  if (!q) return [];
  const starts: T[] = [], words: T[] = [], rest: T[] = [];
  for (const p of people) {
    const n = normalise(p.name);
    if (n.startsWith(q)) starts.push(p);
    else if (n.split(/\s+/).some((w) => w.startsWith(q))) words.push(p);
    else if (n.includes(q) || (p.handle && normalise(p.handle).includes(q))) rest.push(p);
  }
  return [...starts, ...words, ...rest];
}
