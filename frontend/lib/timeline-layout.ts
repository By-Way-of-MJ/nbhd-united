/**
 * Pure geometry for the project timeline (the Gantt). No DOM: the view draws
 * exactly what this computes, and `timeline-layout.test.ts` proves the "nothing
 * overlapping" rule — no arrow crosses a bar it isn't entering, no header
 * element sits on another, and name text stays in its column. A line-for-line
 * port of the iPhone's TimelineLayout.swift so both apps draw the same chart.
 *
 * Coordinates: the chart area starts at x = 0 (the pinned names column is a
 * separate element to its left). y = 0 is the top of the header; body rows
 * start at `headerHeight`.
 */

import { type Day, dayOfMonth, dayShort, isClosed, monthOf, planGroups, type PlanEdge, type ProjectPlan, stepSpan } from "./project-plan";

export type TimelineZoom = "weeks" | "whole";

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface TimelineOptions {
  zoom: TimelineZoom;
  milestonesOnly: boolean;
  /** Visible chart width (the scroll viewport); `whole` fits the project into it. */
  viewportWidth: number;
  nameColumnWidth: number;
  today: Day;
  /** Pixels per day at `weeks` zoom. */
  weekPointsPerDay: number;
}

export type TimelineRowKind = { type: "milestone"; id: string } | { type: "step"; id: string } | { type: "loose" };

export interface TimelineRow {
  kind: TimelineRowKind;
  y: number;
  height: number;
  /** The text area, in the NAMES column's coordinates (x from 0). */
  nameRect: Rect;
  /** Step bar (chart coordinates), if the step has dates. */
  bar: Rect | null;
  doneFraction: number;
  /** Milestones-only mode: the span bar and diamond for a milestone row. */
  span: Rect | null;
  diamond: Rect | null;
}

export interface TimelineTick {
  x: number;
  label: string;
  /** Null when the label is left out so it can't touch its neighbor. */
  labelRect: Rect | null;
}

export interface TimelineDiamond {
  milestoneId: string;
  rect: Rect;
  reached: boolean;
}

export interface TimelineArrow {
  blockerId: string;
  blockedId: string;
  /** Polyline, source first. The last point touches the top edge of the blocked bar. */
  points: Point[];
  /** Arrowhead triangle (tip last). */
  head: Point[];
}

export interface TimelineLayout {
  options: TimelineOptions;
  startDay: Day;
  endDay: Day;
  pointsPerDay: number;
  contentWidth: number;
  contentHeight: number;
  ticks: TimelineTick[];
  todayX: number;
  todayPill: Rect;
  diamonds: TimelineDiamond[];
  rows: TimelineRow[];
  arrows: TimelineArrow[];
}

export const TIMELINE = {
  headerHeight: 62,
  dateRowY: 2,
  dateRowHeight: 14,
  todayRowY: 22,
  todayPillWidth: 40,
  todayPillHeight: 15,
  diamondRowY: 44,
  diamondSize: 12,
  stepRowHeight: 40,
  barInset: 13,
  barHeight: 14,
  arrowClearance: 3,
  leftPad: 4,
  /** Room at the left of a step's name for its owner dot. */
  nameIndent: 24,
} as const;

export const DEFAULT_TIMELINE_OPTIONS: Omit<TimelineOptions, "today"> = {
  zoom: "weeks",
  milestonesOnly: false,
  viewportWidth: 258,
  nameColumnWidth: 100,
  weekPointsPerDay: 12.3,
};

export const maxX = (r: Rect) => r.x + r.width;
export const maxY = (r: Rect) => r.y + r.height;
export const midX = (r: Rect) => r.x + r.width / 2;
export const midY = (r: Rect) => r.y + r.height / 2;

/**
 * Conservative text width for layout-time collision checks (the rendered label
 * may be narrower, never wider, at these sizes).
 */
export function textWidth(text: string, size: number): number {
  return text.length * size * 0.62 + 2;
}

