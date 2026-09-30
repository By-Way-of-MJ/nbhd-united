/**
 * "Your people" star cluster — the canvas painter (ported from the approved
 * WebPeopleCluster mockup, built on the Constellation night-sky techniques).
 * You are the bright centre star; friends are stars placed by the model in
 * lib/people-sky/layout.ts; clusters are soft noise nebulae in their own hue;
 * friends-of-friends are faint nameless glimmers beyond each friend.
 *
 * The backdrop (deep gradient + distant stars) is cached offscreen and only
 * repainted on resize; sprites are drawn once per colour and stamped. Under
 * reduced motion nothing twinkles or drifts, flights become jumps, and idle
 * frames are skipped entirely.
 */

import { easeInOut } from "../night-sky/model";
import { ctx2d, fbm, makeCanvas, seeded } from "../sky-art/noise";
import {
  flightMs,
  homePose,
  lerpPose,
  maxDist,
  MIN_DIST,
  nearAngle,
  pickLabels,
  type LabelCandidate,
  type PeopleSkyModel,
  type Pose,
  type Vec3,
} from "./layout";

const RING = "196,187,255";
const LABEL_CAP = 10;

interface Screen { x: number; y: number; d: number; f: number }
export type SkyPick = { kind: "person"; index: number } | { kind: "cluster"; index: number };
export type SkySelection = SkyPick | null;

/** On-screen star size (CSS px): nearer and closer friends are bigger. */
function starSize(f: number, close: number): number {
  return Math.max(10, Math.min(60, f * (0.46 + close * 0.46)));
}

function hslRgb(h: number, s: number, l: number): string {
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
  };
  return `${f(0)},${f(8)},${f(4)}`;
}

/** Friend star colour: pale, in the friend's (or their cluster's) hue. */
export function starRgb(hue: number, inCluster: boolean): string {
  return hslRgb(((hue % 360) + 360) % 360, inCluster ? 0.7 : 0.35, inCluster ? 0.88 : 0.92);
}
export function nebulaRgb(hue: number): string {
  return hslRgb(((hue % 360) + 360) % 360, 0.55, 0.72);
}
/** CSS colour for UI that names a cluster (the card eyebrow, list dot). */
export function clusterCss(hue: number): string {
  return `rgb(${nebulaRgb(hue)})`;
}

function sprite(rgb: string, size = 64): HTMLCanvasElement {
  const c = makeCanvas(size, size), g = ctx2d(c), h = size / 2;
  const gr = g.createRadialGradient(h, h, 0, h, h, h);
  gr.addColorStop(0, `rgba(${rgb},1)`);
  gr.addColorStop(0.09, `rgba(${rgb},0.92)`);
  gr.addColorStop(0.2, `rgba(${rgb},0.22)`);
  gr.addColorStop(0.52, `rgba(${rgb},0.045)`);
  gr.addColorStop(1, `rgba(${rgb},0)`);
  g.fillStyle = gr;
  g.fillRect(0, 0, size, size);
  return c;
}

/** A soft, uneven nebula: radial falloff modulated by fractal noise. */
function nebulaSprite(rgb: string, seed: number): HTMLCanvasElement {
  const S = 128, c = makeCanvas(S, S), g = ctx2d(c);
  const img = g.createImageData(S, S), d = img.data;
  const [r, gg, b] = rgb.split(",").map(Number);
  for (let y = 0; y < S; y++) {
    for (let x = 0; x < S; x++) {
      const dx = (x - S / 2) / (S / 2), dy = (y - S / 2) / (S / 2);
      const rr = Math.sqrt(dx * dx + dy * dy);
      if (rr >= 1) continue;
      const fall = Math.pow(1 - rr, 1.8);
      const n = fbm(x / 22, y / 22, seed, 4);
      const a = fall * (0.3 + 1.1 * n * n) * 0.46;
      const o = (y * S + x) * 4;
      d[o] = r;
      d[o + 1] = gg;
      d[o + 2] = b;
      d[o + 3] = Math.round(Math.min(1, a) * 255);
    }
  }
  g.putImageData(img, 0, 0);
  return c;
}

