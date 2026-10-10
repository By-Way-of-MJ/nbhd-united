"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { ghostBtnCls, quietBtnCls, textBtnCls } from "@/components/neighborhood/ui";
import {
  bondLine,
  buildPeopleSky,
  clusterPose,
  findPeople,
  homePose,
  personPose,
  reachLine,
  reachTotalLine,
  type SkyClusterInput,
  type SkyPersonInput,
} from "@/lib/people-sky/layout";
import { clusterCss, PeopleSkyRenderer, type SkyPick } from "@/lib/people-sky/renderer";
import type { CircleDetail, CircleSummary, HomeNeighbor } from "@/lib/types";

type Sel = { kind: "person"; id: string } | { kind: "cluster"; id: string } | null;

const LIST_PAGE = 60;

// Wide screens float the card over the sky; phones put it underneath.
const WIDE = "(min-width: 640px)";
function subscribeWide(cb: () => void) {
  const m = window.matchMedia(WIDE);
  m.addEventListener("change", cb);
  return () => m.removeEventListener("change", cb);
}
const getWide = () => window.matchMedia(WIDE).matches;
const getWideServer = () => true;

function sinceYear(n: HomeNeighbor) {
  return n.friends_since.slice(0, 4);
}

function joinNames(names: string[], max = 4): string {
  if (names.length <= max) return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0] ?? "";
  return `${names.slice(0, max).join(", ")} and ${names.length - max} more`;
}

/**
 * "Your people": you as the bright centre star, friends placed in 3D by how
 * much you share, clusters as nebulae, friends-of-friends as nameless
 * glimmers. Drag to look around, scroll (after a click) or pinch to go deeper,
 * click a star or a nebula to fly there. Below it, everyone A–Z.
 */
