"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type KeyboardEvent, type MouseEvent, type ReactNode, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import "./galaxy-flight.css";
import { createStarNote, fetchStarNotes } from "@/lib/api";
import type { GalaxyData } from "@/lib/constellation-game/encounter-logic";
import {
  type AutoPath,
  autoPose,
  buildAutoPath,
  type Cam,
  type View,
  focalFor,
  goalFor,
  isActivationKeyOnControl,
  isTypingTarget,
  keyAction,
  pickStar,
  STEER_ACTIONS,
  type KeyAction,
} from "@/lib/galaxy-flight/camera";
import { connectedIndices, layoutGalaxy } from "@/lib/galaxy-flight/layout";

import { FlightRenderer, type Rect } from "./renderer";

/**
 * Chart your galaxy — the Open Sky flight through your lessons, matching the
 * iPhone's "Explore in flight". Plain canvas 2D + requestAnimationFrame (no
 * Phaser). Every star is a real lesson; click one to autopilot there, Land to
 * read it, leave a note, follow its real connections.
 *
 * Calm exploration only: no adversary, no fail states, nothing to score.
 */

type Phase = { kind: "flying" } | { kind: "landing"; idx: number } | { kind: "landed"; idx: number };

interface Sim {
  cam: Cam;
  vx: number;
  vy: number;
  vz: number;
  target: number | null;
  /** The curve the autopilot (or a landing approach) is flying, if any. */
  auto: AutoPath | null;
  phase: Phase;
  keys: Set<KeyAction>;
  view: View;
  last: number;
  font: string;
  /** Smoothed 0..1 "going fast" factor that drives the dust streaks. */
  streak: number;
}

/** Idle cruise: a steady push forward into the star field (world units / s). */
const DRIFT_VZ = 240;
const MAX_VZ = 720;
const MIN_VZ = -200;
const THROTTLE = 420;
const LATERAL = 300;
const VERTICAL = 220;
/** Speed above which the dust starts to streak, and where it streaks fully. */
const STREAK_FROM = DRIFT_VZ + 120;
const STREAK_FULL = 1800;

