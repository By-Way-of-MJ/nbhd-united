/**
 * Constellation night sky — the pure model behind /constellation (Open Sky).
 *
 * Every star is one real lesson and every constellation one real cluster.
 * Constellations are ranked by age (newest first) and laid out along a calm
 * coil over the dome: each one a little deeper than the last and a small turn
 * to the right, so Next is always a real journey and nothing piles up — with
 * six constellations or with hundreds. Pure maths only — no DOM — so it can be
 * unit-tested with Node's built-in runner after a tsc transpile.
 */

import { hsh } from "../sky-art/noise";

export type Vec3 = [number, number, number];

export interface SkyInputNode {
  id: number;
  text: string;
  context?: string;
  tags?: string[];
  cluster_id: number | null;
  cluster_label?: string;
  source_type?: string;
  source_ref?: string;
  created_at: string;
}

export interface SkyInput {
  nodes: SkyInputNode[];
  clusters: { id: number; label: string }[];
}

export interface SkyLesson {
  id: number;
  text: string;
  context: string;
  sourceType: string;
  sourceRef: string;
  createdAt: string;
  /** World position. */
  pos: Vec3;
  /** Relative brightness, ~1–2.2. */
  size: number;
}

export interface SkyCluster {
  /** Cluster id, or LOOSE_KEY for lessons that belong to no cluster yet. */
  key: number;
  name: string;
  /** Oldest → newest (the order "‹ lesson ›" steps through). */
  lessons: SkyLesson[];
  az: number;
  alt: number;
  /** World distance from the origin (the "here and now"). */
  dist: number;
  ageDays: number;
  lightYears: number;
  when: string;
  pos: Vec3;
  /** Star colour as "r,g,b". */
  tint: string;
  /** How far the camera stands back to frame it. */
  view: number;
  /** Constellation figure: pairs of indices into `lessons` (a minimum spanning tree). */
  links: [number, number][];
  /** Indices of the brightest stars (at most FEATURED_STARS), drawn fully before you arrive. */
  featured: number[];
  /** Figure over just the featured stars (equals `links` for small constellations). */
  featuredLinks: [number, number][];
}

export interface SkyModel {
  /** In navigation order (Previous / Next walk this list). */
  clusters: SkyCluster[];
  lessonCount: number;
}

export interface Pose {
  p: Vec3;
  az: number;
  alt: number;
}

export const LOOSE_KEY = -1;
export const LOOSE_NAME = "Loose stars";
export const D2R = Math.PI / 180;
const DAY = 864e5;
const GOLDEN = Math.PI * (3 - Math.sqrt(5));
/** The coil of constellations spans the dome from just above the hills to high overhead. */
export const ALT_MIN = 10;
export const ALT_MAX = 62;
/** Where the coil starts: the newest constellation, facing the Milky Way. */
const COIL_START_AZ = 60;

/** Real star colours (blue-white → white → warm), never the UI accent. */
export const STAR_TINTS = ["200,222,255", "236,242,255", "255,236,214", "214,228,255", "255,226,196", "226,234,255", "255,244,230"];

// ── Small maths ─────────────────────────────────────────────────────────────

export function dirv(az: number, alt: number): Vec3 {
  const a = az * D2R, e = alt * D2R;
  return [Math.cos(e) * Math.sin(a), Math.sin(e), Math.cos(e) * Math.cos(a)];
}

/** Signed shortest turn from a to b in degrees, in (-180, 180]. Positive = turn right. */
export function angDiff(a: number, b: number): number {
  return ((((b - a) % 360) + 540) % 360) - 180;
}

export function norm360(a: number): number {
  return ((a % 360) + 360) % 360;
}

function dist3(a: Vec3, b: Vec3): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

