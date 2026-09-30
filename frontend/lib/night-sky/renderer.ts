/**
 * Constellation night sky — the canvas painter (ported from the approved
 * WebLessonsDeep mockup). One dome: a procedural Milky Way with dust lanes,
 * thousands of soft background stars in real star colours, a few faint distant
 * galaxies, drifting star dust for depth, the user's lessons as constellations,
 * hills at first light and a dome vignette. Stereographic projection.
 *
 * Everything that doesn't change while the camera rests (sky, background stars,
 * dust, hills, vignette) is cached offscreen and only repainted when the camera
 * moves or the canvas resizes; lessons, figures and labels draw every frame.
 */

import { ctx2d, fbm, makeCanvas, seeded, smooth } from "../sky-art/noise";
import {
  angDiff,
  clusterRadius,
  D2R,
  dirv,
  easeInOut,
  easeSpeed,
  lerpPose,
  LOOK_BELOW,
  moveDuration,
  type Pose,
  type SkyModel,
  type Vec3,
} from "./model";

const BOX = 90;
const SKY_BG = "#030408";
const RING = "196,187,255";

interface Far { v: Vec3; m: number; t: number }
interface Dust { x: number; y: number; z: number; m: number; t: number }
interface Galaxy { v: Vec3; rot: number; e: number; warm: boolean; s: number }
interface Screen { x: number; y: number; d: number }
interface Move { from: Pose; to: Pose; start: number; dur: number }

function wrap(v: number, c: number): number {
  let d = v - c;
  d = (((d + BOX / 2) % BOX) + BOX) % BOX - BOX / 2;
  return c + d;
}

function clamp255(v: number): number {
  return v < 0 ? 0 : v > 255 ? 255 : v;
}

function sprite(rgb: string): HTMLCanvasElement {
  const c = makeCanvas(64, 64), g = ctx2d(c), gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  gr.addColorStop(0, `rgba(${rgb},1)`);
  gr.addColorStop(0.08, `rgba(${rgb},0.95)`);
  gr.addColorStop(0.18, `rgba(${rgb},0.2)`);
  gr.addColorStop(0.5, `rgba(${rgb},0.04)`);
  gr.addColorStop(1, `rgba(${rgb},0)`);
  g.fillStyle = gr;
  g.fillRect(0, 0, 64, 64);
  return c;
}

function galaxySprite(rgb: string): HTMLCanvasElement {
  const c = makeCanvas(96, 96), g = ctx2d(c), gr = g.createRadialGradient(48, 48, 0, 48, 48, 48);
  gr.addColorStop(0, "rgba(255,248,236,0.9)");
  gr.addColorStop(0.1, `rgba(${rgb},0.45)`);
  gr.addColorStop(0.45, `rgba(${rgb},0.08)`);
  gr.addColorStop(1, `rgba(${rgb},0)`);
  g.fillStyle = gr;
  g.fillRect(0, 0, 96, 96);
  return c;
}

/** Fine four-point diffraction glint, drawn once and stamped. */
function glintSprite(): HTMLCanvasElement {
  const c = makeCanvas(128, 128), g = ctx2d(c);
  const h = g.createLinearGradient(0, 64, 128, 64);
  h.addColorStop(0, "rgba(237,243,255,0)");
  h.addColorStop(0.5, "rgba(255,255,255,0.4)");
  h.addColorStop(1, "rgba(237,243,255,0)");
  g.fillStyle = h;
  g.fillRect(0, 63.2, 128, 1.6);
  const v = g.createLinearGradient(64, 0, 64, 128);
  v.addColorStop(0, "rgba(237,243,255,0)");
  v.addColorStop(0.5, "rgba(255,255,255,0.4)");
  v.addColorStop(1, "rgba(237,243,255,0)");
  g.fillStyle = v;
  g.fillRect(63.2, 0, 1.6, 128);
  return c;
}

