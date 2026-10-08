/**
 * Chart your galaxy — the canvas painter. Port of the approved Open Sky mockup:
 * a procedural Milky Way band with dust lanes (rendered once), soft point-light
 * star sprites tinted per cluster (rendered once per cluster), drifting star
 * dust, twinkle, a label under the nearest few stars and a pulsing ring on the
 * autopilot target. Dust streaks into short lines while the autopilot is fast.
 * Everything per-frame is drawImage/fillRect/short strokes — hundreds of stars
 * stay well inside a 60fps budget.
 */
import { type Cam, type View, onScreen, project, projectLooped, type Projected } from "@/lib/galaxy-flight/camera";
import type { FlightGalaxy } from "@/lib/galaxy-flight/layout";
import { clamp255, ctx2d, fbm, makeCanvas, seeded, smooth, starSprite } from "@/lib/sky-art/noise";

export interface FrameState {
  cam: Cam;
  view: View;
  t: number;
  dt: number;
  /** Index of the autopilot target (ring) or null. */
  target: number | null;
  /** Don't twinkle, don't drift dust. */
  still: boolean;
  /** Hide labels while the landing panel is up. */
  quiet: boolean;
  /** Font family for labels (the page's body font). */
  font: string;
  /** Screen rects (canvas px) that labels must stay clear of: header, controls, map, panel. */
  keepOut: Rect[];
  /** 0..1 — how much the dust streaks (fast autopilot). Always 0 when `still`. */
  streak: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const LABEL_H = 14;
const KEEP_OUT_PAD = 8;

function hitsKeepOut(x: number, y: number, tw: number, zones: Rect[]): boolean {
  const l = x - tw / 2 - KEEP_OUT_PAD, r = x + tw / 2 + KEEP_OUT_PAD, t = y - KEEP_OUT_PAD, b = y + LABEL_H + KEEP_OUT_PAD;
  for (const z of zones) {
    if (l < z.x + z.w && r > z.x && t < z.y + z.h && b > z.y) return true;
  }
  return false;
}

const NEBULA_W = 512, NEBULA_H = 320;
const DUST_COUNT = 800;
const DUST_DEPTH = 6000;
/** World length of a dust streak at full speed. */
const STREAK_LEN = 200;
/** Stars fade out over the last stretch of the corridor, so the far end reads as
 *  haze (not a bright pile at the vanishing point) and the loop seam never shows. */
const FAR_FADE = 0.3;
/** Stars nearer than this are already sliding past — never "nearest". */
const NEAR_FLOOR = 140;
const LABEL_COUNT = 8;
const LABEL_RANGE = 1600;

function nebula(): HTMLCanvasElement {
  const W = NEBULA_W, H = NEBULA_H;
  const c = makeCanvas(W, H), g = ctx2d(c), img = g.createImageData(W, H), d = img.data;
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const u = x / W, v = y / H;
      const perp = (v - 0.55 + 0.25 * (u - 0.5)) + 0.08 * (fbm(u * 4, 0.3, 41, 3) - 0.5);
      const w = 0.16;
      const core = Math.exp(-(perp * perp) / (w * w));
      const bulge = Math.exp(-Math.pow((u - 0.58) / 0.2, 2));
      const n = fbm(u * 9, v * 6, 7, 5);
      const dust = smooth(0.45, 0.78, fbm(u * 14 + 4, v * 9, 29, 5)) * Math.exp(-Math.pow((perp - 0.01) / (w * 0.6), 2));
      const I = core * (0.25 + 0.75 * n) * (0.5 + 0.9 * bulge) * (1 - 0.6 * dust) + 0.04 * fbm(u * 5, v * 5, 3, 4);
      const r = 150 + 105 * bulge, gg = 160 + 70 * bulge, bb = 222 - 40 * bulge;
      const o = (y * W + x) * 4;
      d[o] = clamp255(r * I * 0.55 + 3);
      d[o + 1] = clamp255(gg * I * 0.55 + 4);
      d[o + 2] = clamp255(bb * I * 0.55 + 7);
      d[o + 3] = 255;
    }
  }
  g.putImageData(img, 0, 0);
  return c;
}