function clampN(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

// ── Age → depth ─────────────────────────────────────────────────────────────

/** Days between a date and now (never negative). */
export function ageInDays(iso: string, now: Date): number {
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return 0;
  return Math.max(0, (now.getTime() - t) / DAY);
}

/**
 * World distance for an age: this week ≈ 12, a month ≈ 30, a year ≈ 77.
 * Square-root so recent months get room to breathe and last year still fits.
 */
export function ageToDistance(ageDays: number): number {
  return 12 + 9 * Math.sqrt(Math.max(0, ageDays) / 7);
}

/** The friendly depth scale: one light-year per week of age (at least 1). */
export function lightYearsFor(ageDays: number): number {
  return Math.max(1, Math.round(Math.max(0, ageDays) / 7));
}

export function lightYearsLabel(ly: number): string {
  return `${ly} ${ly === 1 ? "light-year" : "light-years"} out`;
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

/** "this week" · "last week" · "earlier this month" · "July" · "December 2025". */
export function whenLabel(iso: string, now: Date): string {
  const age = ageInDays(iso, now);
  if (age < 7) return "this week";
  if (age < 14) return "last week";
  const d = new Date(iso);
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) return "earlier this month";
  if (d.getFullYear() === now.getFullYear()) return MONTHS[d.getMonth()];
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

// ── Depth and direction by rank ────────────────────────────────────────────

/** Nearest constellation's distance, and the depth step between neighbours. */
export const NEAR_DEPTH = 12;
export const MIN_DEPTH_GAP = 6;
export const MAX_DEPTH_GAP = 16;
/** Few constellations spread over about this much depth (then the gap floor takes over). */
const DEPTH_SPAN = 110;

/**
 * Depths for constellations already ranked newest → oldest. Evenly spaced by
 * rank — a step of 6–16 units, so every Next is a real journey — but never
 * nearer than the age alone would put them (a year-old constellation in a
 * sparse sky still sits far away), nor so near that a big one fills the sky.
 */
export function rankDepths(agesNewestFirst: number[], radii: number[] = []): number[] {
  const n = agesNewestFirst.length;
  const gap = clampN(DEPTH_SPAN / Math.max(1, n - 1), MIN_DEPTH_GAP, MAX_DEPTH_GAP);
  const out: number[] = [];
  agesNewestFirst.forEach((age, i) => {
    const byRank = i === 0 ? NEAR_DEPTH : out[i - 1] + gap;
    out.push(Math.max(byRank, i === 0 ? NEAR_DEPTH : ageToDistance(age), 1.6 * (radii[i] ?? 0)));
  });
  return out;
}

/** Coil through n points: turn right by at least `sep` (or the pair's own minimum), climbing one `sep` per full lap. */
function coil(n: number, sep: number, minSteps: number[]): { az: number; alt: number }[] {
  const pts: { az: number; alt: number }[] = [];
  let az = COIL_START_AZ, alt = ALT_MIN + sep * 0.35;
  for (let i = 0; i < n; i++) {
    pts.push({ az: norm360(az), alt });
    const daz = Math.max(sep, minSteps[i] ?? 0) / Math.cos(alt * D2R);
    az += daz;
    alt += (sep * daz) / 360;
  }
  return pts;
}

/**
 * Angular spacing between neighbouring constellations for n of them: the
 * widest (up to 28°) that still fits the whole coil inside the dome band.
 */
export function coilSeparation(n: number, minSteps: number[] = []): number {
  let lo = 1, hi = 28;
  const fits = (s: number) => {
    // Every point, not just the last: past the zenith a coil would fold back on itself.
    return coil(n, s, minSteps).every((p) => p.alt <= ALT_MAX - s * 0.35);
  };
  if (fits(hi)) return hi;
  for (let it = 0; it < 30; it++) {
    const mid = (lo + hi) / 2;
    if (fits(mid)) lo = mid;
    else hi = mid;
  }
  return lo;
}

/** Smallest turn (degrees) between two neighbours so their figures don't overlap from where you stand. */
export function pairStep(r1: number, r2: number, d1: number, d2: number): number {
  return Math.min(65, ((r1 + r2) * 1.05) / ((d1 + d2) / 2) / D2R);
}

/**
 * Directions for constellations in rank order (newest first): a coil that
 * turns right and climbs, so each Next is a small, predictable turn, and
 * constellations a lap apart are one spacing apart in altitude (and far apart
 * in depth). A little key-seeded wobble keeps it from looking machine-made.
 */
export function assignDirections(keysInRank: number[], minSteps: number[] = []): { az: number; alt: number }[] {
  const sep = coilSeparation(keysInRank.length, minSteps);
  return coil(keysInRank.length, sep, minSteps).map((p, i) => {
    const k = keysInRank[i];
    const alt = clampN(p.alt + (hsh(k, 2, 77) - 0.5) * 0.3 * sep, ALT_MIN, ALT_MAX);
    const az = norm360(p.az + ((hsh(k, 1, 77) - 0.5) * 0.3 * sep) / Math.cos(alt * D2R));
    return { az, alt };
  });
}

/** Angular distance between two dome directions, in degrees. */
export function angularGap(a: { az: number; alt: number }, b: { az: number; alt: number }): number {
  const da = dirv(a.az, a.alt), db = dirv(b.az, b.alt);
  const dot = clampN(da[0] * db[0] + da[1] * db[1] + da[2] * db[2], -1, 1);
  return Math.acos(dot) / D2R;
}

// ── Stars inside a constellation ────────────────────────────────────────────

/** Spread radius of a constellation with n stars (world units). */
export function clusterRadius(n: number): number {
  return 3.2 + 1.3 * Math.sqrt(Math.max(1, n));
}

/** Local (side, up, depth) offsets for n stars, deterministic per lesson id. */
export function localOffsets(ids: number[]): Vec3[] {
  const n = ids.length;
  const R = clusterRadius(n);
  return ids.map((id, i) => {
    if (n === 1) return [0, 0, 0];
    const r = R * Math.sqrt((i + 0.5) / n) * (0.78 + 0.44 * hsh(id, 3, 11));
    const a = i * GOLDEN + (hsh(id, 4, 11) - 0.5) * 0.9;
    return [Math.cos(a) * r * 1.2, Math.sin(a) * r * 0.78, (hsh(id, 5, 11) - 0.5) * 3.2];
  });
}

/** Minimum spanning tree over the offsets (Prim from star 0, ties → lower index). */
export function figureLinks(offsets: Vec3[]): [number, number][] {
  const n = offsets.length;
  if (n < 2) return [];
  const inTree = new Array<boolean>(n).fill(false);
  const best = new Array<number>(n).fill(Infinity);
  const from = new Array<number>(n).fill(-1);
  const links: [number, number][] = [];
  best[0] = 0;
  for (let step = 0; step < n; step++) {
    let u = -1;
    for (let i = 0; i < n; i++) if (!inTree[i] && (u < 0 || best[i] < best[u])) u = i;
    inTree[u] = true;
    if (from[u] >= 0) links.push([from[u], u]);
    for (let v = 0; v < n; v++) {
      if (inTree[v]) continue;
      const d = Math.hypot(offsets[u][0] - offsets[v][0], offsets[u][1] - offsets[v][1]);
      if (d < best[v]) {
        best[v] = d;
        from[v] = u;
      }
    }
  }
  return links;
}

/** Big constellations show their brightest few until you arrive. */
export const FEATURED_STARS = 12;

/** Indices of the `cap` brightest stars (ties → earlier index), in index order. */
export function brightestStars(sizes: number[], cap = FEATURED_STARS): number[] {
  return sizes
    .map((s, i) => [s, i] as const)
    .sort((a, b) => b[0] - a[0] || a[1] - b[1])
    .slice(0, cap)
    .map(([, i]) => i)
    .sort((a, b) => a - b);
}

// ── The model ───────────────────────────────────────────────────────────────

export function buildSky(input: SkyInput, now: Date): SkyModel {
  const names = new Map(input.clusters.map((c) => [c.id, c.label]));
  const groups = new Map<number, SkyInputNode[]>();
  for (const n of input.nodes) {
    const key = n.cluster_id == null ? LOOSE_KEY : n.cluster_id;
    const arr = groups.get(key) ?? [];
    arr.push(n);
    groups.set(key, arr);
  }
  // Rank newest → oldest by each constellation's newest lesson; that order is
  // both depth (older = farther) and the Next order.
  const ranked = [...groups].map(([key, members]) => {
    const sorted = [...members].sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime() || a.id - b.id);
    const newest = sorted[sorted.length - 1];
    return { key, members, sorted, newest, ageDays: ageInDays(newest.created_at, now) };
  });
  ranked.sort((a, b) => a.ageDays - b.ageDays || a.key - b.key);
  const radii = ranked.map((r) => clusterRadius(r.members.length));
  const depths = rankDepths(ranked.map((r) => r.ageDays), radii);
  const steps = ranked.map((_, i) => (i + 1 < ranked.length ? pairStep(radii[i], radii[i + 1], depths[i], depths[i + 1]) : 0));
  const dirs = assignDirections(ranked.map((r) => r.key), steps);
  const clusters: SkyCluster[] = ranked.map(({ key, members, sorted, newest, ageDays }, rank) => {
    const dist = depths[rank];
    const { az, alt } = dirs[rank];
    const d = dirv(az, alt);
    const side = dirv(az + 90, 0);
    const up: Vec3 = [-Math.sin(alt * D2R) * Math.sin(az * D2R), Math.cos(alt * D2R), -Math.sin(alt * D2R) * Math.cos(az * D2R)];
    const pos: Vec3 = [d[0] * dist, d[1] * dist, d[2] * dist];
    // Offsets are keyed by id order so a new lesson doesn't reshuffle old ones.
    const byId = [...sorted].sort((a, b) => a.id - b.id);
    const offs = localOffsets(byId.map((n) => n.id));
    const offById = new Map(byId.map((n, i) => [n.id, offs[i]]));
    const lessons: SkyLesson[] = sorted.map((n) => {
      const [u, w, z] = offById.get(n.id)!;
      return {
        id: n.id,
        text: n.text,
        context: n.context ?? "",
        sourceType: n.source_type ?? "",
        sourceRef: n.source_ref ?? "",
        createdAt: n.created_at,
        pos: [pos[0] + side[0] * u + up[0] * w + d[0] * z, pos[1] + side[1] * u + up[1] * w + d[1] * z, pos[2] + side[2] * u + up[2] * w + d[2] * z],
        size: 1 + hsh(n.id, 6, 11) * 1.2,
      };
    });
    const lessonOffs = lessons.map((l) => offById.get(l.id)!);
    const links = figureLinks(lessonOffs);
    const featured = brightestStars(lessons.map((l) => l.size));
    const featuredLinks =
      featured.length === lessons.length ? links : figureLinks(featured.map((i) => lessonOffs[i])).map(([a, b]) => [featured[a], featured[b]] as [number, number]);
    return {
      key,
      name: key === LOOSE_KEY ? LOOSE_NAME : names.get(key) || members.find((m) => m.cluster_label)?.cluster_label || "Unnamed",
      lessons,
      az,
      alt,
      dist,
      ageDays,
      lightYears: lightYearsFor(ageDays),
      when: whenLabel(newest.created_at, now),
      pos,
      tint: STAR_TINTS[Math.floor(hsh(key, 7, 11) * STAR_TINTS.length)],
      view: Math.max(12, clusterRadius(lessons.length) * 1.9),
      links,
      featured,
      featuredLinks,
    };
  });
  return { clusters, lessonCount: input.nodes.length };
}

// ── Level of detail ─────────────────────────────────────────────────────────

/** The chosen constellation and this many either side in the Next order are drawn fully. */
export const FULL_NEIGHBORS = 2;
/** Hard cap on constellation names on screen. */
export const MAX_LABELS = 5;

/** 1 = draw fully (stars, figure, name); 0 = a distant glow with its few brightest stars. */
export function detailTarget(index: number, selected: number): number {
  return selected >= 0 && Math.abs(index - selected) <= FULL_NEIGHBORS ? 1 : 0;
}

/** Depth haze by distance from the camera: clear up close, a faint glimmer far away. */
export function fogFor(camDist: number): number {
  return 0.12 + 0.88 * Math.exp(-Math.max(0, camDist - 24) / 60);
}

/** For a distant constellation, how many of its brightest stars still show. */
export const FAR_STARS = 3;

export interface LabelCandidate {
  index: number;
  camDist: number;
  /** Holds a search match (search results earn their names first). */
  match?: boolean;
}

/**
 * Which constellations get a name this frame: the chosen one, then search
 * matches, then the nearest to the camera — never more than `cap`.
 */
export function pickLabels(cands: LabelCandidate[], selected: number, cap = MAX_LABELS): number[] {
  return [...cands]
    .sort((a, b) => Number(b.index === selected) - Number(a.index === selected) || Number(!!b.match) - Number(!!a.match) || a.camDist - b.camDist || a.index - b.index)
    .slice(0, cap)
    .map((c) => c.index);
}

// ── Navigation ──────────────────────────────────────────────────────────────

/** Degrees the camera looks below a constellation, lifting it clear of the lesson text. */
export const LOOK_BELOW = 9;

/** Where the sky opens: the nearest (most recent) constellation. */
export function startIndex(clusters: Pick<SkyCluster, "dist">[]): number {
  let best = 0;
  clusters.forEach((c, i) => {
    if (c.dist < clusters[best].dist) best = i;
  });
  return best;
}

/** The camera pose that frames a constellation: on its ray, stood back, looking slightly down at it. */
export function poseFor(c: Pick<SkyCluster, "az" | "alt" | "dist" | "view">): Pose {
  const d = dirv(c.az, c.alt);
  const back = c.dist - c.view;
  return { p: [d[0] * back, d[1] * back, d[2] * back], az: c.az, alt: c.alt - LOOK_BELOW };
}

/** "deeper" · "closer" · "turn left" · "turn right" · "turn right, deeper" … */
export function navHint(from: Pick<SkyCluster, "az" | "dist">, to: Pick<SkyCluster, "az" | "dist">): string {
  const parts: string[] = [];
  const turn = angDiff(from.az, to.az);
  if (Math.abs(turn) >= 15) parts.push(turn > 0 ? "turn right" : "turn left");
  const dd = to.dist - from.dist;
  if (Math.abs(dd) >= 2) parts.push(dd > 0 ? "deeper" : "closer");
  return parts.length ? parts.join(", ") : "right beside";
}

export function stepIndex(i: number, delta: number, n: number): number {
  return n ? (((i + delta) % n) + n) % n : 0;
}

// ── Calm camera moves ───────────────────────────────────────────────────────

/** Cubic ease-in-out (smoothstep): zero speed at both ends, no overshoot, peak slope 1.5. */
export function easeInOut(t: number): number {
  const x = clampN(t, 0, 1);
  return x * x * (3 - 2 * x);
}

/** Derivative of easeInOut, normalised so the peak is 1. */
export function easeSpeed(t: number): number {
  const x = clampN(t, 0, 1);
  return (6 * x * (1 - x)) / 1.5;
}

export const MAX_TURN_DEG_PER_S = 25;
export const MIN_MOVE_S = 2.5;
export const BASE_MAX_MOVE_S = 4;
export const HARD_MAX_MOVE_S = 7;

export function turnAngle(a: Pose, b: Pose): number {
  return Math.hypot(angDiff(a.az, b.az) * Math.cos(((a.alt + b.alt) / 2) * D2R), b.alt - a.alt);
}

/**
 * Move duration in seconds: 2.5–4 s scaled by travel distance, stretched so the
 * peak turn rate stays under ~25°/s (up to a hard ceiling for half-sky turns).
 */
export function moveDuration(a: Pose, b: Pose): number {
  const travel = dist3(a.p, b.p);
  const base = MIN_MOVE_S + (BASE_MAX_MOVE_S - MIN_MOVE_S) * Math.min(1, travel / 60);
  const forTurn = (1.5 * turnAngle(a, b)) / MAX_TURN_DEG_PER_S;
  return Math.min(HARD_MAX_MOVE_S, Math.max(base, forTurn));
}

/** Pose at eased progress e ∈ [0,1]: travel and turn share one curve so they glide together. */
export function lerpPose(a: Pose, b: Pose, e: number): Pose {
  return {
    p: [a.p[0] + (b.p[0] - a.p[0]) * e, a.p[1] + (b.p[1] - a.p[1]) * e, a.p[2] + (b.p[2] - a.p[2]) * e],
    az: norm360(a.az + angDiff(a.az, b.az) * e),
    alt: a.alt + (b.alt - a.alt) * e,
  };
}

// ── Search by meaning ───────────────────────────────────────────────────────

/** text-embedding-3-small: short queries against on-topic lessons land ~0.3–0.5; noise sits ≤ ~0.25. */
export const SEARCH_MIN_SIMILARITY = 0.28;
/** Also drop anything far below the best hit, so a strong match isn't diluted by weak ones. */
export const SEARCH_WINDOW = 0.12;
export const SEARCH_MAX_SCORED = 20;
export const SEARCH_MAX_UNSCORED = 5;

export interface SearchHit {
  id: number;
  similarity?: number | null;
}

/** Honest matches: known lessons only, scored ones above the floor and near the best, else the top 5. */
export function filterSearchHits(hits: SearchHit[], known: Set<number>): number[] {
  const inSky = hits.filter((h) => known.has(h.id));
  const scored = inSky.every((h) => typeof h.similarity === "number" && Number.isFinite(h.similarity));
  if (!scored || !inSky.length) return inSky.slice(0, SEARCH_MAX_UNSCORED).map((h) => h.id);
  const ranked = [...inSky].sort((a, b) => (b.similarity as number) - (a.similarity as number));
  const top = ranked[0].similarity as number;
  return ranked
    .filter((h) => (h.similarity as number) >= SEARCH_MIN_SIMILARITY && (h.similarity as number) >= top - SEARCH_WINDOW)
    .slice(0, SEARCH_MAX_SCORED)
    .map((h) => h.id);
}

/**
 * Order search matches for stepping: the best match first, then the others
 * along the path — its own constellation, then deeper ones one by one, then
 * back through the nearer ones — so "next match" never zig-zags across the sky.
 */
export function orderMatches(ids: number[], where: Map<number, { rank: number; lesson: number }>): number[] {
  const known = ids.filter((id) => where.has(id));
  if (known.length < 2) return known;
  const best = where.get(known[0])!;
  const pos = (id: number) => where.get(id)!;
  const rest = known.slice(1);
  const ahead = rest.filter((id) => pos(id).rank >= best.rank).sort((a, b) => pos(a).rank - pos(b).rank || pos(a).lesson - pos(b).lesson);
  const behind = rest.filter((id) => pos(id).rank < best.rank).sort((a, b) => pos(b).rank - pos(a).rank || pos(a).lesson - pos(b).lesson);
  return [known[0], ...ahead, ...behind];
}

export function matchLine(lessons: number, constellations: number, index: number): string {
  if (!lessons) return "Nothing close to that yet";
  const head = `${lessons} ${lessons === 1 ? "lesson" : "lessons"} in ${constellations} ${constellations === 1 ? "constellation" : "constellations"}`;
  return lessons > 1 ? `${head} · ${index + 1} of ${lessons}` : head;
}

// ── Copy ────────────────────────────────────────────────────────────────────

const SOURCE_WORDS: Record<string, string> = {
  conversation: "From a conversation",
  journal: "From your journal",
  reflection: "From a reflection",
  article: "From your reading",
  experience: "From life",
  shared: "From a neighbor",
};

/** "From your journal · 12 Sep 2026". */
export function sourceLine(sourceType: string, createdAt: string): string {
  const d = new Date(createdAt);
  const date = Number.isFinite(d.getTime()) ? `${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)} ${d.getFullYear()}` : "";
  const src = SOURCE_WORDS[sourceType] ?? "";
  return [src, date].filter(Boolean).join(" · ");
}

export function countLine(lessons: number, constellations: number): string {
  return `${lessons} ${lessons === 1 ? "lesson" : "lessons"} · ${constellations} ${constellations === 1 ? "constellation" : "constellations"}`;
}