export function layoutTimeline(plan: ProjectPlan, options: TimelineOptions): TimelineLayout {
  const T = TIMELINE;
  const groupHeight = options.milestonesOnly ? 48 : 44;
  const groups = planGroups(plan);

  // ── Date range ─────────────────────────────────────────────────────────
  const days: Day[] = [options.today];
  for (const step of plan.steps) {
    const span = stepSpan(step);
    if (span) days.push(span.start, span.end);
  }
  for (const m of plan.milestones) if (m.target !== null) days.push(m.target);
  if (plan.target !== null) days.push(plan.target);
  const earliest = Math.min(...days);
  const latest = Math.max(...days);
  // Align to the Monday on/before the earliest day (1970-01-01 was a Thursday).
  const weekday = (((earliest % 7) + 7 + 3) % 7); // 0 = Monday
  const start = earliest - weekday;
  const totalDays = Math.max(latest - start + 4, 14);
  const ppd = options.zoom === "weeks" ? options.weekPointsPerDay : Math.max(3, (options.viewportWidth - T.leftPad - 8) / totalDays);
  const X = (d: Day) => T.leftPad + (d - start) * ppd;

  // ── Header: week ticks, labels spaced so they never touch ──────────────
  const ticks: TimelineTick[] = [];
  let lastLabelEnd = -Infinity;
  let lastMonth = -1;
  for (let week = 0; week * 7 <= totalDays; week++) {
    const day = start + week * 7;
    const x = X(day);
    const month = monthOf(day);
    const label = options.zoom === "weeks" || month !== lastMonth ? dayShort(day) : String(dayOfMonth(day));
    const rect: Rect = { x: x + 4, y: T.dateRowY, width: textWidth(label, 10), height: T.dateRowHeight };
    if (rect.x >= lastLabelEnd + 6) {
      ticks.push({ x, label, labelRect: rect });
      lastLabelEnd = maxX(rect);
      lastMonth = month;
    } else {
      ticks.push({ x, label: "", labelRect: null });
    }
  }

  // ── Today + milestone diamonds (each on its own header row) ────────────
  const todayX = X(options.today);
  const todayPill: Rect = { x: Math.max(0, todayX - T.todayPillWidth / 2), y: T.todayRowY, width: T.todayPillWidth, height: T.todayPillHeight };
  const diamonds: TimelineDiamond[] = [];
  let lastDiamondEnd = -Infinity;
  for (const m of plan.milestones) {
    if (m.target === null) continue;
    let minX = X(m.target) - T.diamondSize / 2;
    if (minX < lastDiamondEnd + 2) minX = lastDiamondEnd + 2; // same-week milestones sit side by side
    const rect: Rect = { x: minX, y: T.diamondRowY, width: T.diamondSize, height: T.diamondSize };
    diamonds.push({ milestoneId: m.id, rect, reached: m.reached });
    lastDiamondEnd = maxX(rect);
  }

  // ── Rows ───────────────────────────────────────────────────────────────
  const rows: TimelineRow[] = [];
  let y: number = T.headerHeight;
  const nameWidth = options.nameColumnWidth - 6;
  for (const group of groups) {
    const m = group.milestone;
    if (m) {
      let span: Rect | null = null;
      let diamond: Rect | null = null;
      if (options.milestonesOnly && m.target !== null) {
        const starts = group.steps.map((s) => stepSpan(s)?.start).filter((d): d is Day => d !== undefined);
        const first = starts.length ? Math.min(...starts) : m.target;
        const sx = X(Math.min(first, m.target));
        span = { x: sx, y: y + groupHeight / 2 - 2, width: Math.max(2, X(m.target) - sx), height: 4 };
        diamond = { x: X(m.target) - T.diamondSize / 2, y: y + groupHeight / 2 - T.diamondSize / 2, width: T.diamondSize, height: T.diamondSize };
      }
      rows.push({ kind: { type: "milestone", id: m.id }, y, height: groupHeight, nameRect: { x: 0, y: y + 4, width: nameWidth, height: groupHeight - 8 }, bar: null, doneFraction: 0, span, diamond });
      y += groupHeight;
    } else if (!options.milestonesOnly && group.steps.length > 0) {
      rows.push({ kind: { type: "loose" }, y, height: 30, nameRect: { x: 0, y: y + 4, width: nameWidth, height: 22 }, bar: null, doneFraction: 0, span: null, diamond: null });
      y += 30;
    }
    if (options.milestonesOnly) continue;
    for (const step of group.steps) {
      let bar: Rect | null = null;
      const s = stepSpan(step);
      if (s) {
        const bx = X(s.start);
        bar = { x: bx, y: y + T.barInset, width: Math.max(ppd, X(s.end + 1) - bx), height: T.barHeight };
      }
      const done = isClosed(step) ? 1 : step.status === "in_progress" ? 0.5 : 0;
      rows.push({
        kind: { type: "step", id: step.id },
        y,
        height: T.stepRowHeight,
        nameRect: { x: T.nameIndent, y: y + 4, width: nameWidth - T.nameIndent, height: T.stepRowHeight - 8 },
        bar,
        doneFraction: done,
        span: null,
        diamond: null,
      });
      y += T.stepRowHeight;
    }
  }

  // ── Arrows ─────────────────────────────────────────────────────────────
  const arrows = options.milestonesOnly ? [] : routeArrows(plan.edges, rows);

  const rightmost = Math.max(
    maxX(todayPill),
    ...diamonds.map((d) => maxX(d.rect)),
    ...rows.map((r) => (r.bar ? maxX(r.bar) : r.span ? maxX(r.span) : 0)),
    ...arrows.flatMap((a) => a.points.map((p) => p.x)),
    ...ticks.map((t) => (t.labelRect ? maxX(t.labelRect) : 0)),
  );
  const natural = Math.max(X(start + totalDays), rightmost + 12);

  return {
    options,
    startDay: start,
    endDay: start + totalDays,
    pointsPerDay: ppd,
    contentWidth: Math.max(options.viewportWidth, natural),
    contentHeight: y + 8,
    ticks,
    todayX,
    todayPill,
    diamonds,
    rows,
    arrows,
  };
}

