// Spec for the project timeline's geometry — runnable with Node's built-in
// runner after a tsc transpile. The "nothing overlapping" rule is checked as a
// property over several plan shapes, at both zooms, with and without
// "Milestones only", at a phone's width and a desktop's.
import { test } from "node:test";
import assert from "node:assert/strict";

import { buildPlan, densePlan, fanInPlan, gardenPlan, ORIGIN } from "./project-plan-fixtures";
import type { ProjectPlan } from "./project-plan";
import { DEFAULT_TIMELINE_OPTIONS, layoutTimeline, maxX, maxY, type Point, type Rect, scrollToToday, TIMELINE, type TimelineLayout, type TimelineZoom } from "./timeline-layout";

const TODAY = ORIGIN + 9;

const PLANS: [string, ProjectPlan][] = [
  ["garden", gardenPlan()],
  ["dense", densePlan()],
  ["dense-7", densePlan(7)],
  ["dense-1234", densePlan(1234)],
  ["fanIn", fanInPlan()],
];

// The phone (iPhone portrait) and the desktop page's measurements.
const SIZES: [string, { viewportWidth: number; nameColumnWidth: number; weekPointsPerDay: number }][] = [
  ["phone", { viewportWidth: 258, nameColumnWidth: 100, weekPointsPerDay: 12.3 }],
  ["desktop", { viewportWidth: 760, nameColumnWidth: 230, weekPointsPerDay: 24 }],
];

function layouts(): [string, TimelineLayout][] {
  const out: [string, TimelineLayout][] = [];
  for (const [name, plan] of PLANS) {
    for (const [size, dims] of SIZES) {
      for (const zoom of ["weeks", "whole"] as TimelineZoom[]) {
        for (const milestonesOnly of [false, true]) {
          out.push([`${name}-${size}-${zoom}-${milestonesOnly ? "ms" : "steps"}`, layoutTimeline(plan, { ...dims, zoom, milestonesOnly, today: TODAY })]);
        }
      }
    }
  }
  return out;
}

const inset = (r: Rect, d: number): Rect => ({ x: r.x + d, y: r.y + d, width: r.width - 2 * d, height: r.height - 2 * d });
const intersects = (a: Rect, b: Rect) => a.x < maxX(b) && b.x < maxX(a) && a.y < maxY(b) && b.y < maxY(a);

/** Axis-aligned segment vs rect (open interior). */
function segmentHits(a: Point, b: Point, r: Rect): boolean {
  const seg: Rect = { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(a.x - b.x), height: Math.abs(a.y - b.y) };
  const interior = inset(r, 0.5);
  // Degenerate (zero-width) segments still intersect when the line passes through.
  return (
    (seg.x < maxX(interior) && maxX(seg) > interior.x && seg.y < maxY(interior) && maxY(seg) > interior.y) ||
    (seg.width === 0 && seg.x > interior.x && seg.x < maxX(interior) && seg.y < maxY(interior) && maxY(seg) > interior.y) ||
    (seg.height === 0 && seg.y > interior.y && seg.y < maxY(interior) && seg.x < maxX(interior) && maxX(seg) > interior.x)
  );
}

const pairs = <T,>(list: T[]): [T, T][] => list.slice(1).map((b, i) => [list[i], b]);

test("timeline: no arrow crosses any bar", () => {
  for (const [name, layout] of layouts()) {
    const bars = layout.rows.map((r) => r.bar ?? r.span).filter((r): r is Rect => !!r);
    for (const arrow of layout.arrows) {
      for (const [a, b] of pairs(arrow.points)) {
        for (const bar of bars) assert.ok(!segmentHits(a, b, bar), `${name}: arrow ${arrow.blockerId}→${arrow.blockedId} crosses a bar`);
      }
    }
  }
});

test("timeline: every arrow ends on its target's top edge, inside the chart", () => {
  let drawn = 0;
  for (const [name, layout] of layouts()) {
    for (const arrow of layout.arrows) {
      drawn++;
      const target = layout.rows.find((r) => r.kind.type === "step" && r.kind.id === arrow.blockedId)?.bar;
      const tip = arrow.points[arrow.points.length - 1];
      assert.ok(target, `${name}: missing target`);
      assert.ok(Math.abs(tip.y - target.y) < 0.01, `${name}: tip isn't on the bar's top edge`);
      assert.ok(tip.x >= target.x && tip.x <= maxX(target), `${name}: tip outside its bar`);
      assert.ok(arrow.points.every((p) => p.x >= 0), `${name}: arrow leaves the chart into the names column`);
      assert.ok(arrow.points.every((p) => p.x <= layout.contentWidth), `${name}: arrow runs past the chart's right edge`);
      // The head is a triangle whose tip is the arrow's last point.
      assert.deepEqual(arrow.head[2], tip);
      // Every segment is horizontal or vertical — no diagonals to clip a bar's corner.
      for (const [a, b] of pairs(arrow.points)) assert.ok(a.x === b.x || a.y === b.y, `${name}: diagonal arrow segment`);
    }
  }
  assert.ok(drawn > 100, "the dense plans draw plenty of arrows");
});