/** Milky Way panorama value: az 0..360 across, alt -15..75 down. */
function milkyVal(az: number, alt: number): [number, number] {
  const mid = 26 + 18 * Math.sin((az + 30) * D2R);
  const perp = (alt - mid) / 11 + 0.35 * (fbm(az / 40, 0.5, 41, 3) - 0.5);
  const core = Math.exp(-perp * perp), bulge = Math.exp(-Math.pow(angDiff(az, 110) / 55, 2));
  const n = fbm(az / 14, alt / 9, 7, 5);
  const dust = smooth(0.46, 0.78, fbm(az / 9 + 4, alt / 6, 29, 5)) * Math.exp(-Math.pow((perp - 0.1) / 0.55, 2));
  const lane = Math.exp(-Math.pow((perp + 0.05) / 0.16, 2)) * smooth(0.35, 0.6, fbm(az / 6, alt / 5, 51, 3)) * (0.4 + bulge);
  return [core * (0.25 + 0.75 * n) * (0.5 + 0.8 * bulge) * (1 - 0.6 * dust) * (1 - 0.45 * Math.min(1, lane)), bulge];
}

export interface PickedStar {
  id: number;
  cluster: number;
}

export class NightSkyRenderer {
  reduced = false;
  compact = false;
  /** Canvas y (CSS px) where the lesson text begins; constellation names fade out above it. */
  labelFloor = Infinity;
  /** Canvas y (CSS px) where the header ends — narrow screens centre the sky between the two. */
  labelCeil = 0;

  private model: SkyModel | null = null;
  private selCluster = -1;
  private selLesson = -1;
  private matches: Set<number> | null = null;
  private cam: Pose = { p: [0, 0, 0], az: 0, alt: 18 };
  private move: Move | null = null;
  private fade: { snap: HTMLCanvasElement; start: number } | null = null;
  private lastPose: Pose = { p: [0, 0, 0], az: 0, alt: 18 };
  private needsDraw = true;

  private far: Far[] = [];
  private dust: Dust[] = [];
  private galaxies: Galaxy[] = [];
  private sprites: { white: HTMLCanvasElement; blue: HTMLCanvasElement; warm: HTMLCanvasElement; glint: HTMLCanvasElement; gw: HTMLCanvasElement; gc: HTMLCanvasElement; tints: Map<string, HTMLCanvasElement> } | null = null;

  private mw: HTMLCanvasElement | null = null;
  private mwImg: ImageData | null = null;
  private mwRow = 0;
  private readonly mwW = 720;
  private readonly mwH = 208;

  private bg: HTMLCanvasElement | null = null;
  private fg: HTMLCanvasElement | null = null;
  private cacheKey = "";
  private font = "Georgia, serif";
  private picks: { id: number; cluster: number; x: number; y: number }[] = [];
  private canvas: HTMLCanvasElement | null = null;
  private view = { w: 1, h: 1, sc: 1, cx: 0, cy: 0, u: 1 };

  constructor() {
    const r = seeded(5);
    for (let i = 0; i < 1800; i++) this.dust.push({ x: (r() - 0.5) * BOX, y: (r() - 0.5) * BOX, z: (r() - 0.5) * BOX, m: 0.15 + Math.pow(r(), 3) * 0.85, t: r() });
    for (let f = 0; f < 3000; f++) {
      const az = r() * 360, alt = Math.asin(Math.min(1, r() * 1.25 - 0.25)) / D2R;
      const band = Math.exp(-Math.pow((alt - (26 + 18 * Math.sin((az + 30) * D2R))) / 13, 2));
      if (r() > 0.3 + 0.7 * band) continue;
      this.far.push({ v: dirv(az, alt), m: Math.pow(r(), 3), t: r() });
    }
    for (let k = 0; k < 9; k++) this.galaxies.push({ v: dirv(r() * 360, 10 + r() * 55), rot: r() * Math.PI, e: 0.2 + r() * 0.4, warm: r() > 0.5, s: 7 + r() * 9 });
  }