/**
 * Finish-to-start arrows. Each leaves the blocker bar's right end along the
 * blocker's row centerline, turns down (or up) in a column that no bar in
 * between occupies, and enters the blocked bar through its TOP edge at a
 * per-target lane so several arrows into one step land side by side.
 */
function routeArrows(edges: PlanEdge[], rows: TimelineRow[]): TimelineArrow[] {
  const T = TIMELINE;
  const rowIndex = new Map<string, number>();
  rows.forEach((row, i) => {
    if (row.kind.type === "step") rowIndex.set(row.kind.id, i);
  });

  const drawable = edges.filter((e) => {
    const a = rowIndex.get(e.blockerId), b = rowIndex.get(e.blockedId);
    return a !== undefined && b !== undefined && rows[a].bar !== null && rows[b].bar !== null && a !== b;
  });
  // Lane 0 (leftmost) goes to the NEAREST blocker row, outer lanes to farther
  // rows — so a far arrow's vertical passes outside a near arrow's horizontal
  // instead of crossing it.
  const incoming = new Map<string, PlanEdge[]>();
  for (const e of drawable) incoming.set(e.blockedId, [...(incoming.get(e.blockedId) ?? []), e]);
  for (const [key, list] of incoming) {
    const target = rowIndex.get(key) as number;
    const distance = (e: PlanEdge) => Math.abs(target - (rowIndex.get(e.blockerId) as number));
    // Stable: ties keep the plan's edge order.
    incoming.set(key, list.map((e, i) => ({ e, i })).sort((p, q) => distance(p.e) - distance(q.e) || p.i - q.i).map((p) => p.e));
  }

  const verticals: { x: number; y0: number; y1: number }[] = [];
  const arrows: TimelineArrow[] = [];
  for (const e of drawable) {
    const si = rowIndex.get(e.blockerId) as number, ti = rowIndex.get(e.blockedId) as number;
    const target = rows[ti];
    const sb = rows[si].bar as Rect, tb = target.bar as Rect;
    const lane = Math.max(0, (incoming.get(e.blockedId) ?? [e]).indexOf(e));
    // Lane x inside the target bar, left to right; never past its end.
    const laneX = Math.min(tb.x + 6 + lane * 6, Math.max(tb.x + 2, maxX(tb) - 2));
    const sy = midY(sb);
    const tipY = tb.y;
    const approachY = target.y + 3 + Math.min(lane, 2) * 3; // inside the target row's top gap
    const downward = ti > si;
    // Rows the vertical passes through.
    const from = downward ? si + 1 : ti, to = downward ? ti : si;
    const blockedColumns: [number, number][] = [];
    for (let i = from; i < to; i++) {
      const bar = rows[i].bar ?? rows[i].span;
      if (bar) blockedColumns.push([bar.x - T.arrowClearance, maxX(bar) + T.arrowClearance]);
    }
    const yTop = downward ? sy : approachY;
    const yBottom = downward ? approachY : sy;
    const free = (x: number) =>
      !blockedColumns.some(([lo, hi]) => x >= lo && x <= hi) && !verticals.some((v) => Math.abs(v.x - x) < 4 && v.y0 < yBottom && yTop < v.y1);
    const minX = maxX(sb) + 6;
    let points: Point[];
    if (downward && laneX >= minX && free(laneX)) {
      // The simple L: along my row, then straight down into the target's top.
      points = [{ x: maxX(sb), y: sy }, { x: laneX, y: sy }, { x: laneX, y: tipY }];
      verticals.push({ x: laneX, y0: sy, y1: tipY });
    } else {
      let xv = minX;
      while (!free(xv)) xv += 2;
      points = [{ x: maxX(sb), y: sy }, { x: xv, y: sy }, { x: xv, y: approachY }];
      if (Math.abs(xv - laneX) > 0.5) points.push({ x: laneX, y: approachY });
      points.push({ x: laneX, y: tipY });
      verticals.push({ x: xv, y0: Math.min(sy, approachY), y1: Math.max(sy, approachY) });
    }
    const head: Point[] = [{ x: laneX - 3.5, y: tipY - 5 }, { x: laneX + 3.5, y: tipY - 5 }, { x: laneX, y: tipY }];
    arrows.push({ blockerId: e.blockerId, blockedId: e.blockedId, points, head });
  }
  return arrows;
}

/** Where the chart should scroll so today sits a little in from the left. */
export function scrollToToday(layout: Pick<TimelineLayout, "todayX">, viewportWidth: number): number {
  return Math.max(0, layout.todayX - viewportWidth * 0.3);
}