export class FlightRenderer {
  private neb: HTMLCanvasElement | null = null;
  private sprites: HTMLCanvasElement[] = [];
  private dust: { x: number; y: number; z: number }[] = [];
  /** Last frame's projections, for hit-testing clicks without re-projecting. */
  lastProjected: (Projected | null)[] = [];
  nearestIndex: number | null = null;

  constructor(private galaxy: FlightGalaxy) {}

  private setup() {
    this.neb = nebula();
    this.sprites = this.galaxy.clusters.map((c) => starSprite(c.rgb, 64, [[0, 1], [0.09, 0.95], [0.2, 0.22], [0.5, 0.05], [1, 0]]));
    const rnd = seeded(3);
    this.dust = Array.from({ length: DUST_COUNT }, () => ({ x: (rnd() - 0.5) * 2600, y: (rnd() - 0.5) * 1700, z: rnd() * DUST_DEPTH }));
  }

  draw(ctx: CanvasRenderingContext2D, dpr: number, s: FrameState) {
    if (!this.neb) this.setup();
    const { cam, view, t } = s;
    const { w, h } = view;
    const stars = this.galaxy.stars;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.imageSmoothingEnabled = true;

    // Milky Way, slightly larger than the view so the parallax never shows an edge.
    const ox = -cam.x * 0.02, oy = -cam.y * 0.02;
    ctx.drawImage(this.neb as HTMLCanvasElement, -w * 0.05 + ox, -h * 0.05 + oy, w * 1.1, h * 1.1);

    ctx.globalCompositeOperation = "lighter";

    // Star dust: tiny near-white points streaming past; at speed each one
    // trails a short line back toward where it came from.
    const streak = s.still ? 0 : s.streak;
    ctx.lineWidth = 1;
    for (const p of this.dust) {
      const q = projectLooped(p, cam, view, DUST_DEPTH);
      if (!q || !onScreen(q, view, 4)) continue;
      const a = Math.min(0.8, 260 / q.dz);
      if (streak > 0.03) {
        const tail = project({ x: p.x, y: p.y, z: cam.z + q.dz + STREAK_LEN * streak }, cam, view);
        if (tail) {
          ctx.strokeStyle = `rgba(235,240,255,${(a * 0.55).toFixed(3)})`;
          ctx.beginPath();
          ctx.moveTo(q.x, q.y);
          ctx.lineTo(tail.x, tail.y);
          ctx.stroke();
          continue;
        }
      }
      ctx.fillStyle = `rgba(235,240,255,${a.toFixed(3)})`;
      ctx.fillRect(q.x, q.y, 1.2, 1.2);
    }

    // Stars: one sprite per cluster, sized by stage and depth.
    const proj: (Projected | null)[] = new Array(stars.length);
    let near: number | null = null, nearDz = Infinity;
    for (let k = 0; k < stars.length; k++) {
      const st = stars[k];
      const q = projectLooped(st, cam, view, this.galaxy.depth);
      proj[k] = q;
      if (!q) continue;
      const size = Math.min(64, st.size * 28 * q.f + 4);
      if (!onScreen(q, view, size)) continue;
      const tw = s.still ? 0.92 : 0.82 + 0.18 * Math.sin(t * 1.3 + k * 0.7);
      const far = Math.min(1, (this.galaxy.depth - q.dz) / (this.galaxy.depth * FAR_FADE));
      ctx.globalAlpha = Math.min(1, 0.12 + q.f * 2.2) * far * tw * st.glow;
      ctx.drawImage(this.sprites[st.cluster], q.x - size / 2, q.y - size / 2, size, size);
      if (q.dz < nearDz && q.dz >= NEAR_FLOOR && onScreen(q, view, 0)) {
        nearDz = q.dz;
        near = k;
      }
    }
    ctx.globalAlpha = 1;
    this.lastProjected = proj;
    this.nearestIndex = near;

    ctx.globalCompositeOperation = "source-over";

    // Labels under the nearest few stars only.
    if (!s.quiet) {
      const close: number[] = [];
      for (let k = 0; k < stars.length; k++) {
        const q = proj[k];
        if (q && q.dz < LABEL_RANGE && onScreen(q, view, 0)) close.push(k);
      }
      close.sort((a, b) => (proj[a] as Projected).dz - (proj[b] as Projected).dz);
      ctx.font = `400 12px ${s.font}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      // Nearest first; a label that would sit on another label or on the UI
      // (header, controls, map) is skipped, and one near the edge is nudged
      // inward so it never reads cut off.
      const placed: { x: number; y: number; w: number }[] = [];
      let shown = 0;
      for (const k of close) {
        if (shown >= LABEL_COUNT) break;
        const q = proj[k] as Projected;
        let txt = stars[k].star.text || "";
        if (txt.length > 34) txt = txt.slice(0, 33) + "…";
        const tw = ctx.measureText(txt).width;
        const x = Math.min(w - tw / 2 - 8, Math.max(tw / 2 + 8, q.x));
        const y = q.y + 14;
        if (y > h - 18 || hitsKeepOut(x, y, tw, s.keepOut)) continue;
        if (placed.some((p) => Math.abs(p.y - y) < 16 && Math.abs(p.x - x) < (p.w + tw) / 2 + 12)) continue;
        placed.push({ x, y, w: tw });
        shown++;
        const a = Math.min(0.85, (LABEL_RANGE - q.dz) / 900);
        ctx.fillStyle = `rgba(226,232,240,${a.toFixed(3)})`;
        ctx.fillText(txt, x, y);
      }
    }

    // Target ring.
    if (s.target != null) {
      const q = proj[s.target];
      if (q) {
        ctx.strokeStyle = "rgba(221,215,255,0.7)";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(q.x, q.y, 17 + (s.still ? 0 : 2 * Math.sin(t * 2)), 0, Math.PI * 2);
        ctx.stroke();
      }
    }
  }

  /**
   * The small corner map, seen from above: the corridor runs left → right (z),
   * stars sit at their sideways offset (x), you are the ring moving along it.
   */
  drawMap(ctx: CanvasRenderingContext2D, dpr: number, w: number, h: number, cam: Cam, target: number | null) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const m = this.mapScale(w, h);
    ctx.strokeStyle = "rgba(226,232,240,0.12)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(m.x0, m.cy + 0.5);
    ctx.lineTo(m.x0 + this.galaxy.depth * m.sz, m.cy + 0.5);
    ctx.stroke();
    for (const st of this.galaxy.stars) {
      ctx.fillStyle = `rgba(${this.galaxy.clusters[st.cluster].rgb},0.8)`;
      const r = st.size > 1.6 ? 1.4 : 1;
      ctx.fillRect(m.x0 + st.z * m.sz - r / 2, m.cy + st.x * m.sx - r / 2, r, r);
    }
    if (target != null) {
      const st = this.galaxy.stars[target];
      ctx.strokeStyle = "rgba(221,215,255,0.9)";
      ctx.beginPath();
      ctx.arc(m.x0 + st.z * m.sz, m.cy + st.x * m.sx, 3.5, 0, Math.PI * 2);
      ctx.stroke();
    }
    const camZ = ((cam.z % this.galaxy.depth) + this.galaxy.depth) % this.galaxy.depth;
    ctx.strokeStyle = "#DDD7FF";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(m.x0 + camZ * m.sz, m.cy + cam.x * m.sx, 4, 0, Math.PI * 2);
    ctx.stroke();
  }

  /** Map px → nearest star index (for tap-to-fly on the map). */
  pickOnMap(w: number, h: number, px: number, py: number, radius = 10): number | null {
    const m = this.mapScale(w, h);
    let best: number | null = null, bd = radius * radius;
    for (const st of this.galaxy.stars) {
      const dx = m.x0 + st.z * m.sz - px, dy = m.cy + st.x * m.sx - py, d = dx * dx + dy * dy;
      if (d < bd) {
        bd = d;
        best = st.index;
      }
    }
    return best;
  }

  private mapScale(w: number, h: number) {
    let mx = 1;
    for (const st of this.galaxy.stars) mx = Math.max(mx, Math.abs(st.x));
    return { sz: (w - 12) / Math.max(1, this.galaxy.depth), sx: (h / 2 - 6) / mx, x0: 6, cy: h / 2 };
  }
}