  // ── State ────────────────────────────────────────────────────────────────

  /** Ask for a repaint (matters under reduced motion, where idle frames are skipped). */
  invalidate(): void {
    this.needsDraw = true;
  }

  setModel(model: SkyModel): void {
    this.model = model;
    this.needsDraw = true;
  }

  setSelection(cluster: number, lessonId: number): void {
    this.selCluster = cluster;
    this.selLesson = lessonId;
    this.needsDraw = true;
  }

  setMatches(ids: Set<number> | null): void {
    this.matches = ids;
    this.needsDraw = true;
  }

  /** Jump without animation (first paint). */
  place(pose: Pose): void {
    this.cam = { p: [...pose.p] as Vec3, az: pose.az, alt: pose.alt };
    this.move = null;
    this.cacheKey = "";
    this.needsDraw = true;
  }

  /** Calm eased flight to a pose; under reduced motion, a cross-fade jump. */
  goTo(pose: Pose, now: number): void {
    if (this.reduced) {
      if (this.canvas && this.canvas.width > 1) {
        const snap = makeCanvas(this.canvas.width, this.canvas.height);
        ctx2d(snap).drawImage(this.canvas, 0, 0);
        this.fade = { snap, start: now };
      }
      this.place(pose);
      return;
    }
    const from: Pose = { p: [...this.cam.p] as Vec3, az: this.cam.az, alt: this.cam.alt };
    const dur = moveDuration(from, pose) * 1000;
    this.move = { from, to: pose, start: now, dur };
  }

  /** Does the next frame differ from the last one? */
  animating(): boolean {
    return this.needsDraw || !!this.move || !!this.fade || !this.reduced || !this.mw;
  }

  pick(x: number, y: number, radius: number): PickedStar | null {
    let best: PickedStar | null = null, bd = radius * radius;
    for (const p of this.picks) {
      const d = (p.x - x) * (p.x - x) + (p.y - y) * (p.y - y);
      if (d < bd) {
        bd = d;
        best = { id: p.id, cluster: p.cluster };
      }
    }
    return best;
  }

  // ── Projection ───────────────────────────────────────────────────────────

  private dir(n: Vec3): Screen | null {
    const a = this.cam.az * D2R, e = this.cam.alt * D2R;
    const x1 = n[0] * Math.cos(a) - n[2] * Math.sin(a), z1 = n[0] * Math.sin(a) + n[2] * Math.cos(a), y1 = n[1];
    const y2 = y1 * Math.cos(e) - z1 * Math.sin(e), z2 = y1 * Math.sin(e) + z1 * Math.cos(e);
    if (z2 < -0.35) return null;
    const k = 1 / (1 + z2), { sc, cx, cy } = this.view;
    return { x: cx + x1 * k * sc, y: cy - y2 * k * sc, d: 0 };
  }

  private pt(w: Vec3): Screen | null {
    const c = this.cam.p, dx = w[0] - c[0], dy = w[1] - c[1], dz = w[2] - c[2];
    const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1e-6;
    const s = this.dir([dx / d, dy / d, dz / d]);
    if (!s) return null;
    s.d = d;
    return s;
  }

  // ── Frame ────────────────────────────────────────────────────────────────