function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (cb) => {
      const m = window.matchMedia(query);
      m.addEventListener("change", cb);
      return () => m.removeEventListener("change", cb);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

function sourceLine(sourceType: string | undefined, createdAt: string): string | null {
  const when = (() => {
    const t = Date.parse(createdAt);
    return Number.isNaN(t) ? "" : new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  })();
  const from = sourceType === "journal" ? "From your journal" : sourceType === "fuel" ? "From Fuel" : sourceType === "chat" || sourceType === "conversation" ? "From a conversation" : sourceType ? `From ${sourceType}` : "";
  if (!from && !when) return null;
  return [from, when].filter(Boolean).join(" · ");
}

function Eyebrow({ children, style }: { children: ReactNode; style?: React.CSSProperties }) {
  return (
    <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-os-label" style={style}>
      {children}
    </span>
  );
}

/** Ghost circle with a word under it (the iPhone control language). */
function FlightButton({ label, icon, onClick, accent, dim, shortcut }: { label: string; icon: ReactNode; onClick: () => void; accent?: boolean; dim?: boolean; shortcut?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={shortcut ? `${label} (${shortcut})` : label}
      aria-keyshortcuts={shortcut}
      className="group os-focus inline-flex flex-col items-center gap-2 rounded-2xl transition-opacity"
      style={{ opacity: dim ? 0.45 : 1 }}
    >
      <span
        aria-hidden="true"
        className={`flex h-14 w-14 items-center justify-center rounded-full border transition group-hover:border-os-accent-line group-hover:text-os-accent ${accent ? "border-os-accent-line text-os-accent" : "border-os-ring text-white"}`}
      >
        {icon}
      </span>
      <span className="text-[12px] text-os-muted">{label}</span>
    </button>
  );
}

export function GalaxyFlight({ galaxy }: { galaxy: GalaxyData }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const laid = useMemo(() => layoutGalaxy(galaxy), [galaxy]);
  const renderer = useMemo(() => new FlightRenderer(laid), [laid]);
  const reduced = useMedia("(prefers-reduced-motion: reduce)");
  const coarse = useMedia("(pointer: coarse)");
  const wide = useMedia("(min-width: 768px)");

  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const mapRef = useRef<HTMLCanvasElement>(null);
  const noteRef = useRef<HTMLInputElement>(null);
  // UI blocks the star labels must stay clear of (measured each frame — four rects, cheap).
  const headerRef = useRef<HTMLDivElement>(null);
  const controlsRef = useRef<HTMLDivElement>(null);
  const mapBoxRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const sim = useRef<Sim>({
    cam: { x: 0, y: 0, z: 0 },
    vx: 0,
    vy: 0,
    vz: DRIFT_VZ,
    target: null,
    auto: null,
    phase: { kind: "flying" },
    keys: new Set(),
    view: { w: 1, h: 1, focal: 1 },
    last: 0,
    font: "sans-serif",
    streak: 0,
  });
  const reducedRef = useRef(reduced);
  useEffect(() => {
    reducedRef.current = reduced;
    if (reduced) sim.current.vz = 0;
  }, [reduced]);

  const [phase, setPhase] = useState<Phase>({ kind: "flying" });
  const [target, setTarget] = useState<number | null>(null);
  const [near, setNear] = useState<number | null>(null);
  const [mapOpen, setMapOpen] = useState(true);
  const [noteText, setNoteText] = useState("");

  // ── flight commands ────────────────────────────────────────────────────
  const flyTo = useCallback((idx: number) => {
    const s = sim.current;
    const goal = goalFor(laid.stars[idx], s.cam, laid.depth);
    if (reducedRef.current) {
      s.cam = goal;
      s.auto = null;
      s.target = null;
      setTarget(null);
      return;
    }
    // A wide swing past the neighbours, then ease in.
    s.auto = buildAutoPath(s.cam, goal, s.last, { bulge: 0.3, minDur: 1.8, maxDur: 5, seed: idx });
    s.vx = 0;
    s.vy = 0;
    s.target = idx;
    setTarget(idx);
  }, [laid]);

  const stop = useCallback(() => {
    const s = sim.current;
    s.vx = 0;
    s.vy = 0;
    s.vz = 0;
    s.target = null;
    s.auto = null;
    setTarget(null);
  }, []);

  const land = useCallback(() => {
    const s = sim.current;
    if (s.phase.kind !== "flying") return;
    const idx = s.target ?? renderer.nearestIndex;
    if (idx == null) return;
    const goal = goalFor(laid.stars[idx], s.cam, laid.depth);
    s.target = null;
    setTarget(null);
    if (reducedRef.current) {
      s.cam = goal;
      s.auto = null;
      s.phase = { kind: "landed", idx };
      setPhase(s.phase);
      return;
    }
    // Straight in, short, slowing to a stop.
    s.auto = buildAutoPath(s.cam, goal, s.last, { bulge: 0, minDur: 0.8, maxDur: 2, seed: idx });
    s.phase = { kind: "landing", idx };
    setPhase(s.phase);
  }, [laid, renderer]);

  const takeOff = useCallback(() => {
    const s = sim.current;
    s.phase = { kind: "flying" };
    s.cam = { ...s.cam, z: s.cam.z - 200 };
    s.vz = reducedRef.current ? 0 : DRIFT_VZ;
    setPhase(s.phase);
    setNoteText("");
    rootRef.current?.focus();
  }, []);

  const exit = useCallback(() => router.push("/constellation"), [router]);

  // ── canvas sizing + the frame loop ────────────────────────────────────
  useEffect(() => {
    const root = rootRef.current;
    const canvas = canvasRef.current;
    if (!root || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    sim.current.font = getComputedStyle(root).fontFamily || "sans-serif";
    let dpr = 1;
    const resize = () => {
      const r = canvas.getBoundingClientRect();
      dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = w * dpr;
        canvas.height = h * dpr;
      }
      sim.current.view = { w, h, focal: focalFor(w, h) };
    };
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    resize();
    root.focus({ preventScroll: true });

    let raf = 0;
    let shownNear: number | null = null;
    const frame = (ms: number) => {
      const s = sim.current;
      const t = ms / 1000;
      const dt = s.last ? Math.min(0.05, t - s.last) : 0.016;
      s.last = t;
      const still = reducedRef.current;
      const cam = s.cam;
      const prevZ = cam.z;

      if (s.phase.kind === "flying") {
        if (s.auto) {
          const { pose, done } = autoPose(s.auto, t);
          s.cam = pose;
          if (done) s.auto = null;
        } else {
          const K = s.keys;
          const tx = (K.has("left") ? -LATERAL : 0) + (K.has("right") ? LATERAL : 0);
          const ty = (K.has("up") ? -VERTICAL : 0) + (K.has("down") ? VERTICAL : 0);
          s.vx += (tx - s.vx) * Math.min(1, dt * 6);
          s.vy += (ty - s.vy) * Math.min(1, dt * 6);
          if (K.has("faster")) s.vz = Math.min(MAX_VZ, s.vz + THROTTLE * dt);
          if (K.has("slower")) s.vz = Math.max(MIN_VZ, s.vz - THROTTLE * dt);
          cam.x += s.vx * dt;
          cam.y += s.vy * dt;
          cam.z += s.vz * dt;
          if (!still && K.size === 0) cam.x += Math.sin(t / 9) * 6 * dt;
        }
      } else if (s.phase.kind === "landing" && s.auto) {
        const { pose, done } = autoPose(s.auto, t);
        s.cam = pose;
        if (done) {
          s.auto = null;
          s.phase = { kind: "landed", idx: s.phase.idx };
          setPhase(s.phase);
        }
      }
      // Dust streaks follow how fast we are really moving, smoothed so a single
      // jump (take-off nudge, instant autopilot) never flashes a streak.
      const speed = dt > 0 ? Math.abs(s.cam.z - prevZ) / dt : 0;
      const wantStreak = still ? 0 : Math.max(0, Math.min(1, (speed - STREAK_FROM) / (STREAK_FULL - STREAK_FROM)));
      s.streak += (wantStreak - s.streak) * Math.min(1, dt * 5);

      const origin = canvas.getBoundingClientRect();
      const keepOut: Rect[] = [];
      for (const el of [headerRef.current, controlsRef.current, mapBoxRef.current, panelRef.current]) {
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) keepOut.push({ x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height });
      }
      renderer.draw(ctx, dpr, {
        cam: s.cam,
        view: s.view,
        t,
        dt,
        target: s.phase.kind === "flying" ? s.target : null,
        still,
        quiet: s.phase.kind === "landed",
        font: s.font,
        keepOut,
        streak: s.streak,
      });
      if (s.phase.kind === "flying" && renderer.nearestIndex !== shownNear) {
        shownNear = renderer.nearestIndex;
        setNear(shownNear);
      }
      const map = mapRef.current;
      if (map) {
        const mctx = map.getContext("2d");
        const r = map.getBoundingClientRect();
        if (mctx && r.width > 0) {
          const mw = Math.round(r.width), mh = Math.round(r.height);
          if (map.width !== mw * dpr || map.height !== mh * dpr) {
            map.width = mw * dpr;
            map.height = mh * dpr;
          }
          renderer.drawMap(mctx, dpr, mw, mh, s.cam, s.target);
        }
      }
      raf = requestAnimationFrame(frame);
    };
    const start = () => {
      cancelAnimationFrame(raf);
      sim.current.last = 0;
      if (document.visibilityState === "visible") raf = requestAnimationFrame(frame);
    };
    document.addEventListener("visibilitychange", start);
    start();
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      document.removeEventListener("visibilitychange", start);
    };
  }, [renderer]);

  // ── input ──────────────────────────────────────────────────────────────
  const onCanvasClick = (e: MouseEvent<HTMLCanvasElement>) => {
    const s = sim.current;
    if (s.phase.kind !== "flying") return;
    const r = e.currentTarget.getBoundingClientRect();
    const idx = pickStar(laid.stars, s.cam, s.view, laid.depth, e.clientX - r.left, e.clientY - r.top);
    if (idx != null) flyTo(idx);
    rootRef.current?.focus({ preventScroll: true });
  };

  const onMapClick = (e: MouseEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const idx = renderer.pickOnMap(r.width, r.height, e.clientX - r.left, e.clientY - r.top);
    if (idx != null && sim.current.phase.kind === "flying") flyTo(idx);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const el = e.target as HTMLElement;
    if (isTypingTarget(el.tagName, el.isContentEditable) || isActivationKeyOnControl(el.tagName, e.key)) return;
    const a = keyAction(e.key);
    if (!a) return;
    e.preventDefault();
    const s = sim.current;
    if (STEER_ACTIONS.has(a)) {
      if (s.phase.kind !== "flying") return;
      s.keys.add(a);
      if (s.target != null || s.auto) {
        s.target = null;
        s.auto = null;
        setTarget(null);
      }
      return;
    }
    if (a === "stop") {
      if (s.phase.kind === "flying") stop();
    } else if (a === "land") {
      land();
    } else if (a === "map") {
      setMapOpen((v) => !v);
    } else if (a === "exit") {
      if (s.phase.kind === "landed") takeOff();
      else exit();
    }
  };
  const onKeyUp = (e: KeyboardEvent<HTMLDivElement>) => {
    const a = keyAction(e.key);
    if (a) sim.current.keys.delete(a);
  };

  // ── landing panel data ─────────────────────────────────────────────────
  const landedIdx = phase.kind === "landed" ? phase.idx : null;
  const landed = landedIdx != null ? laid.stars[landedIdx] : null;
  const landedId = landed?.star.id ?? null;
  const links = useMemo(() => (landedId == null ? [] : connectedIndices(laid, galaxy.edges, landedId)), [laid, galaxy.edges, landedId]);
  const notes = useQuery({
    queryKey: ["star-notes", landedId],
    queryFn: () => fetchStarNotes(landedId as number),
    enabled: landedId != null,
    staleTime: 60_000,
  });
  const addNote = useMutation({
    mutationFn: (text: string) => createStarNote(landedId as number, text),
    onSuccess: () => {
      setNoteText("");
      void queryClient.invalidateQueries({ queryKey: ["star-notes", landedId] });
    },
  });
  const submitNote = (e: FormEvent) => {
    e.preventDefault();
    const text = noteText.trim();
    if (!text || landedId == null || addNote.isPending) return;
    addNote.mutate(text);
  };

  useEffect(() => {
    if (landedIdx != null && wide) noteRef.current?.focus({ preventScroll: true });
  }, [landedIdx, wide]);

  const flying = phase.kind !== "landed";
  const targetStar = target != null ? laid.stars[target] : null;
  const nearStar = near != null ? laid.stars[near] : null;
  const status =
    phase.kind === "landing"
      ? "Landing…"
      : targetStar
        ? `Flying to “${targetStar.star.text.slice(0, 40)}${targetStar.star.text.length > 40 ? "…" : ""}”`
        : nearStar
          ? `Nearest: ${nearStar.star.text.slice(0, 48)}${nearStar.star.text.length > 48 ? "…" : ""}`
          : "Drifting through your galaxy";
  const canLand = phase.kind === "flying" && (target != null || near != null);
  const clusterOf = (idx: number) => laid.clusters[laid.stars[idx].cluster];

  return (
    <div
      ref={rootRef}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      onBlur={() => sim.current.keys.clear()}
      className="gf-root fixed inset-0 overflow-hidden bg-os-sky text-os-ink outline-none"
      aria-label="Chart your galaxy"
    >
      <canvas
        ref={canvasRef}
        onClick={onCanvasClick}
        role="img"
        aria-label={`Flying through your galaxy of ${laid.stars.length} lessons. Click a star to fly to it.`}
        className="absolute inset-0 h-full w-full"
        style={{ cursor: flying ? "crosshair" : "default", touchAction: "none" }}
      />

      {/* Header */}
      <div ref={headerRef} className="absolute inset-x-4 top-[calc(20px+env(safe-area-inset-top,0px))] z-10 flex items-center justify-between gap-4 md:inset-x-8 md:top-6">
        <div className="flex min-w-0 flex-col gap-1">
          <Eyebrow>Chart your galaxy</Eyebrow>
          <span className="truncate font-serif text-[22px] leading-tight text-white md:text-[28px]">
            {laid.stars.length} {laid.stars.length === 1 ? "lesson" : "lessons"} · {laid.clusters.length} {laid.clusters.length === 1 ? "constellation" : "constellations"}
          </span>
        </div>
        <Link
          href="/constellation"
          className="os-focus inline-flex min-h-[40px] shrink-0 items-center rounded-full border border-os-ring px-[18px] text-[14px] text-os-muted transition hover:border-os-accent-line hover:text-os-accent"
        >
          Exit
        </Link>
      </div>

      {/* Flight controls */}
      {flying ? (
        <div ref={controlsRef} className="absolute inset-x-0 bottom-[calc(24px+env(safe-area-inset-bottom,0px))] flex flex-col items-center gap-3 px-4 md:bottom-[34px] md:gap-[14px]">
          <span className="max-w-full truncate text-[13px] text-os-muted" aria-live="polite">{status}</span>
          <div className="flex gap-[26px]">
            <FlightButton
              label="Land"
              shortcut="E"
              accent
              dim={!canLand}
              onClick={land}
              icon={
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true">
                  <path d="M12 4v11M7 10l5 5 5-5M5 20h14" />
                </svg>
              }
            />
            <FlightButton
              label="Map"
              shortcut="M"
              onClick={() => setMapOpen((v) => !v)}
              icon={
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" aria-hidden="true">
                  <path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2z" />
                  <path d="M9 4v14M15 6v14" />
                </svg>
              }
            />
          </div>
          {!coarse ? (
            <span className="text-[11px] text-os-faint">Click a star to fly there · arrows or WASD to steer · Space to stop</span>
          ) : (
            <span className="text-[11px] text-os-faint">Tap a star to fly there</span>
          )}
        </div>
      ) : null}

      {/* Corner map */}
      {mapOpen && flying ? (
        <div ref={mapBoxRef} className="gf-map">
          <Eyebrow>Map</Eyebrow>
          <canvas
            ref={mapRef}
            onClick={onMapClick}
            role="img"
            aria-label="Map of your galaxy from above: the flight path runs left to right. Click a star to fly to it."
            className="h-[100px] w-full cursor-pointer md:h-[112px]"
          />
        </div>
      ) : null}

      {/* Landing panel */}
      {landed ? (
        <section ref={panelRef} className="gf-panel" aria-label="Landed on a lesson">
          <Eyebrow style={{ color: `rgb(${laid.clusters[landed.cluster].rgb})` }}>{laid.clusters[landed.cluster].label} · Landed</Eyebrow>
          <p className="m-0 font-serif text-[24px] leading-[1.2] text-white md:text-[30px]">{landed.star.text}</p>
          {landed.star.context ? <p className="m-0 text-[14px] leading-[1.6] text-os-muted">{landed.star.context}</p> : null}
          {(() => {
            const line = sourceLine(landed.star.source_type, landed.star.created_at);
            return line ? <span className="text-[12px] text-os-faint">{line}</span> : null;
          })()}
          {links.length ? (
            <div className="flex flex-col gap-2 border-t border-os-hairline pt-[14px]">
              <Eyebrow>Connects to</Eyebrow>
              {links.map((idx) => (
                <button
                  key={idx}
                  type="button"
                  onClick={() => {
                    takeOff();
                    flyTo(idx);
                  }}
                  className="os-focus min-h-[36px] rounded text-left text-[14px] leading-snug text-os-accent hover:underline"
                >
                  {laid.stars[idx].star.text}
                  <span className="sr-only">, in {clusterOf(idx).label}</span>
                </button>
              ))}
            </div>
          ) : null}
          <form onSubmit={submitNote} className="flex flex-col gap-2 pt-1">
            <label htmlFor="gf-note" className="text-[11px] font-semibold uppercase tracking-[0.18em] text-os-label">
              A note for this star
            </label>
            <div className="flex items-end gap-3">
              <input
                id="gf-note"
                ref={noteRef}
                value={noteText}
                onChange={(e) => setNoteText(e.target.value)}
                placeholder="What does this mean to you now?"
                maxLength={2000}
                className="os-focus min-h-[42px] w-full border-0 border-b border-os-ring bg-transparent text-[15px] text-white outline-none placeholder:text-os-faint"
              />
              <button type="submit" disabled={!noteText.trim() || addNote.isPending} className="os-btn-text os-focus shrink-0 rounded text-[14px] disabled:opacity-40">
                {addNote.isPending ? "Saving…" : "Save"}
              </button>
            </div>
            {addNote.isError ? <span className="text-[12px] text-os-danger">Couldn&apos;t save that note. Try again.</span> : null}
          </form>
          {notes.data?.length ? (
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {notes.data.slice(0, 4).map((n) => (
                <li key={n.id} className="text-[13px] leading-snug text-os-muted">
                  {n.text}
                  <span className="ml-2 text-[11px] text-os-faint">{new Date(n.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</span>
                </li>
              ))}
            </ul>
          ) : null}
          <button type="button" onClick={takeOff} className="os-btn os-focus mt-2 self-start" aria-keyshortcuts="Escape">
            Take off
          </button>
        </section>
      ) : null}
    </div>
  );
}
