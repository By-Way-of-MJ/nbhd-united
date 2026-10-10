/**
 * Constellation night sky — the canvas painter (ported from the approved
 * WebLessonsDeep mockup). One dome: a procedural Milky Way with dust lanes,
 * thousands of soft background stars in real star colours, a few faint distant
 * galaxies, drifting star dust for depth, the user's lessons as constellations,
 * and hills at first light. Stereographic projection.
 *
 * While the camera rests, everything that doesn't change (sky, background
 * stars, dust, hills) is cached offscreen and only the lessons, figures and
 * names redraw; in flight, every layer paints straight onto the canvas. The
 * dome vignette is a CSS overlay (static per size), not part of the canvas.
 *
 * Level of detail keeps a big sky calm: only the chosen constellation and its
 * nearest neighbours in the Next order are drawn fully (every star, the figure,
 * the name); the rest recede into depth haze as a soft glow with their few
 * brightest stars, and at most a handful of names are on screen at once.
 */

import { ctx2d, fbm, makeCanvas, seeded, smooth } from "../sky-art/noise";
import {
  angDiff,
  brightestStars,
  clusterRadius,
  D2R,
  detailTarget,
  dirv,
  easeInOut,
  easeSpeed,
  FAR_STARS,
  fogFor,
  type LabelCandidate,
  lerpPose,
  LOOK_BELOW,
  MAX_LABELS,
  moveDuration,
  pickLabels,
  type Pose,
  type SkyModel,
  type Vec3,
} from "./model";

const BOX = 90;
const SKY_BG = "#030408";
const RING = "196,187,255";
/** Top of the hills layer on the 1800-unit design height (the glow above the ridges starts here). */
const FG_TOP = 1300;
const STREAK_BANDS = 5;

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