export class PeopleSkyRenderer {
  reduced = false;
  compact = false;
  /** Canvas px reserved at the top/bottom for overlaid text (labels avoid them). */
  insetTop = 0;
  insetBottom = 0;
  /** A card is open over the sky (right side on wide screens, the bottom on phones). */
  cardOpen = false;
  private shift = { x: 0, y: 0 };

  private model: PeopleSkyModel | null = null;
  private sel: SkySelection = null;
  private hover = -1;
  private hits: Set<number> | null = null;
  private cam: Pose = { yaw: 0.4, pitch: 0.24, dist: 16, target: [0, 0, 0] };
  private flight: { from: Pose; to: Pose; start: number; dur: number } | null = null;
  private lastInput = -Infinity;
  private lastT = 0;
  private needsDraw = true;

  private bg: HTMLCanvasElement | null = null;
  private bgKey = "";
  private bgStars: { x: number; y: number; m: number; t: number }[] = [];
  private sprites: { you: HTMLCanvasElement; glim: HTMLCanvasElement; tints: Map<string, HTMLCanvasElement>; nebulae: Map<string, HTMLCanvasElement> } | null = null;
  private font = "Georgia, serif";
  private sans = "system-ui, sans-serif";
  private picks: { i: number; x: number; y: number; r: number; d: number }[] = [];
  private clusterPicks: { i: number; x: number; y: number; r: number }[] = [];
  private view = { w: 1, h: 1, F: 1 };

  constructor() {
    const r = seeded(23);
    for (let i = 0; i < 900; i++) this.bgStars.push({ x: r(), y: r(), m: Math.pow(r(), 3), t: r() });
  }

  // ── State ───────────────────────────────────────────────────────────────

  invalidate(): void {
    this.needsDraw = true;
  }

  setModel(model: PeopleSkyModel, first: boolean): void {
    this.model = model;
    if (first) this.cam = homePose(model, this.cam.yaw);
    this.needsDraw = true;
  }

  setSelection(sel: SkySelection): void {
    this.sel = sel;
    this.needsDraw = true;
  }

  setHover(i: number): void {
    if (i === this.hover) return;
    this.hover = i;
    this.needsDraw = true;
  }

  setHits(hits: Set<number> | null): void {
    this.hits = hits && hits.size ? hits : null;
    this.needsDraw = true;
  }

  pose(): Pose {
    return { ...this.cam, target: [...this.cam.target] as Vec3 };
  }

  /** Calm eased flight; under reduced motion, a jump. */
  goTo(pose: Pose, now: number): void {
    const to = { ...pose, yaw: nearAngle(this.cam.yaw, pose.yaw) };
    if (this.reduced) {
      this.cam = to;
      this.flight = null;
      this.needsDraw = true;
      return;
    }
    this.flight = { from: this.pose(), to, start: now, dur: flightMs(this.cam, to) };
    this.needsDraw = true;
  }

  orbit(dx: number, dy: number, now: number): void {
    this.flight = null;
    this.lastInput = now;
    this.cam.yaw += dx * 0.0065;
    this.cam.pitch = Math.max(-0.95, Math.min(0.95, this.cam.pitch + dy * 0.0045));
    this.needsDraw = true;
  }

  zoom(factor: number, now: number): void {
    if (!this.model) return;
    if (this.flight) {
      // Zooming mid-flight lands where the flight was headed, closer or further.
      this.cam = { ...this.flight.to };
      this.flight = null;
    }
    this.lastInput = now;
    this.cam.dist = Math.max(MIN_DIST, Math.min(maxDist(this.model), this.cam.dist * factor));
    this.needsDraw = true;
  }

  animating(): boolean {
    return this.needsDraw || !!this.flight || !this.reduced;
  }