test("timeline: arrows start at the blocker bar's right end, on its centerline", () => {
  for (const [name, layout] of layouts()) {
    for (const arrow of layout.arrows) {
      const source = layout.rows.find((r) => r.kind.type === "step" && r.kind.id === arrow.blockerId)?.bar;
      assert.ok(source, `${name}: missing source`);
      assert.ok(Math.abs(arrow.points[0].x - maxX(source)) < 0.01 && Math.abs(arrow.points[0].y - (source.y + source.height / 2)) < 0.01, `${name}: arrow doesn't leave its blocker's end`);
    }
  }
});

test("timeline: header elements never overlap", () => {
  for (const [name, layout] of layouts()) {
    const rects: Rect[] = [...layout.ticks.map((t) => t.labelRect).filter((r): r is Rect => !!r), ...layout.diamonds.map((d) => d.rect), layout.todayPill];
    for (let i = 0; i < rects.length; i++) {
      for (let j = i + 1; j < rects.length; j++) assert.ok(!intersects(rects[i], rects[j]), `${name}: header elements ${i} and ${j} overlap`);
    }
    for (const rect of rects) assert.ok(maxY(rect) <= TIMELINE.headerHeight, `${name}: header spills into rows`);
  }
});

test("timeline: week labels keep a gap between them", () => {
  for (const [name, layout] of layouts()) {
    const labels = layout.ticks.map((t) => t.labelRect).filter((r): r is Rect => !!r);
    for (const [a, b] of pairs(labels)) assert.ok(b.x >= maxX(a) + 6, `${name}: two week labels touch`);
    assert.ok(labels.length > 0, `${name}: no week labels at all`);
  }
});

test("timeline: names stay in their column and rows don't overlap", () => {
  for (const [name, layout] of layouts()) {
    for (const row of layout.rows) {
      assert.ok(maxX(row.nameRect) <= layout.options.nameColumnWidth, `${name}: name text runs into the chart`);
      assert.ok(row.nameRect.x >= 0 && row.nameRect.width > 0, `${name}: name has no room`);
      assert.ok(row.nameRect.y >= row.y && maxY(row.nameRect) <= row.y + row.height, `${name}: name spills out of its row`);
      if (row.bar) assert.ok(row.bar.y >= row.y && maxY(row.bar) <= row.y + row.height, `${name}: bar spills out of its row`);
      if (row.diamond) assert.ok(row.diamond.y >= row.y && maxY(row.diamond) <= row.y + row.height, `${name}: diamond spills out of its row`);
    }
    for (const [a, b] of pairs(layout.rows)) assert.ok(a.y + a.height <= b.y + 0.01, `${name}: rows overlap`);
    assert.ok(layout.rows.every((r) => r.y >= TIMELINE.headerHeight), `${name}: a row sits under the header`);
  }
});

test("timeline: bars and diamonds stay inside the chart's width", () => {
  for (const [name, layout] of layouts()) {
    for (const row of layout.rows) {
      for (const rect of [row.bar, row.span, row.diamond]) {
        if (rect) assert.ok(rect.x >= 0 && maxX(rect) <= layout.contentWidth + 0.01, `${name}: a bar runs off the chart`);
      }
    }
    assert.ok(maxX(layout.todayPill) <= layout.contentWidth && layout.todayPill.x >= 0, `${name}: Today runs off the chart`);
    assert.ok(layout.contentWidth >= layout.options.viewportWidth, `${name}: chart is narrower than its viewport`);
  }
});

test("timeline: fan-in arrows land in separate lanes without crossing", () => {
  const layout = layoutTimeline(fanInPlan(), { ...DEFAULT_TIMELINE_OPTIONS, today: TODAY });
  const tips = layout.arrows.map((a) => a.points[a.points.length - 1].x);
  assert.equal(tips.length, 5);
  const target = layout.rows.find((r) => r.kind.type === "step" && r.kind.id === "target")?.bar as Rect;
  assert.ok(new Set(tips.map((x) => Math.round(x))).size === tips.length || target.width < 30);
  // At the desktop's scale the bar is wide enough for five distinct lanes.
  const wide = layoutTimeline(fanInPlan(), { ...DEFAULT_TIMELINE_OPTIONS, viewportWidth: 760, nameColumnWidth: 230, weekPointsPerDay: 24, today: TODAY });
  assert.equal(new Set(wide.arrows.map((a) => Math.round(a.points[a.points.length - 1].x))).size, 5);
});