/** Soft haze for a distant constellation. */
function glowSprite(rgb: string): HTMLCanvasElement {
  const c = makeCanvas(64, 64), g = ctx2d(c), gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  gr.addColorStop(0, `rgba(${rgb},0.55)`);
  gr.addColorStop(0.35, `rgba(${rgb},0.18)`);
  gr.addColorStop(1, `rgba(${rgb},0)`);
  g.fillStyle = gr;
  g.fillRect(0, 0, 64, 64);
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
  /** Per constellation: eased level of detail (0 distant glow → 1 full), and "arrived" (0 → 1). */
  private detail = new Float32Array(0);
  private arrived = new Float32Array(0);
  /** Per constellation: its few brightest stars, and which stars are featured before arrival. */
  private farIdx: number[][] = [];
  private featured: Uint8Array[] = [];
  private matchCl = new Uint8Array(0);
  private lastT = 0;
  private rot = { ca: 1, sa: 0, ce: 1, se: 0 };
  private readonly sp: Screen = { x: 0, y: 0, d: 0 };
  private readonly sp2: Screen = { x: 0, y: 0, d: 0 };
  private cam: Pose = { p: [0, 0, 0], az: 0, alt: 18 };
  private move: Move | null = null;
  private fade: { snap: HTMLCanvasElement; start: number } | null = null;
  private lastPose: Pose = { p: [0, 0, 0], az: 0, alt: 18 };
  private needsDraw = true;

  private far: Far[] = [];
  private dust: Dust[] = [];
  private galaxies: Galaxy[] = [];
  private sprites: { white: HTMLCanvasElement; blue: HTMLCanvasElement; warm: HTMLCanvasElement; glint: HTMLCanvasElement; gw: HTMLCanvasElement; gc: HTMLCanvasElement; tints: Map<string, HTMLCanvasElement>; glows: Map<string, HTMLCanvasElement> } | null = null;

  private mw: HTMLCanvasElement | null = null;
  /** Optional DOM layer behind the canvas that shows the Milky Way, moved by CSS transform (composited, not repainted). */
  private dome: HTMLCanvasElement | null = null;
  private domeTransform = "";
  private mwImg: ImageData | null = null;
  private mwRow = 0;
  private readonly mwW = 720;
  private readonly mwH = 208;

  private bg: HTMLCanvasElement | null = null;
  private fg: HTMLCanvasElement | null = null;
  private cacheKey = "";
  private font = "Georgia, serif";
  /** Clickable stars from the last frame: a reused pool, `pickCount` of them live. */
  private picks: { id: number; cluster: number; x: number; y: number }[] = [];
  private pickCount = 0;
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
    const n = model.clusters.length;
    this.farIdx = model.clusters.map((c) => brightestStars(c.lessons.map((l) => l.size), FAR_STARS));
    this.featured = model.clusters.map((c) => {
      const f = new Uint8Array(c.lessons.length);
      for (const i of c.featured) f[i] = 1;
      return f;
    });
    // Start settled at the current selection (no fade on first paint).
    this.detail = new Float32Array(n);
    this.arrived = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      this.detail[i] = detailTarget(i, this.selCluster);
      this.arrived[i] = i === this.selCluster ? 1 : 0;
    }
    this.syncMatchClusters();
    this.needsDraw = true;
  }

  setSelection(cluster: number, lessonId: number): void {
    const first = this.selCluster < 0;
    this.selCluster = cluster;
    this.selLesson = lessonId;
    if (first) {
      for (let i = 0; i < this.detail.length; i++) {
        this.detail[i] = detailTarget(i, cluster);
        this.arrived[i] = i === cluster ? 1 : 0;
      }
    }
    this.needsDraw = true;
  }

  setMatches(ids: Set<number> | null): void {
    this.matches = ids;
    this.syncMatchClusters();
    this.needsDraw = true;
  }

  private syncMatchClusters(): void {
    const m = this.model;
    this.matchCl = new Uint8Array(m ? m.clusters.length : 0);
    const ids = this.matches;
    if (!m || !ids) return;
    m.clusters.forEach((c, i) => {
      if (c.lessons.some((l) => ids.has(l.id))) this.matchCl[i] = 1;
    });
  }

  /**
   * Show the Milky Way on a canvas element that sits behind the sky canvas.
   * The panorama is drawn into it once and then only moved and scaled with a
   * CSS transform, so the browser composites it instead of the sky canvas
   * repainting a full-screen image every frame in flight.
   */
  attachDome(el: HTMLCanvasElement | null): void {
    this.dome = el;
    this.domeTransform = "";
    this.cacheKey = "";
    this.needsDraw = true;
    if (el && this.mw) this.fillDome();
  }

  private fillDome(): void {
    const el = this.dome, mw = this.mw;
    if (!el || !mw) return;
    // Three copies side by side so any heading is covered without seams.
    el.width = mw.width * 3;
    el.height = mw.height;
    // One CSS pixel per panorama pixel; the transform does all the scaling.
    el.style.width = `${el.width}px`;
    el.style.height = `${el.height}px`;
    el.style.maxWidth = "none";
    el.style.transformOrigin = "0 0";
    el.style.willChange = "transform";
    const g = ctx2d(el);
    for (let i = 0; i < 3; i++) g.drawImage(mw, i * mw.width, 0);
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
    for (let i = 0; i < this.pickCount; i++) {
      const p = this.picks[i];
      const d = (p.x - x) * (p.x - x) + (p.y - y) * (p.y - y);
      if (d < bd) {
        bd = d;
        best = { id: p.id, cluster: p.cluster };
      }
    }
    return best;
  }

  // ── Projection ───────────────────────────────────────────────────────────

  /** Project a unit direction into `out`; false if it's behind the camera. Uses the per-frame rotation cache. */
  private projDir(nx: number, ny: number, nz: number, out: Screen): boolean {
    const { ca, sa, ce, se } = this.rot;
    const x1 = nx * ca - nz * sa, z1 = nx * sa + nz * ca;
    const y2 = ny * ce - z1 * se, z2 = ny * se + z1 * ce;
    if (z2 < -0.35) return false;
    const k = 1 / (1 + z2), { sc, cx, cy } = this.view;
    out.x = cx + x1 * k * sc;
    out.y = cy - y2 * k * sc;
    return true;
  }

  /** Project a world point into `out` (with its distance from the camera); false if behind. */
  private proj(wx: number, wy: number, wz: number, out: Screen): boolean {
    const c = this.cam.p, dx = wx - c[0], dy = wy - c[1], dz = wz - c[2];
    const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1e-6;
    out.d = d;
    return this.projDir(dx / d, dy / d, dz / d, out);
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
    this.rot = { ca: Math.cos(this.cam.az * D2R), sa: Math.sin(this.cam.az * D2R), ce: Math.cos(this.cam.alt * D2R), se: Math.sin(this.cam.alt * D2R) };
    const mv: Vec3 = [this.cam.p[0] - this.lastPose.p[0], this.cam.p[1] - this.lastPose.p[1], this.cam.p[2] - this.lastPose.p[2]];
    this.lastPose = { p: [...this.cam.p] as Vec3, az: this.cam.az, alt: this.cam.alt };

    const W = Math.round(w * dpr), H = Math.round(h * dpr);
    // The hills only ever reach the bottom ~28% of the view, so their layer is just that band.
    const fgTop = Math.floor(((FG_TOP * h) / 1800) * dpr);
    const key = `${W}x${H}:${this.cam.p.map((v) => v.toFixed(3)).join(",")}:${this.cam.az.toFixed(3)}:${this.cam.alt.toFixed(3)}:${this.mw ? 1 : 0}:${this.compact ? 1 : 0}`;
    const streaks = !this.reduced && peak > 0.72;
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    if (this.move || streaks) {
      // In flight every layer changes each frame, so paint straight onto the
      // canvas — caching would only add two full-screen copies per frame.
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.paintBackground(ctx, streaks ? mv : null, streaks ? smooth(0.72, 1, peak) : 0);
      this.paintLessons(ctx, now / 1000);
      ctx.globalCompositeOperation = "source-over";
      this.paintForeground(ctx);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalAlpha = 1;
      this.cacheKey = "";
    } else {
      if (key !== this.cacheKey) {
        if (!this.bg || this.bg.width !== W || this.bg.height !== H) {
          this.bg = makeCanvas(W, H);
          this.fg = makeCanvas(W, Math.max(1, H - fgTop));
        }
        const b = ctx2d(this.bg);
        b.setTransform(dpr, 0, 0, dpr, 0, 0);
        this.paintBackground(b, null, 0);
        const f = ctx2d(this.fg!);
        f.setTransform(1, 0, 0, 1, 0, 0);
        f.clearRect(0, 0, W, H - fgTop);
        f.setTransform(dpr, 0, 0, dpr, 0, -fgTop);
        this.paintForeground(f);
        this.cacheKey = key;
      }
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      // "copy" replaces last frame outright (the cached layer may be transparent over the dome).
      ctx.globalCompositeOperation = "copy";
      ctx.drawImage(this.bg!, 0, 0);
      ctx.globalCompositeOperation = "source-over";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.paintLessons(ctx, now / 1000);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      ctx.drawImage(this.fg!, 0, fgTop);
    }

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
      glows: new Map(),
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

  private glow(rgb: string): HTMLCanvasElement {
    const m = this.sprites!.glows;
    let s = m.get(rgb);
    if (!s) {
      s = glowSprite(rgb);
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
        // Glow on top of the sky colour (SKY_BG = 3,4,8), baked in so the panorama can be copied opaque.
        d[o] = clamp255((150 + 105 * warm) * I * 0.5 + 2 + 3);
        d[o + 1] = clamp255((166 + 70 * warm) * I * 0.5 + 3 + 4);
        d[o + 2] = clamp255((222 - 30 * warm) * I * 0.5 + 6 + 8);
        d[o + 3] = 255;
      }
    }
    this.mwRow = end;
    if (end >= H) {
      const c = makeCanvas(W, H);
      ctx2d(c).putImageData(this.mwImg, 0, 0);
      this.mw = c;
      this.mwImg = null;
      this.fillDome();
    }
  }

  private paintBackground(ctx: CanvasRenderingContext2D, mv: Vec3 | null, streak: number): void {
    const { w, h, sc, cx, cy, u } = this.view;
    const c = this.cam;
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = SKY_BG;

    // Milky Way across the dome (the slice of panorama you're facing). The
    // panorama already carries the sky colour, so it's a plain opaque copy —
    // no full-screen fill and blend underneath — with the sky filled only
    // above and below it.
    if (this.dome && this.mw) {
      // The Milky Way is its own composited layer behind this canvas.
      ctx.clearRect(0, 0, w, h);
      const ppd = sc * 0.01333, wAz = ((c.az % 360) + 360) % 360, tileW = 360 * ppd, x0 = cx - (wAz / 360) * tileW;
      const tf = `translate(${(x0 - tileW).toFixed(2)}px,${(cy - (75 - c.alt) * ppd).toFixed(2)}px) scale(${(tileW / this.mw.width).toFixed(5)},${((90 * ppd) / this.mw.height).toFixed(5)})`;
      if (tf !== this.domeTransform) {
        this.dome.style.transform = tf;
        this.domeTransform = tf;
      }
    } else if (this.mw) {
      const ppd = sc * 0.01333, wAz = ((c.az % 360) + 360) % 360, tileW = 360 * ppd, x0 = cx - (wAz / 360) * tileW;
      const y0 = cy - (75 - c.alt) * ppd, y1 = y0 + 90 * ppd;
      for (let dup = -1; dup <= 1; dup++) {
        const x = x0 + dup * tileW;
        if (x < w && x + tileW > 0) ctx.drawImage(this.mw, x, y0, tileW, 90 * ppd);
      }
      if (y0 > 0) ctx.fillRect(0, 0, w, Math.ceil(y0) + 1);
      if (y1 < h) ctx.fillRect(0, Math.floor(y1) - 1, w, h - y1 + 2);
    } else ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = "lighter";

    // Background stars: fixed on the dome, far beyond any lesson.
    const glint = this.sprites!.glint;
    const farStep = this.compact ? 2 : 1;
    const fp = this.sp;
    for (let i = 0; i < this.far.length; i += farStep) {
      const F = this.far[i];
      if (!this.projDir(F.v[0], F.v[1], F.v[2], fp) || fp.x < -10 || fp.x > w + 10 || fp.y < -10 || fp.y > h + 10) continue;
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
      const gp = this.sp;
      if (!this.projDir(G.v[0], G.v[1], G.v[2], gp)) continue;
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
    // Streaks are batched into a few strokes by nearness (thousands of single strokes stall a frame).
    const trails = mv && streak > 0 ? Array.from({ length: STREAK_BANDS }, () => new Path2D()) : null;
    for (let j = 0; j < this.dust.length; j += dustStep) {
      const D = this.dust[j];
      const wx = wrap(D.x, c.p[0]), wy = wrap(D.y, c.p[1]), wz = wrap(D.z, c.p[2]);
      const p = this.sp;
      if (!this.proj(wx, wy, wz, p) || p.x < -30 || p.x > w + 30 || p.y < -30 || p.y > h + 30) continue;
      const near = Math.min(1, 10 / p.d), sz = (1 + D.m * 3.5 * (0.3 + near * 2)) * u;
      if (trails && mv) {
        // Faint streaks, only near peak speed.
        const tail = this.sp2;
        if (this.proj(wx - mv[0] * 6, wy - mv[1] * 6, wz - mv[2] * 6, tail)) {
          const band = trails[Math.min(STREAK_BANDS - 1, Math.floor(near * STREAK_BANDS))];
          band.moveTo(p.x, p.y);
          band.lineTo(tail.x, tail.y);
        }
      }
      ctx.globalAlpha = Math.min(0.9, (0.08 + D.m * 0.55) * (0.2 + near));
      ctx.drawImage(this.starColor(D.t), p.x - sz / 2, p.y - sz / 2, sz, sz);
    }
    if (trails) {
      ctx.strokeStyle = "rgb(235,240,255)";
      trails.forEach((path, b) => {
        const near = (b + 0.5) / STREAK_BANDS;
        ctx.globalAlpha = 0.12 * near * streak;
        ctx.lineWidth = (0.5 + near * 0.6) * u;
        ctx.stroke(path);
      });
    }
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
  }

  /** Hills at first light, over everything else (the dome vignette is a CSS overlay above the canvas). */
  private paintForeground(ctx: CanvasRenderingContext2D): void {
    const { w, h } = this.view;
    const c = this.cam;
    const Y = (v: number) => (v / 1800) * h;
    const lift = Math.max(-10, Math.min(30, c.alt)) * 3.5;
    const glow = ctx.createLinearGradient(0, Y(1380 + lift), 0, Y(1640 + lift));
    glow.addColorStop(0, "rgba(255,190,140,0)");
    glow.addColorStop(1, "rgba(255,170,120,0.10)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, Y(FG_TOP), w, h - Y(FG_TOP));
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
  }

  private addPick(id: number, cluster: number, x: number, y: number): void {
    const p = this.picks[this.pickCount];
    if (p) {
      p.id = id;
      p.cluster = cluster;
      p.x = x;
      p.y = y;
    } else this.picks.push({ id, cluster, x, y });
    this.pickCount++;
  }

  private paintLessons(ctx: CanvasRenderingContext2D, t: number): void {
    this.pickCount = 0;
    const model = this.model;
    if (!model) return;
    const { w, h, u, sc } = this.view;
    const matches = this.matches;
    const selC = this.selCluster;
    const S = this.sp, S2 = this.sp2;
    const cam = this.cam.p;
    const glint = this.sprites!.glint;
    const reduced = this.reduced;

    // Ease each constellation's level of detail toward its target (~0.7 s).
    const dt = this.lastT ? Math.min(0.1, Math.max(0, t - this.lastT)) : 1;
    this.lastT = t;
    const k = reduced ? 1 : 1 - Math.exp(-dt * 4);
    let still = true;

    ctx.lineCap = "round";
    ctx.strokeStyle = "rgb(236,240,255)";
    const labels: LabelCandidate[] = [];
    let selX = 0, selY = 0, hasSel = false;
    let n = 0;

    for (let ci = 0; ci < model.clusters.length; ci++) {
      const cl = model.clusters[ci];
      const det = (this.detail[ci] += (detailTarget(ci, selC) - this.detail[ci]) * k);
      const arr = (this.arrived[ci] += ((ci === selC ? 1 : 0) - this.arrived[ci]) * k);
      if (Math.abs(det - detailTarget(ci, selC)) > 0.004 || Math.abs(arr - (ci === selC ? 1 : 0)) > 0.004) still = false;
      n += cl.lessons.length;

      // Cull the whole constellation early when it's behind the camera or well off screen.
      const R = clusterRadius(cl.lessons.length);
      const onDome = this.proj(cl.pos[0], cl.pos[1], cl.pos[2], S);
      const camDist = S.d;
      if (ci !== selC) {
        if (!onDome) {
          if (camDist > R * 1.6) continue;
        } else {
          const pr = (R / Math.max(1, camDist)) * sc * 0.7 + 60;
          if (S.x < -pr || S.x > w + pr || S.y < -pr || S.y > h + pr) continue;
        }
      }
      const cx = S.x, cy = S.y;
      const fog = fogFor(camDist);
      const hasMatch = !!matches && this.matchCl[ci] === 1;
      const far = 1 - det;
      const spr = this.tint(cl.tint);

      // Distant: a soft haze and its few brightest stars.
      if (far > 0.01 && onDome) {
        const gs = Math.min(150, Math.max(10, (R * 1.3 * sc) / camDist)) * u;
        ctx.globalCompositeOperation = "lighter";
        ctx.globalAlpha = 0.13 * fog * far * (matches && !hasMatch ? 0.4 : 1);
        ctx.drawImage(this.glow(cl.tint), cx - gs / 2, cy - gs / 2, gs, gs);
        for (const li of this.farIdx[ci]) {
          const L = cl.lessons[li];
          if (!this.proj(L.pos[0], L.pos[1], L.pos[2], S2)) continue;
          const ss = Math.min(14, (L.size * 520) / S2.d + 2.2) * u;
          const lit = !matches || matches.has(L.id);
          ctx.globalAlpha = far * (0.3 + 0.55 * fog) * (lit ? 1 : 0.25) * (reduced ? 1 : 0.86 + 0.14 * Math.sin(t * 1.1 + (n + li) * 2.3));
          ctx.drawImage(spr, S2.x - ss / 2, S2.y - ss / 2, ss, ss);
          if (det < 0.5) this.addPick(L.id, ci, S2.x, S2.y);
        }
        // Search matches shine through the haze.
        if (hasMatch && matches) {
          for (const L of cl.lessons) {
            if (!matches.has(L.id) || !this.proj(L.pos[0], L.pos[1], L.pos[2], S2)) continue;
            const ss = Math.min(16, (L.size * 600) / S2.d + 3) * u;
            ctx.globalAlpha = far * (0.55 + 0.45 * fog);
            ctx.drawImage(spr, S2.x - ss / 2, S2.y - ss / 2, ss, ss);
            ctx.globalAlpha = far * 0.7;
            ctx.drawImage(glint, S2.x - ss * 0.6, S2.y - ss * 0.6, ss * 1.2, ss * 1.2);
            if (det < 0.5) this.addPick(L.id, ci, S2.x, S2.y);
          }
        }
      }

      // Near: every figure line and star. Big constellations show their
      // brightest stars fully and the rest as faint points until you arrive.
      if (det > 0.01) {
        const isSelC = ci === selC;
        const links = arr > 0.5 ? cl.links : cl.featuredLinks;
        const feat = this.featured[ci];
        if (links.length) {
          ctx.globalCompositeOperation = "source-over";
          const hz = Math.max(0.06, Math.min(1, 16 / camDist));
          const base = (isSelC ? 0.5 : 0.2) * det * hz;
          ctx.lineWidth = (0.4 + 0.35 * hz) * u;
          // Lit (or no search) lines in one stroke, dimmed ones in another.
          for (let pass = 0; pass < (matches ? 2 : 1); pass++) {
            ctx.beginPath();
            let any = false;
            for (const [ia, ib] of links) {
              const A = cl.lessons[ia], B = cl.lessons[ib];
              if (matches && (matches.has(A.id) && matches.has(B.id)) !== (pass === 0)) continue;
              if (!this.proj(A.pos[0], A.pos[1], A.pos[2], S) || !this.proj(B.pos[0], B.pos[1], B.pos[2], S2)) continue;
              ctx.moveTo(S.x, S.y);
              ctx.lineTo(S2.x, S2.y);
              any = true;
            }
            if (!any) continue;
            ctx.globalAlpha = matches ? (pass === 0 ? 0.45 * det * hz : 0.04 * det) : base;
            ctx.stroke();
          }
        }
        ctx.globalCompositeOperation = "lighter";
        for (let li = 0; li < cl.lessons.length; li++) {
          const L = cl.lessons[li];
          if (!this.proj(L.pos[0], L.pos[1], L.pos[2], S) || S.x < -80 || S.x > w + 80 || S.y < -80 || S.y > h + 80) continue;
          const lit = !matches || matches.has(L.id);
          const isSel = isSelC && L.id === this.selLesson;
          const shown = feat[li] || isSel || (matches && lit) ? 1 : arr;
          const near = Math.max(0.1, Math.min(1, 16 / S.d));
          const full = Math.min(70, (L.size * 700) / S.d + 3) * u;
          const ss = shown >= 1 ? full : 2.6 * u + (full - 2.6 * u) * shown;
          const tw = reduced ? 1 : 0.86 + 0.14 * Math.sin(t * 1.1 + (n + li) * 2.3);
          ctx.globalAlpha = det * (lit ? 1 : 0.08) * (0.3 + 0.7 * near) * (0.3 + 0.7 * shown) * tw;
          ctx.drawImage(spr, S.x - ss / 2, S.y - ss / 2, ss, ss);
          if (isSel) {
            hasSel = true;
            selX = S.x;
            selY = S.y;
          }
          if (near > 0.5 && (isSel || (matches && lit))) {
            const Lg = ss * 0.55;
            ctx.drawImage(glint, S.x - Lg, S.y - Lg, Lg * 2, Lg * 2);
          }
          if (det >= 0.5) this.addPick(L.id, ci, S.x, S.y);
        }
      }
      if (det > 0.3 || hasMatch || ci === selC) labels.push({ index: ci, camDist, match: hasMatch });
    }
    if (!still) this.needsDraw = true;
    ctx.globalCompositeOperation = "source-over";

    // Selection ring — the one violet accent.
    if (hasSel) {
      const r = (14 + (reduced ? 0 : 1.5 * Math.sin(t * 1.6))) * u;
      ctx.globalAlpha = 0.75;
      ctx.strokeStyle = `rgba(${RING},0.85)`;
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.arc(selX, selY, r, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Constellation names: the chosen one, then search matches, then the
    // nearest — at most a handful, never piled on each other.
    const placed: { x0: number; x1: number; y0: number; y1: number }[] = [];
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    for (const ci of pickLabels(labels, selC, labels.length)) {
      if (placed.length >= MAX_LABELS) break;
      const cl = model.clusters[ci];
      const drop = clusterRadius(cl.lessons.length) * 0.75 + 2.5;
      if (!this.proj(cl.pos[0], cl.pos[1] - drop, cl.pos[2], S) || S.x < -200 || S.x > w + 200 || S.y < 0 || S.y > h) continue;
      const on = ci === selC;
      const hz = Math.max(0.12, Math.min(1, 20 / S.d));
      const fade = Number.isFinite(this.labelFloor) ? 1 - smooth(this.labelFloor - 70, this.labelFloor - 16, S.y) : 1;
      let alpha = (on ? 0.92 : 0.5 * Math.max(this.detail[ci], 0.6)) * hz * fade;
      if (matches && !on && this.matchCl[ci] !== 1) alpha *= 0.35;
      if (alpha < 0.07) continue;
      const size = Math.max(12, Math.min(30, 430 / S.d)) * u;
      ctx.font = `italic 400 ${Math.round(size)}px ${this.font}`;
      const tw2 = ctx.measureText(cl.name).width / 2;
      const box = { x0: S.x - tw2 - 6, x1: S.x + tw2 + 6, y0: S.y - size, y1: S.y + size * 0.3 };
      if (!on && (box.x0 < 4 || box.x1 > w - 4)) continue;
      if (placed.some((p) => p.x0 < box.x1 && box.x0 < p.x1 && p.y0 < box.y1 && box.y0 < p.y1)) continue;
      placed.push(box);
      ctx.globalAlpha = alpha;
      ctx.fillStyle = "rgb(236,240,255)";
      ctx.fillText(cl.name, S.x, S.y);
    }
    ctx.globalAlpha = 1;
  }
}