  draw(ctx: CanvasRenderingContext2D, w: number, h: number, dpr: number, now: number): void {
    this.canvas = ctx.canvas;
    this.needsDraw = false;
    if (!this.sprites) this.initSprites();
    this.buildMilkyWay();
    // Narrow screens pull back a little so a whole constellation fits across.
    const sc = Math.max(0.682 * w, 0.833 * h) * (this.compact ? 0.66 : 1);
    let cy = h * 0.46;
    if (this.compact && Number.isFinite(this.labelFloor) && this.labelFloor > this.labelCeil) {
      // The chosen constellation sits LOOK_BELOW degrees above the view centre (sc·tan(θ/2)).
      const mid = this.labelCeil + 0.47 * (this.labelFloor - this.labelCeil);
      cy = Math.min(h * 0.62, Math.max(h * 0.3, mid + sc * Math.tan((LOOK_BELOW / 2) * D2R)));
    }
    this.view = { w, h, sc, cx: w / 2, cy, u: Math.max(0.8, sc / 750) };

    // Camera: one eased curve for travel and turn together.
    let peak = 0;
    if (this.move) {
      const t = (now - this.move.start) / this.move.dur;
      this.cam = lerpPose(this.move.from, this.move.to, easeInOut(t));
      peak = easeSpeed(t);
      if (t >= 1) this.move = null;
    }
    const mv: Vec3 = [this.cam.p[0] - this.lastPose.p[0], this.cam.p[1] - this.lastPose.p[1], this.cam.p[2] - this.lastPose.p[2]];
    this.lastPose = { p: [...this.cam.p] as Vec3, az: this.cam.az, alt: this.cam.alt };

    const W = Math.round(w * dpr), H = Math.round(h * dpr);
    const key = `${W}x${H}:${this.cam.p.map((v) => v.toFixed(3)).join(",")}:${this.cam.az.toFixed(3)}:${this.cam.alt.toFixed(3)}:${this.mw ? 1 : 0}:${this.compact ? 1 : 0}`;
    const streaks = !this.reduced && peak > 0.72;
    if (key !== this.cacheKey || streaks) {
      if (!this.bg || this.bg.width !== W || this.bg.height !== H) {
        this.bg = makeCanvas(W, H);
        this.fg = makeCanvas(W, H);
      }
      const b = ctx2d(this.bg);
      b.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.paintBackground(b, streaks ? mv : null, streaks ? smooth(0.72, 1, peak) : 0);
      const f = ctx2d(this.fg!);
      f.setTransform(1, 0, 0, 1, 0, 0);
      f.clearRect(0, 0, W, H);
      f.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.paintForeground(f);
      this.cacheKey = streaks ? "" : key;
    }

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.drawImage(this.bg!, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.paintLessons(ctx, now / 1000);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.drawImage(this.fg!, 0, 0);

    if (this.fade) {
      const t = (now - this.fade.start) / 450;
      if (t >= 1) this.fade = null;
      else {
        ctx.globalAlpha = 1 - t;
        ctx.drawImage(this.fade.snap, 0, 0, W, H);
        ctx.globalAlpha = 1;
      }
    }
  }

  private initSprites(): void {
    this.sprites = {
      white: sprite("240,244,255"),
      blue: sprite("214,228,255"),
      warm: sprite("255,236,214"),
      glint: glintSprite(),
      gw: galaxySprite("255,226,196"),
      gc: galaxySprite("206,214,255"),
      tints: new Map(),
    };
    if (typeof document !== "undefined") {
      const fam = getComputedStyle(document.documentElement).getPropertyValue("--font-serif").trim();
      if (fam) this.font = `${fam}, Georgia, serif`;
      document.fonts?.load(`italic 400 24px ${this.font}`).catch(() => {});
    }
  }

  private tint(rgb: string): HTMLCanvasElement {
    const m = this.sprites!.tints;
    let s = m.get(rgb);
    if (!s) {
      s = sprite(rgb);
      m.set(rgb, s);
    }
    return s;
  }

  private starColor(t: number): HTMLCanvasElement {
    const s = this.sprites!;
    return t > 0.86 ? s.warm : t > 0.6 ? s.blue : s.white;
  }

  /** Builds the Milky Way panorama a slice per frame so first paint never stalls. */
  private buildMilkyWay(): void {
    if (this.mw) return;
    const W = this.mwW, H = this.mwH;
    if (!this.mwImg) this.mwImg = new ImageData(W, H);
    const d = this.mwImg.data;
    const end = Math.min(H, this.mwRow + 26);
    for (let y = this.mwRow; y < end; y++) {
      for (let x = 0; x < W; x++) {
        const az = (x / W) * 360, alt = 75 - (y / H) * 90;
        let v = milkyVal(az, alt);
        if (az > 300) {
          const k = (az - 300) / 60, v2 = milkyVal(az - 360, alt);
          v = [v[0] * (1 - k) + v2[0] * k, v[1] * (1 - k) + v2[1] * k];
        }
        const I = v[0], warm = v[1] * 0.9, o = (y * W + x) * 4;
        d[o] = clamp255((150 + 105 * warm) * I * 0.5 + 2);
        d[o + 1] = clamp255((166 + 70 * warm) * I * 0.5 + 3);
        d[o + 2] = clamp255((222 - 30 * warm) * I * 0.5 + 6);
        d[o + 3] = 255;
      }
    }
    this.mwRow = end;
    if (end >= H) {
      const c = makeCanvas(W, H);
      ctx2d(c).putImageData(this.mwImg, 0, 0);
      this.mw = c;
      this.mwImg = null;
    }
  }

  private paintBackground(ctx: CanvasRenderingContext2D, mv: Vec3 | null, streak: number): void {
    const { w, h, sc, cx, cy, u } = this.view;
    const c = this.cam;
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = SKY_BG;
    ctx.fillRect(0, 0, w, h);

    // Milky Way across the dome (the slice of panorama you're facing).
    ctx.globalCompositeOperation = "lighter";
    if (this.mw) {
      const ppd = sc * 0.01333, wAz = ((c.az % 360) + 360) % 360, tileW = 360 * ppd, x0 = cx - (wAz / 360) * tileW;
      for (let dup = -1; dup <= 1; dup++) ctx.drawImage(this.mw, x0 + dup * tileW, cy - (75 - c.alt) * ppd, tileW, 90 * ppd);
    }

    // Background stars: fixed on the dome, far beyond any lesson.
    const glint = this.sprites!.glint;
    const farStep = this.compact ? 2 : 1;
    for (let i = 0; i < this.far.length; i += farStep) {
      const F = this.far[i], fp = this.dir(F.v);
      if (!fp || fp.x < -10 || fp.x > w + 10 || fp.y < -10 || fp.y > h + 10) continue;
      const fs = (1.25 + F.m * 6) * u;
      ctx.globalAlpha = 0.25 + F.m * 0.7;
      ctx.drawImage(this.starColor(F.t), fp.x - fs / 2, fp.y - fs / 2, fs, fs);
      if (F.m > 0.75) {
        const L = (4.5 + F.m * 4) * u;
        ctx.globalAlpha = 0.6;
        ctx.drawImage(glint, fp.x - L, fp.y - L, L * 2, L * 2);
      }
    }

    // A few faint distant galaxies.
    for (const G of this.galaxies) {
      const gp = this.dir(G.v);
      if (!gp) continue;
      ctx.save();
      ctx.translate(gp.x, gp.y);
      ctx.rotate(G.rot);
      ctx.scale(1, G.e);
      ctx.globalAlpha = 0.4;
      const s = G.s * 0.5 * u;
      ctx.drawImage(G.warm ? this.sprites!.gw : this.sprites!.gc, -s, -s, s * 2, s * 2);
      ctx.restore();
    }

    // Star dust around the camera — what makes travel read as travel.
    const dustStep = this.compact ? 2 : 1;
    for (let j = 0; j < this.dust.length; j += dustStep) {
      const D = this.dust[j];
      const wp: Vec3 = [wrap(D.x, c.p[0]), wrap(D.y, c.p[1]), wrap(D.z, c.p[2])];
      const p = this.pt(wp);
      if (!p || p.x < -30 || p.x > w + 30 || p.y < -30 || p.y > h + 30) continue;
      const near = Math.min(1, 10 / p.d), sz = (1 + D.m * 3.5 * (0.3 + near * 2)) * u;
      if (mv && streak > 0) {
        // Faint streaks, only near peak speed.
        const tail = this.pt([wp[0] - mv[0] * 6, wp[1] - mv[1] * 6, wp[2] - mv[2] * 6]);
        if (tail) {
          ctx.globalAlpha = 1;
          ctx.strokeStyle = `rgba(235,240,255,${(0.12 * near * streak).toFixed(3)})`;
          ctx.lineWidth = (0.5 + near * 0.6) * u;
          ctx.beginPath();
          ctx.moveTo(p.x, p.y);
          ctx.lineTo(tail.x, tail.y);
          ctx.stroke();
        }
      }
      ctx.globalAlpha = Math.min(0.9, (0.08 + D.m * 0.55) * (0.2 + near));
      ctx.drawImage(this.starColor(D.t), p.x - sz / 2, p.y - sz / 2, sz, sz);
    }
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
  }

  /** Hills at first light and the dome vignette, over everything else. */
  private paintForeground(ctx: CanvasRenderingContext2D): void {
    const { w, h } = this.view;
    const c = this.cam;
    const Y = (v: number) => (v / 1800) * h;
    const lift = Math.max(-10, Math.min(30, c.alt)) * 3.5;
    const glow = ctx.createLinearGradient(0, Y(1380 + lift), 0, Y(1640 + lift));
    glow.addColorStop(0, "rgba(255,190,140,0)");
    glow.addColorStop(1, "rgba(255,170,120,0.10)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, Y(1300), w, h - Y(1300));
    const ridge = (base: number, bow: number, amp: number, freq: number, row: number, seed: number, drift: number, fill: string) => {
      ctx.fillStyle = fill;
      ctx.beginPath();
      ctx.moveTo(0, h);
      const step = Math.max(8, w / 110);
      for (let x = 0; x <= w + step; x += step) {
        const k = (x - w / 2) / (w / 2);
        ctx.lineTo(x, Y(base + lift + k * k * bow - amp * fbm(((x / w) * 2200) / freq + c.az / drift, row, seed, 4)));
      }
      ctx.lineTo(w, h);
      ctx.closePath();
      ctx.fill();
    };
    ridge(1600, 150, 40, 260, 0.5, 9, 30, "#05070C");
    ridge(1680, 120, 28, 180, 1.5, 19, 22, "#030408");
    const diag = Math.hypot(w, h);
    const vg = ctx.createRadialGradient(w / 2, h * 0.53, diag * 0.3, w / 2, h * 0.53, diag * 0.565);
    vg.addColorStop(0, "rgba(3,4,8,0)");
    vg.addColorStop(1, "rgba(3,4,8,.9)");
    ctx.fillStyle = vg;
    ctx.fillRect(0, 0, w, h);
  }

  private paintLessons(ctx: CanvasRenderingContext2D, t: number): void {
    this.picks = [];
    const model = this.model;
    if (!model) return;
    const { w, h, u } = this.view;
    const matches = this.matches;
    const lit = (id: number) => !matches || matches.has(id);
    const tw = (n: number) => (this.reduced ? 1 : 0.86 + 0.14 * Math.sin(t * 1.1 + n * 2.3));

    // Constellation figures.
    ctx.globalCompositeOperation = "source-over";
    ctx.lineCap = "round";
    model.clusters.forEach((cl, ci) => {
      for (const [ia, ib] of cl.links) {
        const A = cl.lessons[ia], B = cl.lessons[ib];
        const a = this.pt(A.pos), b = this.pt(B.pos);
        if (!a || !b) continue;
        const hz = Math.max(0.06, Math.min(1, 16 / a.d));
        let base = ci === this.selCluster ? 0.5 : 0.18;
        if (matches) base = lit(A.id) && lit(B.id) ? 0.45 : 0.04;
        ctx.globalAlpha = 1;
        ctx.strokeStyle = `rgba(236,240,255,${(base * hz).toFixed(3)})`;
        ctx.lineWidth = (0.4 + 0.35 * hz) * u;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    });

    // Lesson stars.
    ctx.globalCompositeOperation = "lighter";
    let n = 0;
    let sel: Screen | null = null;
    model.clusters.forEach((cl, ci) => {
      const spr = this.tint(cl.tint);
      for (const L of cl.lessons) {
        n++;
        const ps = this.pt(L.pos);
        if (!ps || ps.x < -80 || ps.x > w + 80 || ps.y < -80 || ps.y > h + 80) continue;
        const near = Math.max(0.1, Math.min(1, 16 / ps.d));
        const ss = Math.min(70, (L.size * 700) / ps.d + 3) * u;
        const al = (matches && !lit(L.id) ? 0.08 : 1) * (0.3 + 0.7 * near) * tw(n);
        ctx.globalAlpha = al;
        ctx.drawImage(spr, ps.x - ss / 2, ps.y - ss / 2, ss, ss);
        const isSel = ci === this.selCluster && L.id === this.selLesson;
        if (isSel) sel = ps;
        if (near > 0.5 && (isSel || (matches && lit(L.id)))) {
          const Lg = ss * 0.55;
          ctx.drawImage(this.sprites!.glint, ps.x - Lg, ps.y - Lg, Lg * 2, Lg * 2);
        }
        this.picks.push({ id: L.id, cluster: ci, x: ps.x, y: ps.y });
      }
    });
    ctx.globalCompositeOperation = "source-over";

    // Selection ring — the one violet accent.
    if (sel) {
      const s = sel as Screen;
      const r = (14 + (this.reduced ? 0 : 1.5 * Math.sin(t * 1.6))) * u;
      ctx.globalAlpha = 0.75;
      ctx.strokeStyle = `rgba(${RING},0.85)`;
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.arc(s.x, s.y, r, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Constellation names: nearest first, never piled on each other.
    const labels: { x: number; y: number; size: number; alpha: number; name: string; on: boolean }[] = [];
    model.clusters.forEach((cl, ci) => {
      const drop = clusterRadius(cl.lessons.length) * 0.75 + 2.5;
      const lp = this.pt([cl.pos[0], cl.pos[1] - drop, cl.pos[2]]);
      if (!lp || lp.x < -200 || lp.x > w + 200 || lp.y < 0 || lp.y > h) return;
      const on = ci === this.selCluster;
      const hz = Math.max(0.12, Math.min(1, 20 / lp.d));
      const fade = Number.isFinite(this.labelFloor) ? 1 - smooth(this.labelFloor - 70, this.labelFloor - 16, lp.y) : 1;
      let alpha = (on ? 0.92 : 0.5) * hz * fade;
      if (matches && !on && !cl.lessons.some((l) => matches.has(l.id))) alpha *= 0.35;
      if (alpha < 0.07) return;
      labels.push({ x: lp.x, y: lp.y, size: Math.max(12, Math.min(30, 430 / lp.d)) * u, alpha, name: cl.name, on });
    });
    labels.sort((a, b) => Number(b.on) - Number(a.on) || b.size - a.size);
    const placed: { x0: number; x1: number; y0: number; y1: number }[] = [];
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    for (const L of labels) {
      ctx.font = `italic 400 ${Math.round(L.size)}px ${this.font}`;
      const tw2 = ctx.measureText(L.name).width / 2;
      const box = { x0: L.x - tw2 - 6, x1: L.x + tw2 + 6, y0: L.y - L.size, y1: L.y + L.size * 0.3 };
      if (!L.on && (box.x0 < 4 || box.x1 > w - 4)) continue;
      if (placed.some((p) => p.x0 < box.x1 && box.x0 < p.x1 && p.y0 < box.y1 && box.y0 < p.y1)) continue;
      placed.push(box);
      ctx.globalAlpha = 1;
      ctx.fillStyle = `rgba(236,240,255,${L.alpha.toFixed(3)})`;
      ctx.fillText(L.name, L.x, L.y);
    }
    ctx.globalAlpha = 1;
  }
}
