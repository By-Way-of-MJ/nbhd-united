/**
 * Chart your galaxy — camera maths and input mapping. Pure, no DOM.
 *
 * The flight is a pseudo-3D corridor: the camera looks down +z, stars are
 * perspective-projected, and z loops every `depth` units so free flight never
 * runs out of galaxy while every star keeps one fixed position for the map.
 */

export interface Cam {
  x: number;
  y: number;
  z: number;
}

export interface View {
  w: number;
  h: number;
  focal: number;
}

export interface Projected {
  x: number;
  y: number;
  /** focal / dz — how much the perspective scales things at this depth. */
  f: number;
  dz: number;
}

export interface Point3 {
  x: number;
  y: number;
  z: number;
}

/** Nothing closer than this is drawn (it would be behind / on the lens). */
export const NEAR = 20;
/** Where the camera parks in front of a star for a target ring or a landing. */
export const HOVER_DISTANCE = 260;

export function focalFor(w: number, h: number): number {
  return 0.45 * Math.max(w, h);
}

/** The star's next position ahead of the camera on the looping corridor. */
export function aheadZ(z: number, camZ: number, depth: number): number {
  const d = (((z - camZ) % depth) + depth) % depth;
  return camZ + d;
}

export function project(p: Point3, cam: Cam, view: View): Projected | null {
  const dz = p.z - cam.z;
  if (dz < NEAR) return null;
  const f = view.focal / dz;
  return { x: view.w / 2 + (p.x - cam.x) * f, y: view.h / 2 + (p.y - cam.y) * f, f, dz };
}

export function projectLooped(p: Point3, cam: Cam, view: View, depth: number): Projected | null {
  return project({ x: p.x, y: p.y, z: aheadZ(p.z, cam.z, depth) }, cam, view);
}

export function onScreen(q: Projected, view: View, margin = 40): boolean {
  return q.x >= -margin && q.x <= view.w + margin && q.y >= -margin && q.y <= view.h + margin;
}

/** The closest star that is actually in front of you and on screen (ignoring
 *  anything nearer than `minDz`, which is already sliding past the lens). */
export function nearestStar(stars: Point3[], cam: Cam, view: View, depth: number, minDz = 0): number | null {
  let best: number | null = null;
  let bestDz = Infinity;
  for (let i = 0; i < stars.length; i++) {
    const q = projectLooped(stars[i], cam, view, depth);
    if (!q || q.dz < minDz || !onScreen(q, view, 0)) continue;
    if (q.dz < bestDz) {
      bestDz = q.dz;
      best = i;
    }
  }
  return best;
}

/** The star under a click, if one is within `radius` css px — nearest wins. */
export function pickStar(stars: Point3[], cam: Cam, view: View, depth: number, px: number, py: number, radius = 44): number | null {
  let best: number | null = null;
  let bestD = radius * radius;
  for (let i = 0; i < stars.length; i++) {
    const q = projectLooped(stars[i], cam, view, depth);
    if (!q) continue;
    const dx = q.x - px, dy = q.y - py, d = dx * dx + dy * dy;
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  }
  return best;
}

/**
 * Where the camera should park to look at a star. If the star is just behind
 * you, back up to it rather than flying the whole loop round.
 */
export function goalFor(star: Point3, cam: Cam, depth: number): Point3 {
  let d = (((star.z - cam.z) % depth) + depth) % depth;
  if (d > depth - 800) d -= depth;
  return { x: star.x, y: star.y, z: cam.z + d - HOVER_DISTANCE };
}

// ── autopilot ────────────────────────────────────────────────────────────
// A flight to a star is a gentle quadratic curve: it swings out sideways past
// the neighbouring stars, then eases in and slows as it arrives.

export interface AutoPath {
  from: Point3;
  ctrl: Point3;
  to: Point3;
  start: number;
  dur: number;
}

export function easeInOut(u: number): number {
  const t = u < 0 ? 0 : u > 1 ? 1 : u;
  return t * t * (3 - 2 * t);
}

/** Cruise speed the autopilot duration is scaled against (world units / s). */
const AUTO_SPEED = 700;
const MAX_BULGE = 320;

/**
 * Build the curve from the camera to a goal pose. `bulge` is the sideways swing
 * as a fraction of the distance (0 = straight in); `seed` picks the side.
 */
export function buildAutoPath(from: Point3, to: Point3, now: number, opts: { bulge: number; minDur: number; maxDur: number; seed?: number }): AutoPath {
  const dx = to.x - from.x, dy = to.y - from.y, dz = to.z - from.z;
  const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
  const seed = opts.seed ?? 0;
  let px = -dy, py = dx;
  const L = Math.sqrt(px * px + py * py);
  if (L < 1e-3) {
    const a = seed * 2.399963;
    px = Math.cos(a);
    py = Math.sin(a);
  } else {
    px /= L;
    py /= L;
  }
  const side = seed % 2 === 0 ? 1 : -1;
  const amt = Math.min(MAX_BULGE, dist * opts.bulge) * side;
  const ctrl = { x: (from.x + to.x) / 2 + px * amt, y: (from.y + to.y) / 2 + py * amt * 0.7, z: (from.z + to.z) / 2 };
  const dur = Math.max(opts.minDur, Math.min(opts.maxDur, dist / AUTO_SPEED));
  return { from: { ...from }, ctrl, to: { ...to }, start: now, dur };
}

/** Where the camera is on the path at `now`; `done` once it has arrived. */
export function autoPose(p: AutoPath, now: number): { pose: Point3; done: boolean } {
  const u = p.dur > 0 ? (now - p.start) / p.dur : 1;
  const e = easeInOut(u), m = 1 - e;
  return {
    pose: {
      x: m * m * p.from.x + 2 * m * e * p.ctrl.x + e * e * p.to.x,
      y: m * m * p.from.y + 2 * m * e * p.ctrl.y + e * e * p.to.y,
      z: p.from.z + (p.to.z - p.from.z) * e,
    },
    done: u >= 1,
  };
}

export type KeyAction = "left" | "right" | "up" | "down" | "faster" | "slower" | "stop" | "land" | "map" | "exit";

const KEY_MAP: Record<string, KeyAction> = {
  ArrowLeft: "left",
  a: "left",
  ArrowRight: "right",
  d: "right",
  ArrowUp: "up",
  ArrowDown: "down",
  w: "faster",
  s: "slower",
  " ": "stop",
  e: "land",
  m: "map",
  Escape: "exit",
};

/** Keyboard → flight action. Letters are case-insensitive; unknown keys → null. */
export function keyAction(key: string): KeyAction | null {
  if (!key) return null;
  return KEY_MAP[key] ?? KEY_MAP[key.toLowerCase()] ?? null;
}

export const STEER_ACTIONS: ReadonlySet<KeyAction> = new Set(["left", "right", "up", "down", "faster", "slower"]);

/** True when a key press belongs to a text field (never hijack typing). */
export function isTypingTarget(tagName: string, contentEditable = false): boolean {
  const t = tagName.toUpperCase();
  return contentEditable || t === "INPUT" || t === "TEXTAREA" || t === "SELECT";
}

/** Space/Enter on a focused button or link must stay a click, not a flight command. */
export function isActivationKeyOnControl(tagName: string, key: string): boolean {
  const t = tagName.toUpperCase();
  return (t === "BUTTON" || t === "A") && (key === " " || key === "Enter");
}
