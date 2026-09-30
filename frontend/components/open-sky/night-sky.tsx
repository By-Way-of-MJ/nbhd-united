"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { isPlayEnabled } from "@/lib/constellation-game/flag";
import { withTagClusters } from "@/lib/constellation-data";
import {
  buildSky,
  countLine,
  filterSearchHits,
  LOOSE_KEY,
  lightYearsLabel,
  matchLine,
  navHint,
  poseFor,
  sourceLine,
  startIndex,
  stepIndex,
} from "@/lib/night-sky/model";
import { NightSkyRenderer } from "@/lib/night-sky/renderer";
import { useConstellationQuery, useDeleteLessonMutation, useLessonSearchQuery, usePendingLessonsQuery } from "@/lib/queries";
import type { ConstellationData } from "@/lib/types";

const EMPTY: ConstellationData = { nodes: [], edges: [], affinity_edges: [], clusters: [] };

const Chevron = ({ dir, size = 17 }: { dir: "left" | "right"; size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={dir === "left" ? "M15 6l-6 6 6 6" : "M9 6l6 6-6 6"} />
  </svg>
);

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function isTyping(el: EventTarget | null): boolean {
  const t = el as HTMLElement | null;
  return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
}

/** A ghost circle with its word and a quiet hint underneath ("turn left, deeper"). */
function NavButton({ label, hint, icon, onClick, href, disabled }: { label: string; hint?: string; icon: React.ReactNode; onClick?: () => void; href?: string; disabled?: boolean }) {
  const inner = (
    <>
      <span className="flex h-12 w-12 items-center justify-center rounded-full border border-os-ring text-white transition group-hover:border-os-accent-line group-hover:text-os-accent sm:h-[52px] sm:w-[52px]" aria-hidden="true">
        {icon}
      </span>
      <span className="text-[0.75rem] text-os-muted">{label}</span>
      {hint !== undefined ? <span className="min-h-[1rem] text-[0.6875rem] leading-4 text-os-faint">{hint}</span> : null}
    </>
  );
  const cls = "group os-focus flex w-[96px] flex-col items-center gap-1.5 rounded-2xl disabled:opacity-40";
  if (href) {
    return (
      <Link href={href} className={cls} aria-label={label}>
        {inner}
      </Link>
    );
  }
  return (
    <button type="button" onClick={onClick} disabled={disabled} className={cls} aria-label={hint ? `${label} constellation (${hint})` : label}>
      {inner}
    </button>
  );
}

/**
 * Constellation (Open Sky): the whole page is one night sky with depth. Every
 * star is a real lesson, every constellation a real cluster; older
 * constellations sit farther away. Previous / Next fly the camera calmly
 * between them, a click on any star goes to it, and search finds lessons by
 * meaning.
 */
export function OpenSkyConstellation() {
  const { data: raw = EMPTY, isLoading, error } = useConstellationQuery();
  const { data: pending = [] } = usePendingLessonsQuery();
  const deleteLesson = useDeleteLessonMutation();
  const [playEnabled, setPlayEnabled] = useState(false);
  useEffect(() => setPlayEnabled(isPlayEnabled()), []);

  const data = useMemo(() => withTagClusters(raw), [raw]);
  const [now] = useState(() => new Date());
  const sky = useMemo(() => buildSky(data, now), [data, now]);
  const known = useMemo(() => new Set(data.nodes.map((n) => n.id)), [data]);
  const constellationCount = sky.clusters.filter((c) => c.key !== LOOSE_KEY).length;

  // Selection by stable ids, resolved against the current sky.
  const [sel, setSel] = useState<{ key: number; lesson: number } | null>(null);
  const found = sel ? sky.clusters.findIndex((c) => c.key === sel.key) : -1;
  const ci = found >= 0 ? found : startIndex(sky.clusters);
  const cluster = sky.clusters[ci];
  const li = cluster ? Math.max(0, cluster.lessons.findIndex((l) => l.id === sel?.lesson)) : 0;
  const lesson = cluster ? (sel && cluster.lessons[li]?.id === sel.lesson ? cluster.lessons[li] : cluster.lessons[cluster.lessons.length - 1]) : undefined;
  const lessonIndex = cluster && lesson ? cluster.lessons.indexOf(lesson) : 0;

  const goCluster = useCallback((idx: number, lessonId?: number) => {
    const c = sky.clusters[idx];
    if (!c) return;
    setSel({ key: c.key, lesson: lessonId ?? c.lessons[c.lessons.length - 1].id });
  }, [sky]);
  const step = useCallback((d: number) => goCluster(stepIndex(ci, d, sky.clusters.length)), [goCluster, ci, sky.clusters.length]);
  const stepLesson = (d: number) => {
    if (!cluster) return;
    setSel({ key: cluster.key, lesson: cluster.lessons[stepIndex(lessonIndex, d, cluster.lessons.length)].id });
  };

  // ── Search by meaning ──────────────────────────────────────────────────
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query, 300);
  const q = debounced.trim();
  const search = useLessonSearchQuery(q);
  const settled = !!q && !!search.data && !search.isPlaceholderData && q === query.trim();
  const matches = useMemo(() => (q && search.data ? filterSearchHits(search.data, known) : null), [q, search.data, known]);
  const [matchIndex, setMatchIndex] = useState(0);
  const matchKey = settled && matches ? `${q}|${matches.join(",")}` : "";
  const lessonCluster = useMemo(() => {
    const m = new Map<number, number>();
    sky.clusters.forEach((c, i) => c.lessons.forEach((l) => m.set(l.id, i)));
    return m;
  }, [sky]);
  const goLesson = useCallback((id: number) => {
    const idx = lessonCluster.get(id);
    if (idx != null) goCluster(idx, id);
  }, [lessonCluster, goCluster]);
  // A fresh result set travels to its best match.
  useEffect(() => {
    if (!matchKey || !matches?.length) return;
    setMatchIndex(0);
    goLesson(matches[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchKey]);
  const searching = !!query.trim();
  const matchClusters = matches ? new Set(matches.map((id) => lessonCluster.get(id))).size : 0;
  let status = "";
  if (searching) {
    if (search.isError) status = "Search isn't available right now";
    else if (!settled) status = "Searching…";
    else status = matchLine(matches?.length ?? 0, matchClusters, matchIndex);
  }
  const nextMatch = () => {
    if (!matches?.length) return;
    const i = (matchIndex + 1) % matches.length;
    setMatchIndex(i);
    goLesson(matches[i]);
  };

  // ── Canvas ─────────────────────────────────────────────────────────────
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [renderer] = useState(() => new NightSkyRenderer());
  const placed = useRef(false);
  const panelRef = useRef<HTMLElement>(null);
  const headerRef = useRef<HTMLElement>(null);
  // Constellation names fade before they reach the lesson text.
  useEffect(() => {
    const el = panelRef.current;
    if (!el) return;
    const measure = () => {
      const eyebrow = el.firstElementChild as HTMLElement | null;
      renderer.labelFloor = eyebrow ? el.offsetTop + eyebrow.offsetTop : el.offsetTop;
      const head = headerRef.current;
      renderer.labelCeil = head ? head.offsetTop + head.offsetHeight : 0;
      renderer.invalidate();
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (headerRef.current) ro.observe(headerRef.current);
    return () => ro.disconnect();
  }, [renderer]);

  useEffect(() => {
    renderer.setModel(sky);
  }, [renderer, sky]);
  useEffect(() => {
    renderer.setSelection(ci, lesson?.id ?? -1);
  }, [renderer, ci, lesson?.id]);
  useEffect(() => {
    renderer.setMatches(searching && matches?.length ? new Set(matches) : null);
  }, [renderer, searching, matches]);
  const poseKey = cluster ? `${cluster.key}:${cluster.az}:${cluster.alt}:${cluster.dist}:${cluster.view}` : "";
  useEffect(() => {
    if (!cluster) return;
    const pose = poseFor(cluster);
    if (!placed.current) {
      renderer.place(pose);
      placed.current = true;
    } else renderer.goTo(pose, performance.now());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renderer, poseKey]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
    const applyReduce = () => {
      renderer.reduced = reduce.matches;
      renderer.invalidate();
    };
    applyReduce();
    reduce.addEventListener("change", applyReduce);
    let raf = 0;
    let size = { w: 0, h: 0, dpr: 1 };
    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(1, Math.round(rect.width)), h = Math.max(1, Math.round(rect.height));
      if (w !== size.w || h !== size.h || dpr !== size.dpr) {
        size = { w, h, dpr };
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
        renderer.compact = w < 640;
        renderer.invalidate();
      }
    };
    const frame = (ms: number) => {
      raf = requestAnimationFrame(frame);
      resize();
      if (renderer.animating()) renderer.draw(ctx, size.w, size.h, size.dpr, ms);
    };
    const start = () => {
      if (!raf) raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      cancelAnimationFrame(raf);
      raf = 0;
    };
    // Paused while the tab is hidden.
    const onVis = () => (document.hidden ? stop() : (renderer.invalidate(), start()));
    document.addEventListener("visibilitychange", onVis);
    if (!document.hidden) start();
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
      reduce.removeEventListener("change", applyReduce);
    };
  }, [renderer]);

  const pickAt = (e: React.MouseEvent<HTMLCanvasElement>, touch: boolean) => {
    const r = e.currentTarget.getBoundingClientRect();
    return renderer.pick(e.clientX - r.left, e.clientY - r.top, touch ? 34 : 26);
  };
  const onCanvasClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const hit = pickAt(e, false);
    if (hit) goCluster(hit.cluster, hit.id);
  };
  const onCanvasMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    e.currentTarget.style.cursor = pickAt(e, false) ? "pointer" : "default";
  };

  // ←/→ walk the constellations (unless you're typing).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.metaKey || e.ctrlKey || isTyping(e.target)) return;
      if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step]);

  // ── Lesson actions ─────────────────────────────────────────────────────
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setMenuOpen(false);
    setConfirmDelete(false);
  }, [lesson?.id]);
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onEsc = (e: KeyboardEvent) => e.key === "Escape" && setMenuOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onEsc);
    };
  }, [menuOpen]);
  const onDelete = () => {
    if (!lesson || !cluster) return;
    const rest = cluster.lessons.filter((l) => l.id !== lesson.id);
    deleteLesson.mutate(lesson.id, {
      onSuccess: () => {
        setMenuOpen(false);
        setConfirmDelete(false);
        if (rest.length) setSel({ key: cluster.key, lesson: rest[Math.min(lessonIndex, rest.length - 1)].id });
        else setSel(null);
      },
    });
  };

  const n = sky.clusters.length;
  const prev = n ? sky.clusters[stepIndex(ci, -1, n)] : undefined;
  const next = n ? sky.clusters[stepIndex(ci, 1, n)] : undefined;
  const skyLabel = n
    ? `A night sky of your lessons: ${countLine(sky.lessonCount, constellationCount)}. Older constellations sit farther away. Now showing ${cluster?.name}.`
    : "A night sky, waiting for your first lessons.";
  const errorText = error ? (error instanceof Error ? error.message : "Couldn't load your constellation.") : "";

  return (
    <div className="relative h-full w-full overflow-hidden text-os-ink" style={{ background: "#030408" }}>
      <canvas
        ref={canvasRef}
        role="img"
        aria-label={skyLabel}
        onClick={onCanvasClick}
        onMouseMove={onCanvasMove}
        className="absolute inset-0 block h-full w-full"
      />
      {/* Soft shade behind the words — no edges, just the sky getting darker. */}
      <div className="pointer-events-none absolute inset-x-0 top-0 h-48 bg-gradient-to-b from-[#030408]/70 to-transparent" aria-hidden="true" />
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-80 bg-gradient-to-t from-[#030408]/80 via-[#030408]/40 to-transparent" aria-hidden="true" />

      <header ref={headerRef} className="pointer-events-none absolute inset-x-0 top-0 flex flex-col gap-3 px-5 pt-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6 sm:px-8 sm:pt-7 lg:px-11 lg:pt-8">
        <div className="pointer-events-auto flex flex-col">
          <h1 className="os-page-title">Constellation</h1>
          {n ? <p className="mt-2.5 text-[0.75rem] font-semibold uppercase tracking-[0.15em] text-os-faint">{countLine(sky.lessonCount, constellationCount)}</p> : null}
          {pending.length > 0 ? (
            <Link href="/constellation/pending" className="os-focus -mb-2 mt-0.5 inline-flex min-h-[44px] items-center self-start rounded text-[0.8125rem] text-os-accent hover:underline">
              {pending.length} new {pending.length === 1 ? "lesson" : "lessons"} to review ›
            </Link>
          ) : null}
        </div>
        {n ? (
          <div className="pointer-events-auto flex w-full flex-col gap-1 sm:w-[340px] sm:items-end">
            <div className="flex w-full items-center gap-2.5 border-b border-os-ring/60 focus-within:border-os-accent">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" className="shrink-0 text-os-faint" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="M20 20l-4-4" />
              </svg>
              <label htmlFor="sky-search" className="sr-only">Search your lessons by meaning</label>
              <input
                id="sky-search"
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setQuery("");
                  if (e.key === "Enter" && matches && matches.length > 1) nextMatch();
                }}
                placeholder="Search by meaning, e.g. tired"
                autoComplete="off"
                className="min-h-[44px] min-w-0 flex-1 bg-transparent text-[0.9375rem] text-os-ink outline-none placeholder:text-os-faint focus-visible:outline-none [&::-webkit-search-cancel-button]:hidden"
              />
              {query ? (
                <button type="button" onClick={() => setQuery("")} aria-label="Clear search" className="os-focus -mr-2 flex h-11 w-11 items-center justify-center rounded-full text-os-faint hover:text-os-ink">
                  <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8" /></svg>
                </button>
              ) : null}
            </div>
            <div className="flex min-h-[32px] flex-wrap items-center gap-x-3.5 whitespace-nowrap text-[0.8125rem] text-os-muted" aria-live="polite">
              {status ? <span>{status}</span> : null}
              {settled && matches && matches.length > 1 ? (
                <button type="button" onClick={nextMatch} aria-label="Next match" className="os-focus -my-1.5 inline-flex min-h-[44px] items-center rounded text-os-accent hover:underline">
                  next match ›
                </button>
              ) : null}
            </div>
          </div>
        ) : null}
      </header>

      <section
        ref={panelRef}
        aria-label="The chosen lesson"
        className="absolute inset-x-0 bottom-0 flex flex-col items-center px-5 pb-[calc(env(safe-area-inset-bottom)+96px)] text-center md:px-10 md:pb-7"
      >
        {isLoading ? (
          <p className="mb-24 text-[0.9375rem] text-os-muted">Loading your constellation…</p>
        ) : errorText ? (
          <p className="mb-24 text-[0.9375rem] text-os-ink">{errorText}</p>
        ) : !cluster || !lesson ? (
          <div className="mb-20 max-w-sm">
            <p className="font-serif text-[1.75rem] leading-tight text-white">Your constellation begins here</p>
            <p className="mt-2 text-[0.9375rem] text-os-muted">As you talk with your assistant, the lessons you keep appear here as stars.</p>
          </div>
        ) : (
          <>
            <p className="max-w-[46rem] text-[0.625rem] [text-wrap:balance] font-semibold uppercase tracking-[0.13em] text-os-muted sm:text-[0.6875rem] sm:tracking-[0.18em]">
              {cluster.name} · {lightYearsLabel(cluster.lightYears)} · from {cluster.when}
            </p>
            <p key={lesson.id} className="os-fade-in mt-2.5 line-clamp-3 max-w-[44rem] [text-wrap:balance] sm:line-clamp-4 font-serif text-[1.375rem] leading-[1.22] text-[rgba(255,250,242,0.96)] sm:text-[1.75rem]">
              {lesson.text}
            </p>
            {/* Phone: source on its own line, steppers beneath. Wider: one quiet row. */}
            <div className="mt-1.5 flex flex-wrap items-center justify-center text-[0.75rem] text-os-faint sm:mt-1 sm:flex-nowrap">
              <span className="w-full px-1 sm:order-2 sm:w-auto">{sourceLine(lesson.sourceType, lesson.createdAt)}</span>
              {cluster.lessons.length > 1 ? (
                <>
                  <button type="button" onClick={() => stepLesson(-1)} aria-label="Previous lesson in this constellation" className="os-focus flex h-11 w-11 items-center justify-center rounded-full hover:text-os-ink sm:order-1">
                    <Chevron dir="left" size={14} />
                  </button>
                  <span className="os-num whitespace-nowrap sm:order-3">
                    <span className="hidden sm:inline">· </span>
                    {lessonIndex + 1} of {cluster.lessons.length}
                  </span>
                  <button type="button" onClick={() => stepLesson(1)} aria-label="Next lesson in this constellation" className="os-focus flex h-11 w-11 items-center justify-center rounded-full hover:text-os-ink sm:order-4">
                    <Chevron dir="right" size={14} />
                  </button>
                </>
              ) : null}
              <div className="relative sm:order-5" ref={menuRef}>
                <button
                  type="button"
                  onClick={() => setMenuOpen((v) => !v)}
                  aria-label="Lesson actions"
                  aria-expanded={menuOpen}
                  className="os-focus flex h-11 w-11 items-center justify-center rounded-full hover:text-os-ink"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" /></svg>
                </button>
                {menuOpen ? (
                  <div role="menu" className="absolute bottom-full right-0 z-20 mb-2 w-64 rounded-2xl border border-os-hairline bg-os-surface-solid p-2 text-left">
                    {!confirmDelete ? (
                      <button type="button" role="menuitem" onClick={() => setConfirmDelete(true)} className="os-focus flex min-h-[44px] w-full items-center rounded-xl px-3 text-[0.9375rem] text-os-danger hover:bg-os-accent-soft">
                        Delete lesson
                      </button>
                    ) : (
                      <div className="p-2">
                        <p className="text-[0.875rem] leading-relaxed text-os-ink">Delete this lesson permanently? This can&rsquo;t be undone.</p>
                        <div className="mt-3 flex gap-2">
                          <button type="button" disabled={deleteLesson.isPending} onClick={onDelete} className="os-focus min-h-[44px] flex-1 rounded-full border border-os-danger/60 text-[0.875rem] text-os-danger hover:bg-os-danger/10 disabled:opacity-50">
                            {deleteLesson.isPending ? "Deleting…" : "Delete"}
                          </button>
                          <button type="button" disabled={deleteLesson.isPending} onClick={() => setConfirmDelete(false)} className="os-focus min-h-[44px] flex-1 rounded-full text-[0.875rem] text-os-muted hover:text-os-ink">
                            Cancel
                          </button>
                        </div>
                        {deleteLesson.isError ? <p className="mt-2 text-[0.8125rem] text-os-danger">Couldn&rsquo;t delete it. Try again.</p> : null}
                      </div>
                    )}
                  </div>
                ) : null}
              </div>
            </div>
            <div className="mt-2 flex items-start justify-center gap-4 sm:gap-6">
              <NavButton label="Previous" hint={n > 1 && prev ? navHint(cluster, prev) : ""} icon={<Chevron dir="left" />} onClick={() => step(-1)} disabled={n < 2} />
              {playEnabled ? (
                <NavButton
                  label="Fly in"
                  href="/constellation/play"
                  icon={<svg width="14" height="14" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M2 1.4l6.2 3.6L2 8.6z" fill="currentColor" /></svg>}
                />
              ) : null}
              <NavButton label="Next" hint={n > 1 && next ? navHint(cluster, next) : ""} icon={<Chevron dir="right" />} onClick={() => step(1)} disabled={n < 2} />
            </div>
          </>
        )}
      </section>
    </div>
  );
}