  pick(x: number, y: number, touch: boolean): SkyPick | null {
    let best: SkyPick | null = null, bd = Infinity;
    for (const p of this.picks) {
      const r = p.r + (touch ? 16 : 8);
      const d = (p.x - x) ** 2 + (p.y - y) ** 2;
      if (d < r * r && d < bd) {
        bd = d;
        best = { kind: "person", index: p.i };
      }
    }
    if (best) return best;
    let br = Infinity;
    for (const c of this.clusterPicks) {
      if ((c.x - x) ** 2 + (c.y - y) ** 2 < c.r * c.r && c.r < br) {
        br = c.r;
        best = { kind: "cluster", index: c.i };
      }
    }
    return best;
  }

  // ── Projection ──────────────────────────────────────────────────────────

  private proj(p: Vec3): Screen | null {
    const c = this.cam;
    const x = p[0] - c.target[0], y = p[1] - c.target[1], z = p[2] - c.target[2];
    const cy = Math.cos(c.yaw), sy = Math.sin(c.yaw);
    const x1 = x * cy - z * sy, z1 = x * sy + z * cy;
    const cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
    const y1 = y * cp - z1 * sp, z2 = y * sp + z1 * cp;
    const d = z2 + c.dist;
    if (d < 0.6) return null;
    const f = this.view.F / d;
    const top = this.insetTop, bottom = this.view.h - this.insetBottom;
    return { x: this.view.w / 2 + this.shift.x + x1 * f, y: (top + bottom) / 2 + this.shift.y - y1 * f, d, f };
  }

  // ── Frame ───────────────────────────────────────────────────────────────