export function PeopleSky({
  neighbors,
  circles,
  circleDetails,
  reachTotal,
  loading,
  error,
  skyPending,
  skyError,
  onMessage,
  onToggleSky,
  onShareLesson,
  onOpenCluster,
  onInviteCluster,
  onInvite,
}: {
  neighbors: HomeNeighbor[];
  circles: CircleSummary[];
  circleDetails: Map<string, CircleDetail>;
  reachTotal?: string | null;
  loading: boolean;
  error: string;
  skyPending: boolean;
  skyError: string;
  onMessage: (n: HomeNeighbor) => void;
  onToggleSky: (n: HomeNeighbor) => void;
  onShareLesson: (n: HomeNeighbor) => void;
  onOpenCluster: (circleId: string) => void;
  onInviteCluster: (circleId: string) => void;
  onInvite: () => void;
}) {
  // ── Model ──────────────────────────────────────────────────────────────
  const byHandle = useMemo(() => new Map(neighbors.map((n) => [n.handle, n])), [neighbors]);
  const clusterInputs: SkyClusterInput[] = useMemo(
    () =>
      circles.map((c) => ({
        id: c.circle_id,
        name: c.name,
        hue: c.hue,
        memberIds: (circleDetails.get(c.circle_id)?.members ?? [])
          .filter((m) => !m.is_me && m.handle && byHandle.has(m.handle))
          .map((m) => byHandle.get(m.handle as string)!.friendship_id),
      })),
    [circles, circleDetails, byHandle],
  );
  const personInputs: SkyPersonInput[] = useMemo(
    () =>
      neighbors.map((n) => ({
        id: n.friendship_id,
        name: n.display_name,
        handle: n.handle,
        inSky: n.in_my_sky,
        bond: n.bond,
        hue: n.avatar_hue,
        reach: n.reach ?? null,
        clusters: clusterInputs.filter((c) => c.memberIds.includes(n.friendship_id)).map((c) => c.id),
      })),
    [neighbors, clusterInputs],
  );
  const model = useMemo(() => buildPeopleSky(personInputs, clusterInputs), [personInputs, clusterInputs]);
  const personIndex = useMemo(() => new Map(model.people.map((p, i) => [p.id, i])), [model]);
  const clusterIndex = useMemo(() => new Map(model.clusters.map((c, i) => [c.id, i])), [model]);
  const neighborById = useMemo(() => new Map(neighbors.map((n) => [n.friendship_id, n])), [neighbors]);

  // ── Selection + search ────────────────────────────────────────────────
  const [sel, setSel] = useState<Sel>(null);
  const [query, setQuery] = useState("");
  const hits = useMemo(() => findPeople(model.people, query), [model, query]);
  const selPerson = sel?.kind === "person" ? neighborById.get(sel.id) ?? null : null;
  const selCluster = sel?.kind === "cluster" ? circles.find((c) => c.circle_id === sel.id) ?? null : null;
  const personGone = sel?.kind === "person" && !selPerson;
  const clusterGone = sel?.kind === "cluster" && (!selCluster || !clusterIndex.has(sel.id));
  if (personGone || clusterGone) setSel(null);

  // ── Canvas ─────────────────────────────────────────────────────────────
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const topRef = useRef<HTMLDivElement>(null);
  const [renderer] = useState(() => new PeopleSkyRenderer());
  const wide = useSyncExternalStore(subscribeWide, getWide, getWideServer);
  const placed = useRef(false);
  const [engaged, setEngaged] = useState(false);
  const [nudge, setNudge] = useState(false);

  useEffect(() => {
    renderer.setModel(model, !placed.current);
    placed.current = true;
  }, [renderer, model]);
  useEffect(() => {
    renderer.cardOpen = !!sel && wide;
    renderer.invalidate();
    const idx = sel?.kind === "person" ? personIndex.get(sel.id) : sel?.kind === "cluster" ? clusterIndex.get(sel.id) : undefined;
    renderer.setSelection(sel && idx !== undefined ? { kind: sel.kind, index: idx } : null);
  }, [renderer, sel, personIndex, clusterIndex, wide]);
  useEffect(() => {
    renderer.setHits(query.trim() ? new Set(hits.map((p) => personIndex.get(p.id)!).filter((i) => i !== undefined)) : null);
  }, [renderer, hits, query, personIndex]);

  const flyTo = useCallback(
    (next: Sel) => {
      const cam = renderer.pose();
      if (next?.kind === "person") {
        const i = personIndex.get(next.id);
        if (i !== undefined) renderer.goTo(personPose(model.people[i], cam.yaw, Math.max(0.12, Math.min(0.4, cam.pitch))), performance.now());
      } else if (next?.kind === "cluster") {
        const i = clusterIndex.get(next.id);
        if (i !== undefined) renderer.goTo(clusterPose(model.clusters[i], cam.yaw, Math.max(0.12, Math.min(0.4, cam.pitch))), performance.now());
      } else {
        renderer.goTo(homePose(model, cam.yaw), performance.now());
      }
    },
    [renderer, model, personIndex, clusterIndex],
  );
  const select = useCallback(
    (next: Sel, scroll = false) => {
      setSel(next);
      flyTo(next);
      if (scroll) wrapRef.current?.scrollIntoView({ block: "nearest", behavior: renderer.reduced ? "auto" : "smooth" });
    },
    [flyTo, renderer],
  );

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
    let visible = true;
    let size = { w: 0, h: 0, dpr: 1 };
    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(1, Math.round(rect.width)), h = Math.max(1, Math.round(rect.height));
      const top = topRef.current ? topRef.current.offsetTop + topRef.current.offsetHeight : 0;
      if (w !== size.w || h !== size.h || dpr !== size.dpr || top !== renderer.insetTop) {
        size = { w, h, dpr };
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
        renderer.compact = w < 640;
        renderer.insetTop = top;
        renderer.insetBottom = w < 640 ? 36 : 44;
        renderer.invalidate();
      }
    };
    const frame = (ms: number) => {
      raf = requestAnimationFrame(frame);
      resize();
      if (renderer.animating()) renderer.draw(ctx, size.w, size.h, size.dpr, ms);
    };
    const start = () => {
      if (!raf && visible && !document.hidden) raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      cancelAnimationFrame(raf);
      raf = 0;
    };
    // Paused while the tab is hidden or the sky is scrolled out of view.
    const onVis = () => (document.hidden ? stop() : (renderer.invalidate(), start()));
    document.addEventListener("visibilitychange", onVis);
    const io = new IntersectionObserver(([e]) => {
      visible = e.isIntersecting;
      if (visible) {
        renderer.invalidate();
        start();
      } else stop();
    });
    io.observe(canvas);
    start();
    return () => {
      stop();
      io.disconnect();
      document.removeEventListener("visibilitychange", onVis);
      reduce.removeEventListener("change", applyReduce);
    };
  }, [renderer]);

  // Pointer: drag to orbit, pinch to go deeper, click a star or a nebula.
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const gesture = useRef<{ moved: boolean; pinch: number }>({ moved: false, pinch: 0 });
  const local = (e: { clientX: number; clientY: number }) => {
    const r = canvasRef.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 1) gesture.current = { moved: false, pinch: 0 };
    setEngaged(true);
    setNudge(false);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const prev = pointers.current.get(e.pointerId);
    if (!prev) {
      if (e.pointerType === "mouse") {
        const { x, y } = local(e);
        const hit = renderer.pick(x, y, false);
        renderer.setHover(hit?.kind === "person" ? hit.index : -1);
        e.currentTarget.style.cursor = hit ? "pointer" : "grab";
      }
      return;
    }
    const now = performance.now();
    if (pointers.current.size >= 2) {
      pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const [a, b] = [...pointers.current.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (gesture.current.pinch) renderer.zoom(gesture.current.pinch / d, now);
      gesture.current.pinch = d;
      gesture.current.moved = true;
      return;
    }
    const dx = e.clientX - prev.x, dy = e.clientY - prev.y;
    if (!gesture.current.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    gesture.current.moved = true;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    renderer.orbit(dx, dy, now);
    e.currentTarget.style.cursor = "grabbing";
  };
  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const had = pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) gesture.current.pinch = 0;
    if (!had || gesture.current.moved || pointers.current.size > 0) {
      if (pointers.current.size === 0) e.currentTarget.style.cursor = "grab";
      return;
    }
    const { x, y } = local(e);
    const hit: SkyPick | null = renderer.pick(x, y, e.pointerType !== "mouse");
    if (hit?.kind === "person") select({ kind: "person", id: model.people[hit.index].id });
    else if (hit?.kind === "cluster") select({ kind: "cluster", id: model.clusters[hit.index].id });
  };
  const onPointerCancel = (e: React.PointerEvent<HTMLCanvasElement>) => {
    pointers.current.delete(e.pointerId);
    gesture.current.pinch = 0;
  };

  // Wheel goes deeper only once you've clicked into the sky (or with ⌘/Ctrl,
  // which is also how trackpad pinch arrives) — otherwise the page scrolls.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (e: WheelEvent) => {
      if (!engaged && !e.ctrlKey && !e.metaKey) {
        setNudge(true);
        return;
      }
      e.preventDefault();
      const k = Math.exp(Math.max(-0.5, Math.min(0.5, e.deltaY * (e.ctrlKey ? 0.01 : 0.0022))));
      renderer.zoom(k, performance.now());
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, [renderer, engaged]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    const now = performance.now();
    const step = e.shiftKey ? 60 : 24;
    if (e.key === "ArrowLeft") renderer.orbit(-step, 0, now);
    else if (e.key === "ArrowRight") renderer.orbit(step, 0, now);
    else if (e.key === "ArrowUp") renderer.orbit(0, -step, now);
    else if (e.key === "ArrowDown") renderer.orbit(0, step, now);
    else if (e.key === "+" || e.key === "=") renderer.zoom(0.85, now);
    else if (e.key === "-" || e.key === "_") renderer.zoom(1.18, now);
    else if (e.key === "Escape" && sel) select(null);
    else return;
    e.preventDefault();
  };

  // Typing a name flies to the best match once you pause.
  useEffect(() => {
    const q = query.trim();
    if (!q || !hits.length) return;
    const t = window.setTimeout(() => {
      const first = hits[0];
      if (sel?.kind !== "person" || sel.id !== first.id) select({ kind: "person", id: first.id });
    }, 380);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, hits]);

  // ── Words ──────────────────────────────────────────────────────────────
  const total = reachTotalLine(reachTotal);
  const selModel = selPerson ? model.people[personIndex.get(selPerson.friendship_id) ?? -1] : undefined;
  const sharedClusters = selModel ? selModel.clusters.map((id) => circles.find((c) => c.circle_id === id)).filter((c): c is CircleSummary => !!c) : [];
  const primaryCluster = sharedClusters[0];
  const selClusterModel = selCluster ? model.clusters[clusterIndex.get(selCluster.circle_id) ?? -1] : undefined;
  const clusterDetail = selCluster ? circleDetails.get(selCluster.circle_id) : undefined;
  const clusterNames = clusterDetail ? [...clusterDetail.members].sort((a, b) => Number(b.is_me) - Number(a.is_me)).map((m) => (m.is_me ? "You" : m.display_name.split(/\s+/)[0])) : [];
  const skyLabel = neighbors.length
    ? `Your people as a star cluster: you at the centre, ${neighbors.length} ${neighbors.length === 1 ? "friend" : "friends"} around you${model.clusters.length ? `, ${model.clusters.length} ${model.clusters.length === 1 ? "cluster" : "clusters"} as nebulae` : ""}. Everyone is also listed A to Z below.`
    : "Your sky, with only you in it so far.";

  const card = selPerson ? (
    <aside aria-label={selPerson.display_name} aria-live="polite" className="os-fade-in flex flex-col gap-2.5">
      <span className="text-[0.6875rem] font-semibold uppercase tracking-[0.16em]" style={{ color: primaryCluster ? clusterCss(primaryCluster.hue) : "var(--os-label)" }}>
        {selPerson.in_my_sky ? "In your sky" : "Friend"}
        {sharedClusters.length ? ` · ${sharedClusters.map((c) => c.name).join(" · ")}` : ""}
      </span>
      <span className="os-serif text-[2rem] leading-none text-white sm:text-[2.5rem]">{selPerson.display_name}</span>
      <span className="text-[0.875rem] leading-relaxed text-os-muted">
        Friends since {sinceYear(selPerson)}. {bondLine({ inSky: selPerson.in_my_sky, bond: selPerson.bond })}
      </span>
      {reachLine(selPerson.reach, selPerson.display_name) ? <span className="text-[0.8125rem] text-os-faint">{reachLine(selPerson.reach, selPerson.display_name)}</span> : null}
      {skyError ? <span role="alert" className="text-[0.8125rem] text-os-danger">{skyError}</span> : null}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-1">
        <button type="button" className={ghostBtnCls} onClick={() => onMessage(selPerson)}>
          Message
        </button>
        <button type="button" className={textBtnCls} onClick={() => onShareLesson(selPerson)}>
          Share a lesson
        </button>
        <button type="button" className={quietBtnCls} disabled={skyPending} onClick={() => onToggleSky(selPerson)}>
          {selPerson.in_my_sky ? "Take out of your sky" : "Add to your sky"}
        </button>
        <button type="button" className={quietBtnCls} onClick={() => select(null)}>
          Back to you
        </button>
      </div>
    </aside>
  ) : selCluster && selClusterModel ? (
    <aside aria-label={selCluster.name} aria-live="polite" className="os-fade-in flex flex-col gap-2.5">
      <span className="text-[0.6875rem] font-semibold uppercase tracking-[0.16em]" style={{ color: clusterCss(selCluster.hue) }}>
        Cluster · {selCluster.member_count} {selCluster.member_count === 1 ? "person" : "people"}
        {selCluster.my_role === "admin" ? " · you host" : ""}
      </span>
      <span className="os-serif text-[2rem] leading-none text-white sm:text-[2.5rem]">{selCluster.name}</span>
      {clusterNames.length ? <span className="text-[0.875rem] leading-relaxed text-os-muted">{joinNames(clusterNames)}.</span> : null}
      {clusterDetail?.description ? <span className="line-clamp-2 text-[0.8125rem] text-os-faint">{clusterDetail.description}</span> : null}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-1">
        <button type="button" className={ghostBtnCls} onClick={() => onOpenCluster(selCluster.circle_id)}>
          Open
        </button>
        <button type="button" className={textBtnCls} onClick={() => onInviteCluster(selCluster.circle_id)}>
          Invite
        </button>
        <button type="button" className={quietBtnCls} onClick={() => select(null)}>
          Back to you
        </button>
      </div>
    </aside>
  ) : null;

  return (
    <section aria-labelledby="people-heading" className="os-hairline-top pt-4">
      <div
        ref={wrapRef}
        className="relative -mx-4 h-[460px] scroll-mt-4 overflow-hidden sm:-mx-6 sm:h-[560px] md:mx-0 lg:h-[600px]"
        style={{
          WebkitMaskImage: "linear-gradient(to right, transparent, #000 5%, #000 95%, transparent), linear-gradient(to bottom, transparent, #000 7%, #000 90%, transparent)",
          WebkitMaskComposite: "source-in",
          maskImage: "linear-gradient(to right, transparent, #000 5%, #000 95%, transparent), linear-gradient(to bottom, transparent, #000 7%, #000 90%, transparent)",
          maskComposite: "intersect",
        }}
      >
        <canvas
          ref={canvasRef}
          role="img"
          aria-label={skyLabel}
          tabIndex={0}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerCancel}
          onPointerLeave={(e) => {
            if (e.pointerType === "mouse") {
              renderer.setHover(-1);
              setEngaged(false);
            }
          }}
          onKeyDown={onKeyDown}
          className="os-focus absolute left-0 top-0 block h-full w-full cursor-grab touch-pan-y select-none"
        />

        <div ref={topRef} className="pointer-events-none absolute inset-x-0 top-0 flex flex-col gap-3 px-6 pt-6 sm:flex-row sm:items-start sm:justify-between sm:px-9 sm:pt-7">
          <div className="flex flex-col gap-1.5">
            <h2 id="people-heading" className="text-[0.75rem] font-semibold uppercase tracking-[0.15em] text-os-label">
              Your people
            </h2>
            {total ? <p className="text-[0.8125rem] text-os-faint">{total}</p> : null}
          </div>
          {neighbors.length > 1 ? (
            <div className="pointer-events-auto flex w-full items-center gap-2 border-b border-os-ring focus-within:border-os-accent sm:w-[260px]">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" className="shrink-0 text-os-faint" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="M20 20l-4-4" />
              </svg>
              <label htmlFor="find-person" className="sr-only">
                Find a person
              </label>
              <input
                id="find-person"
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setQuery("");
                  if (e.key === "Enter" && hits[0]) select({ kind: "person", id: hits[0].id });
                }}
                placeholder="Find a person"
                autoComplete="off"
                className="min-h-[40px] min-w-0 flex-1 bg-transparent text-[0.875rem] text-os-ink outline-none placeholder:text-os-faint focus-visible:outline-none [&::-webkit-search-cancel-button]:hidden"
              />
              {query ? (
                <button type="button" onClick={() => setQuery("")} aria-label="Clear search" className="os-focus -mr-2 flex h-10 w-10 items-center justify-center rounded-full text-os-faint hover:text-os-ink">
                  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true">
                    <path d="M2 2l8 8M10 2l-8 8" />
                  </svg>
                </button>
              ) : null}
            </div>
          ) : null}
        </div>

        {loading || error || neighbors.length === 0 ? (
          <div className="pointer-events-none absolute inset-x-0 bottom-16 flex flex-col items-center gap-3 px-6 text-center">
            <p className="text-[0.9375rem] text-os-muted">{loading ? "Finding your people…" : error ? `Couldn’t load your people. ${error}` : "Your sky is waiting for its first friend."}</p>
            {!loading && !error ? (
              <button type="button" onClick={onInvite} className={`${ghostBtnCls} pointer-events-auto`}>
                Invite someone
              </button>
            ) : null}
          </div>
        ) : null}

        {card && wide ? (
          <div className="absolute bottom-0 right-9 w-[330px] pb-9">
            <div className="os-hairline-top pt-4">{card}</div>
          </div>
        ) : null}

        <div className="pointer-events-none absolute bottom-6 left-6 flex flex-col gap-1 text-[0.75rem] text-os-faint sm:bottom-9 sm:left-9">
          <span aria-live="polite">
            {nudge ? "Click the sky first, then scroll to go deeper" : neighbors.length ? (wide ? "Drag to look around · scroll or pinch to go deeper · click a star" : "Swipe sideways to look around · pinch to go deeper · tap a star") : ""}
          </span>
          {neighbors.length ? (
            <span className="hidden sm:inline">
              <span className="text-white">&#9679;</span> friends &nbsp;<span className="text-os-faint">&middot;</span> friends of friends (never named)
              {model.clusters.length ? (
                <>
                  {" "}
                  &nbsp;<span style={{ color: clusterCss(model.clusters[0].hue) }}>&#9676;</span> clusters
                </>
              ) : null}
            </span>
          ) : null}
        </div>
      </div>

      {card && !wide ? <div className="os-hairline-top mb-6 pt-4">{card}</div> : null}

      {neighbors.length ? <EveryoneColumns neighbors={neighbors} query={query} selectedId={selPerson?.friendship_id ?? null} onHover={(id) => renderer.setHover(id ? personIndex.get(id) ?? -1 : -1)} onSelect={(id) => select({ kind: "person", id }, true)} /> : null}
    </section>
  );
}