test("timeline: the garden's arrows don't cross each other", () => {
  for (const [, dims] of SIZES) {
    const layout = layoutTimeline(gardenPlan(), { ...dims, zoom: "weeks", milestonesOnly: false, today: TODAY });
    assert.equal(layout.arrows.length, 5);
    const segments = layout.arrows.flatMap((a, i) => pairs(a.points).map(([p, q]) => ({ i, p, q })));
    const box = (p: Point, q: Point): Rect => ({ x: Math.min(p.x, q.x), y: Math.min(p.y, q.y), width: Math.abs(p.x - q.x), height: Math.abs(p.y - q.y) });
    for (const x of segments) {
      for (const y of segments) {
        if (y.i <= x.i) continue;
        assert.ok(!intersects(box(x.p, x.q), box(y.p, y.q)), `garden arrows ${x.i} and ${y.i} cross`);
      }
    }
  }
});

test("timeline: whole-project zoom fits the viewport", () => {
  assert.ok(layoutTimeline(gardenPlan(), { ...DEFAULT_TIMELINE_OPTIONS, zoom: "whole", today: TODAY }).contentWidth <= 258.5);
  assert.ok(layoutTimeline(gardenPlan(), { ...DEFAULT_TIMELINE_OPTIONS, zoom: "whole", viewportWidth: 760, today: TODAY }).contentWidth <= 760.5);
});

test("timeline: the chart starts on a Monday and always shows two weeks", () => {
  const layout = layoutTimeline(gardenPlan(), { ...DEFAULT_TIMELINE_OPTIONS, today: TODAY });
  assert.equal(layout.startDay, ORIGIN, "2026-10-05 is a Monday");
  assert.equal(layout.ticks[0].label, "Oct 5");
  assert.equal(layout.ticks[1].label, "Oct 12");
  const solo = buildPlan({ milestones: [], steps: [{ id: "talk", start: null, end: null }], edges: [] });
  const empty = layoutTimeline(solo, { ...DEFAULT_TIMELINE_OPTIONS, today: ORIGIN + 2 });
  assert.equal(empty.endDay - empty.startDay, 14);
  assert.equal(empty.rows.filter((r) => r.bar).length, 0, "an undated step has a row but no bar");
  assert.equal(empty.rows.length, 2, "its row sits under an Other steps heading");
});

test("timeline: bars span their dates; done fills, in progress half-fills", () => {
  const layout = layoutTimeline(gardenPlan(), { ...DEFAULT_TIMELINE_OPTIONS, today: TODAY });
  const row = (id: string) => layout.rows.find((r) => r.kind.type === "step" && r.kind.id === id);
  const buy = row("buy");
  assert.ok(buy?.bar);
  assert.ok(Math.abs(buy.bar.x - (TIMELINE.leftPad + 7 * 12.3)) < 0.01);
  assert.ok(Math.abs(buy.bar.width - 4 * 12.3) < 0.01, "Oct 12 – 15 is four days wide");
  assert.equal(buy.doneFraction, 0.5);
  assert.equal(row("measure")?.doneFraction, 1);
  assert.equal(row("frames")?.doneFraction, 0);
  assert.ok(Math.abs(layout.todayX - (TIMELINE.leftPad + 9 * 12.3)) < 0.01);
});

test("timeline: milestones only shows one span and diamond per milestone, and no arrows", () => {
  const layout = layoutTimeline(gardenPlan(), { ...DEFAULT_TIMELINE_OPTIONS, milestonesOnly: true, today: TODAY });
  assert.equal(layout.rows.length, 3);
  assert.ok(layout.rows.every((r) => r.kind.type === "milestone" && r.span && r.diamond && !r.bar));
  assert.equal(layout.arrows.length, 0);
  const all = layoutTimeline(gardenPlan(), { ...DEFAULT_TIMELINE_OPTIONS, today: TODAY });
  assert.equal(all.rows.length, 9, "three milestone headings + six steps");
  assert.equal(all.diamonds.length, 3);
});

test("timeline: milestones in the same week sit side by side", () => {
  const plan = buildPlan({ milestones: [["a", 8], ["b", 8], ["c", 9]], steps: [{ id: "x", milestone: "a", start: 0, end: 3 }], edges: [] });
  for (const zoom of ["weeks", "whole"] as TimelineZoom[]) {
    const { diamonds } = layoutTimeline(plan, { ...DEFAULT_TIMELINE_OPTIONS, zoom, today: TODAY });
    for (const [a, b] of pairs(diamonds)) assert.ok(b.rect.x >= maxX(a.rect) + 2, `${zoom}: two diamonds touch`);
  }
});

test("timeline: opens with today a little in from the left", () => {
  const layout = layoutTimeline(gardenPlan(), { ...DEFAULT_TIMELINE_OPTIONS, today: TODAY });
  assert.ok(Math.abs(scrollToToday(layout, 258) - (layout.todayX - 77.4)) < 0.01);
  assert.equal(scrollToToday({ todayX: 10 }, 258), 0, "never scrolls left of the start");
});