  draw(ctx: CanvasRenderingContext2D, w: number, h: number, dpr: number, now: number): void {
    this.needsDraw = false;
    if (!this.sprites) this.initSprites();
    this.view = { w, h, F: Math.min(w * 0.62, (h - this.insetTop - this.insetBottom) * 1.05) };
    const dt = this.lastT ? Math.min(64, now - this.lastT) : 16;
    this.lastT = now;
    // Make room for an open card: frame the sky beside it (wide) or above it (phone).
    const want = { x: this.cardOpen && !this.compact ? -Math.min(190, w * 0.16) : 0, y: 0 };
    const k = this.reduced ? 1 : 1 - Math.exp(-dt / 220);
    this.shift.x += (want.x - this.shift.x) * k;
    this.shift.y += (want.y - this.shift.y) * k;
    if (Math.abs(want.x - this.shift.x) + Math.abs(want.y - this.shift.y) > 0.5) this.needsDraw = true;

    if (this.flight) {
      const t = (now - this.flight.start) / this.flight.dur;
      this.cam = lerpPose(this.flight.from, this.flight.to, easeInOut(t));
      if (t >= 1) this.flight = null;
    } else if (!this.reduced && !this.sel && now - this.lastInput > 3500) {
      // A slow drift while you're just looking.
      this.cam.yaw += dt * 0.000045;
    }

    const W = Math.round(w * dpr), H = Math.round(h * dpr);
    const key = `${W}x${H}`;
    if (key !== this.bgKey || !this.bg) {
      this.bg = makeCanvas(W, H);
      const b = ctx2d(this.bg);
      b.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.paintBackdrop(b, w, h);
      this.bgKey = key;
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, W, H);
    ctx.drawImage(this.bg, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.paintSky(ctx, now / 1000);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
  }

  private initSprites(): void {
    this.sprites = { you: sprite("255,247,230", 96), glim: sprite("214,222,255", 32), tints: new Map(), nebulae: new Map() };
    if (typeof document !== "undefined") {
      const cs = getComputedStyle(document.documentElement);
      const serif = cs.getPropertyValue("--font-serif").trim();
      const body = cs.getPropertyValue("--font-body").trim();
      if (serif) this.font = `${serif}, Georgia, serif`;
      if (body) this.sans = `${body}, system-ui, sans-serif`;
      document.fonts?.load(`italic 400 18px ${this.font}`).then(() => this.invalidate()).catch(() => {});
    }
  }

  private tint(rgb: string): HTMLCanvasElement {
    const m = this.sprites!.tints;
    let s = m.get(rgb);
    if (!s) m.set(rgb, (s = sprite(rgb)));
    return s;
  }

  private nebula(rgb: string, seed: number): HTMLCanvasElement {
    const m = this.sprites!.nebulae, k = `${rgb}:${seed}`;
    let s = m.get(k);
    if (!s) m.set(k, (s = nebulaSprite(rgb, seed)));
    return s;
  }

  private paintBackdrop(ctx: CanvasRenderingContext2D, w: number, h: number): void {
    // Transparent edges: the page's own sky shows through, so there's no box.
    ctx.clearRect(0, 0, w, h);
    const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.min(w, h) * 0.75);
    g.addColorStop(0, "rgba(18,24,48,0.55)");
    g.addColorStop(0.6, "rgba(10,14,30,0.25)");
    g.addColorStop(1, "rgba(7,9,12,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = "lighter";
    const white = this.tint("236,240,255"), blue = this.tint("206,220,255"), warm = this.tint("255,236,214");
    const density = Math.min(1, (w * h) / (1100 * 620));
    const count = Math.round(this.bgStars.length * Math.max(0.35, density));
    for (let i = 0; i < count; i++) {
      const s = this.bgStars[i];
      const size = 1.4 + s.m * 5.5;
      ctx.globalAlpha = 0.14 + s.m * 0.55;
      ctx.drawImage(s.t > 0.86 ? warm : s.t > 0.6 ? blue : white, s.x * w - size / 2, s.y * h - size / 2, size, size);
    }
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
  }

  private paintSky(ctx: CanvasRenderingContext2D, t: number): void {
    this.picks = [];
    this.clusterPicks = [];
    const model = this.model;
    const { w, h } = this.view;
    if (!model) return;
    const sp = this.sprites!;
    const sel = this.sel;
    const selPerson = sel?.kind === "person" ? sel.index : -1;
    const selCluster = sel?.kind === "cluster" ? sel.index : -1;
    const focusCluster = selCluster >= 0 ? selCluster : selPerson >= 0 ? model.people[selPerson].cluster : -1;
    const hits = this.hits;
    const tw = (n: number, speed = 1.2) => (this.reduced ? 1 : 0.8 + 0.2 * Math.sin(t * speed + n * 1.9));
    const onScreen = (s: Screen, m = 60) => s.x > -m && s.x < w + m && s.y > -m && s.y < h + m;

    // Nebulae, one soft cloud per member plus one over the whole cluster.
    ctx.globalCompositeOperation = "lighter";
    model.clusters.forEach((c, ci) => {
      const rgb = nebulaRgb(c.hue);
      const cp = this.proj(c.centre);
      if (!cp) return;
      let a = focusCluster < 0 ? 0.85 : ci === focusCluster ? 1 : 0.4;
      if (hits) a *= 0.6;
      const big = cp.f * c.radius * 2.9;
      ctx.globalAlpha = a;
      ctx.drawImage(this.nebula(rgb, 3 + ci), cp.x - big / 2, cp.y - big / 2, big, big);
      c.members.forEach((mi, k) => {
        const mp = this.proj(model.people[mi].pos);
        if (!mp) return;
        const s = mp.f * (2.2 + (k % 3) * 0.55);
        ctx.globalAlpha = a * 0.7;
        ctx.drawImage(this.nebula(rgb, 11 + ((ci + k) % 4)), mp.x - s / 2, mp.y - s / 2, s, s);
      });
      this.clusterPicks.push({ i: ci, x: cp.x, y: cp.y, r: Math.max(24, cp.f * c.radius * 0.95) });
    });

    // Friends of friends: faint and nameless, brighter around the one in focus.
    const focusVia = selPerson >= 0 ? selPerson : this.hover;
    for (let i = 0; i < model.glimmers.length; i++) {
      const g = model.glimmers[i];
      const gp = this.proj(g.pos);
      if (!gp || !onScreen(gp, 10)) continue;
      const focus = g.via === focusVia;
      if (focus) {
        const vp = this.proj(model.people[g.via].pos);
        if (vp) {
          ctx.globalCompositeOperation = "source-over";
          ctx.globalAlpha = 1;
          ctx.strokeStyle = "rgba(214,222,255,0.13)";
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          ctx.moveTo(vp.x, vp.y);
          ctx.lineTo(gp.x, gp.y);
          ctx.stroke();
          ctx.globalCompositeOperation = "lighter";
        }
      }
      ctx.globalAlpha = (focus ? 0.7 : hits ? 0.08 : 0.2) * tw(i, 1.7);
      const s = Math.max(2.5, Math.min(9, gp.f * 0.2 * (0.6 + g.m)));
      ctx.drawImage(sp.glim, gp.x - s / 2, gp.y - s / 2, s, s);
    }

    // Threads from you: thicker and brighter for the people closest to you.
    const you = this.proj([0, 0, 0]);
    const crowd = model.people.length > 60;
    ctx.globalCompositeOperation = "source-over";
    ctx.lineCap = "round";
    if (you) {
      model.people.forEach((p, i) => {
        const on = i === selPerson || i === this.hover, hit = hits?.has(i);
        // In a crowd only your sky gets threads — and only the one in focus while searching.
        if (crowd && !on && !hit && (!p.inSky || hits || selPerson >= 0)) return;
        const pp = this.proj(p.pos);
        if (!pp) return;
        const base = on || hit ? 0.42 : (0.05 + p.close * 0.13) * (hits ? 0.4 : 1);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = `rgba(226,224,255,${base.toFixed(3)})`;
        ctx.lineWidth = on ? 1.5 : 0.45 + p.close * 1.2;
        ctx.beginPath();
        ctx.moveTo(you.x, you.y);
        ctx.lineTo(pp.x, pp.y);
        ctx.stroke();
      });
    }

    // Friends, far to near.
    const shown = model.people
      .map((p, i) => ({ p, i, s: this.proj(p.pos) }))
      .filter((o): o is { p: (typeof model.people)[number]; i: number; s: Screen } => !!o.s && onScreen(o.s))
      .sort((a, b) => b.s.d - a.s.d);
    ctx.globalCompositeOperation = "lighter";
    for (const { p, i, s } of shown) {
      const on = i === selPerson;
      const dim = hits ? !hits.has(i) : selCluster >= 0 && p.cluster !== selCluster;
      const size = starSize(s.f, p.close) + (on ? 12 : 0);
      const spr = this.tint(starRgb(p.hue, p.cluster >= 0));
      const a = (dim ? 0.22 : 0.8 + 0.2 * p.close) * tw(i);
      // A wide soft halo, then the star itself — bright core, faint spill.
      ctx.globalAlpha = a * 0.28;
      ctx.drawImage(spr, s.x - size * 1.3, s.y - size * 1.3, size * 2.6, size * 2.6);
      ctx.globalAlpha = a;
      ctx.drawImage(spr, s.x - size / 2, s.y - size / 2, size, size);
      this.picks.push({ i, x: s.x, y: s.y, r: Math.max(10, size * 0.28), d: s.d });
    }

    // You — the brightest star, breathing slowly.
    if (you) {
      const ys = Math.max(48, Math.min(120, you.f * 1.9)) * (this.reduced ? 1 : 1 + 0.04 * Math.sin(t * 0.8));
      ctx.globalAlpha = 1;
      ctx.drawImage(sp.you, you.x - ys / 2, you.y - ys / 2, ys, ys);
    }
    ctx.globalCompositeOperation = "source-over";

    // Selection ring — the one violet accent.
    if (selPerson >= 0) {
      const s = this.proj(model.people[selPerson].pos);
      if (s) {
        const r = 17 + (this.reduced ? 0 : 1.5 * Math.sin(t * 1.6));
        ctx.globalAlpha = 0.85;
        ctx.strokeStyle = `rgba(${RING},0.9)`;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.arc(s.x, s.y, r, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    // Labels: cluster names and up to ten people, never piled on each other.
    const cands: (LabelCandidate & { text: string; kind: "person" | "cluster" | "you"; size: number; color: string; alpha: number })[] = [];
    const add = (c: (typeof cands)[number]) => cands.push(c);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    if (you) {
      const fs = this.compact ? 16 : 19;
      ctx.font = `italic 400 ${fs}px ${this.font}`;
      const yr = Math.max(48, Math.min(120, you.f * 1.9));
      add({ key: "you", x: you.x, y: you.y + yr * 0.28 + 12, w: ctx.measureText("You").width, h: fs, priority: 0, force: true, text: "You", kind: "you", size: fs, color: "#FFFFFF", alpha: 0.95 });
    }
    model.clusters.forEach((c, ci) => {
      const fs = 11;
      ctx.font = `600 ${fs}px ${this.sans}`;
      const text = c.name.toUpperCase();
      const width = ctx.measureText(text).width + text.length * 1.6;
      // Below the nebula, else above it, else at its heart.
      [-0.7, 0.75, 0].forEach((dy, k) => {
        const lp = this.proj([c.centre[0], c.centre[1] + c.radius * dy, c.centre[2]]);
        if (!lp) return;
        add({ key: `c${ci}:${k}`, group: `c${ci}`, x: lp.x, y: lp.y, w: width, h: fs + 2, priority: 1000 - k + (ci === focusCluster ? 100 : 0), force: ci === selCluster && k === 0, text, kind: "cluster", size: fs, color: `rgb(${nebulaRgb(c.hue)})`, alpha: focusCluster < 0 || ci === focusCluster ? 0.85 : 0.4 });
      });
    });
    const near = [...shown].reverse();
    for (const { p, i, s } of near) {
      const on = i === selPerson || i === this.hover;
      const hit = hits?.has(i) ?? false;
      if (hits && !hit && !on) continue;
      const fs = Math.round(Math.max(13, Math.min(20, s.f * 0.5 + 6)));
      ctx.font = `italic 400 ${fs}px ${this.font}`;
      const size = starSize(s.f, p.close);
      const priority = (hit ? 50 : 0) + (p.inSky ? 20 : 0) + p.close * 10 + Math.min(10, s.f / 6);
      add({ key: `p${i}`, x: s.x, y: s.y - size * 0.32 - fs * 0.7, w: ctx.measureText(p.name).width, h: fs, priority, force: on, text: p.name, kind: "person", size: fs, color: on || hit ? "#EDEAFF" : "rgba(236,240,255,0.9)", alpha: on ? 1 : 0.86 });
    }
    const top = this.insetTop + 4, bottom = h - this.insetBottom;
    const inBand = cands.filter((c) => c.force || (c.y - c.h > top && c.y + c.h < bottom));
    const clusterCount = model.clusters.length;
    const cardW = this.cardOpen && !this.compact ? 380 : 0;
    const cardH = 0;
    const placed = new Set(pickLabels(inBand, LABEL_CAP + clusterCount + 1, w - cardW, h - cardH));
    for (const c of inBand) {
      if (!placed.has(c.key)) continue;
      ctx.globalAlpha = c.alpha;
      ctx.fillStyle = c.color;
      if (c.kind === "cluster") {
        ctx.font = `600 ${c.size}px ${this.sans}`;
        // Letter-spaced small caps, drawn by hand (canvas letterSpacing isn't everywhere).
        const chars = [...c.text];
        const widths = chars.map((ch) => ctx.measureText(ch).width + 1.6);
        let x = c.x - widths.reduce((a, b) => a + b, 0) / 2;
        ctx.textAlign = "left";
        chars.forEach((ch, k) => {
          ctx.fillText(ch, x, c.y);
          x += widths[k];
        });
        ctx.textAlign = "center";
      } else {
        ctx.font = `italic 400 ${c.size}px ${this.font}`;
        ctx.fillText(c.text, c.x, c.y);
      }
    }
    ctx.globalAlpha = 1;
  }
}