/** Everyone, A–Z, in compact columns — the full list for big neighborhoods and screen readers. */
function EveryoneColumns({
  neighbors,
  query,
  selectedId,
  onSelect,
  onHover,
}: {
  neighbors: HomeNeighbor[];
  query: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onHover: (id: string | null) => void;
}) {
  const [all, setAll] = useState(false);
  const sorted = useMemo(() => [...neighbors].sort((a, b) => a.display_name.localeCompare(b.display_name) || a.friendship_id.localeCompare(b.friendship_id)), [neighbors]);
  const q = query.trim();
  const rows = useMemo(
    () => (q ? findPeople(sorted.map((n) => ({ ...n, name: n.display_name })), q) : sorted),
    [sorted, q],
  );
  const visible = q || all ? rows : rows.slice(0, LIST_PAGE);
  return (
    <div className="mt-2">
      <div className="flex items-baseline justify-between gap-4 pb-2">
        <h3 className="text-[0.6875rem] font-semibold uppercase tracking-[0.15em] text-os-label">Everyone, A&ndash;Z</h3>
        {q ? <span className="text-[0.8125rem] text-os-faint">{rows.length ? `${rows.length} ${rows.length === 1 ? "match" : "matches"}` : `No one matches “${q}”`}</span> : null}
      </div>
      <ul className="os-hairline-top columns-2 gap-x-8 pt-2 sm:columns-3 lg:columns-4">
        {visible.map((n, i) => {
          const letter = n.display_name.charAt(0).toLocaleUpperCase();
          const head = !q && (i === 0 || visible[i - 1].display_name.charAt(0).toLocaleUpperCase() !== letter);
          const on = n.friendship_id === selectedId;
          return (
            <li key={n.friendship_id} className="break-inside-avoid">
              {head ? <span className="block pb-0.5 pt-2 text-[0.6875rem] font-semibold text-os-faint">{letter}</span> : null}
              <button
                type="button"
                onClick={() => onSelect(n.friendship_id)}
                onMouseEnter={() => onHover(n.friendship_id)}
                onMouseLeave={() => onHover(null)}
                onFocus={() => onHover(n.friendship_id)}
                onBlur={() => onHover(null)}
                aria-pressed={on}
                className={`os-focus flex min-h-[36px] w-full items-center gap-2 rounded text-left text-[0.875rem] transition ${on ? "text-os-accent" : "text-os-ink hover:text-white"}`}
              >
                <span className="truncate">{n.display_name}</span>
                {n.in_my_sky ? <span className="shrink-0 text-[0.6875rem] text-os-accent" aria-label="in your sky">&#10022;</span> : null}
              </button>
            </li>
          );
        })}
      </ul>
      {!q && !all && rows.length > LIST_PAGE ? (
        <button type="button" onClick={() => setAll(true)} className={`${textBtnCls} mt-1`}>
          Show all {rows.length}
        </button>
      ) : null}
    </div>
  );
}

