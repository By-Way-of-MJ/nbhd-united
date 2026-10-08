"use client";

import clsx from "clsx";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { capsuleCls, Diamond, OwnerDot, PillTabs, stepColor, useMediaQuery } from "@/components/neighborhood/project/parts";
import {
  canEditStep,
  type Day,
  dayShort,
  dependentsOf,
  findMilestone,
  findStep,
  isAwaitingYes,
  isWaiting,
  joinNames,
  ownerLabel,
  type PlanStep,
  type ProjectPlan,
  stepState,
  undatedOpenSteps,
  undatedTimelineHint,
  whenLabel,
} from "@/lib/project-plan";
import { layoutTimeline, maxX, maxY, midX, midY, type Rect, scrollToToday, TIMELINE, type TimelineLayout, type TimelineZoom } from "@/lib/timeline-layout";

const diamondPath = (r: Rect) => `M${midX(r)} ${r.y} L${maxX(r)} ${midY(r)} L${midX(r)} ${maxY(r)} L${r.x} ${midY(r)} Z`;
const points = (list: { x: number; y: number }[]) => list.map((p) => `${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" L");

/** What a screen reader hears for a step on the chart. */
function describe(plan: ProjectPlan, step: PlanStep, today: Day): string {
  const parts = [step.title, ownerLabel(plan, step), whenLabel(step) ?? "no dates", stepState(plan, step, today).word];
  const unlocks = dependentsOf(plan, step.id).map((s) => s.title);
  if (unlocks.length) parts.push(`then unlocks ${joinNames(unlocks)}`);
  return parts.join(", ");
}

/**
 * The Gantt: a pinned names column on the left and a chart that scrolls
 * sideways. Everything is placed by `layoutTimeline`; this only draws it.
 */
export function TimelineView({
  plan,
  today,
  onOpenStep,
  onAddDates,
}: {
  plan: ProjectPlan;
  today: Day;
  onOpenStep: (step: PlanStep) => void;
  /** Opens a step's editor so it can get dates. */
  onAddDates: (step: PlanStep) => void;
}) {
  const [zoom, setZoom] = useState<TimelineZoom>("weeks");
  const [milestonesOnly, setMilestonesOnly] = useState(false);
  const [viewport, setViewport] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const clip = useId();
  // Desktop: the mockup's roomy column and 24px days. Phone: the iPhone's.
  const roomy = useMediaQuery("(min-width: 768px)");
  const nameColumnWidth = roomy ? 230 : 116;
  const weekPointsPerDay = roomy ? 24 : 12.3;

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setViewport(el.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const layout: TimelineLayout | null = useMemo(
    () => (viewport > 0 ? layoutTimeline(plan, { zoom, milestonesOnly, viewportWidth: viewport, nameColumnWidth, today, weekPointsPerDay }) : null),
    [plan, zoom, milestonesOnly, viewport, nameColumnWidth, today, weekPointsPerDay],
  );

  // Open with today a little in from the left; the whole project starts at its start.
  const ready = viewport > 0;
  const todayX = layout?.todayX ?? 0;
  const scrollKey = `${zoom}:${ready}:${roomy}`;
  const scrolledFor = useRef("");
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !ready || scrolledFor.current === scrollKey) return;
    scrolledFor.current = scrollKey;
    el.scrollLeft = zoom === "weeks" ? scrollToToday({ todayX }, el.clientWidth) : 0;
  }, [scrollKey, ready, zoom, todayX]);

  const undated = undatedOpenSteps(plan);
  const hint = undatedTimelineHint(plan);
  const firstEditable = undated.find((s) => canEditStep(plan, s));
  const scrolls = !!layout && layout.contentWidth > viewport + 1;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
        <PillTabs
          small
          label="Zoom"
          value={zoom}
          onChange={setZoom}
          options={[
            { value: "weeks", label: "Weeks" },
            { value: "whole", label: "Whole project" },
          ]}
        />
        <button
          type="button"
          aria-pressed={milestonesOnly}
          onClick={() => setMilestonesOnly((v) => !v)}
          className={clsx(
            "os-focus inline-flex min-h-[40px] items-center gap-2 rounded-full border px-4 text-[0.8125rem] transition",
            milestonesOnly ? "border-os-ring bg-[rgba(226,232,240,0.12)] text-white" : "border-os-hairline text-os-faint hover:text-os-ink",
          )}
        >
          <Diamond reached={false} size={10} active={milestonesOnly} /> Milestones only
        </button>
        {hint ? (
          <span className="flex min-w-[220px] flex-1 flex-wrap items-center justify-end gap-x-3 gap-y-1.5">
            <span className="text-[0.8125rem] text-os-faint">{hint}</span>
            {firstEditable ? (
              <button type="button" className={capsuleCls} onClick={() => onAddDates(firstEditable)}>
                Add dates
              </button>
            ) : null}
          </span>
        ) : null}
      </div>

      <div className="flex border-t border-os-hairline">
        {/* Pinned names column. */}
        <div className="relative shrink-0 border-r border-os-hairline" style={{ width: nameColumnWidth, height: layout?.contentHeight ?? 220 }}>
          {plan.milestones.length ? (
            <span className="absolute left-0 text-[0.625rem] text-os-faint" style={{ top: TIMELINE.diamondRowY - 1 }}>
              Milestones
            </span>
          ) : null}
          {layout?.rows.map((row, i) => {
            const box = { left: row.nameRect.x, top: row.nameRect.y, width: row.nameRect.width, height: row.nameRect.height };
            if (row.kind.type === "milestone") {
              const m = findMilestone(plan, row.kind.id);
              if (!m) return null;
              return (
                <div key={`m-${m.id}`} className="absolute flex flex-col justify-center" style={box}>
                  <span className="os-serif truncate text-[1.0625rem] leading-tight text-white md:text-[1.125rem]" title={m.title}>
                    {m.title}
                  </span>
                  {m.target !== null ? <span className="text-[0.625rem] leading-tight text-os-faint">{dayShort(m.target)}</span> : null}
                </div>
              );
            }
            if (row.kind.type === "loose") {
              return (
                <div key={`loose-${i}`} className="absolute flex items-center" style={box}>
                  <span className="os-serif truncate text-[1.0625rem] leading-tight text-white md:text-[1.125rem]">Other steps</span>
                </div>
              );
            }
            const step = findStep(plan, row.kind.id);
            if (!step) return null;
            return (
              <div key={`s-${step.id}`} className="absolute" style={{ left: 0, top: row.nameRect.y, width: row.nameRect.x + row.nameRect.width, height: row.nameRect.height }}>
                <button type="button" onClick={() => onOpenStep(step)} aria-label={describe(plan, step, today)} className="os-focus flex h-full w-full items-center gap-1.5 rounded text-left">
                  <OwnerDot plan={plan} step={step} size={18} />
                  <span className="flex min-w-0 flex-col">
                    <span
                      className={clsx("text-[0.75rem] leading-[0.9375rem] md:truncate md:text-[0.8125rem]", !roomy && "line-clamp-2", step.status === "done" ? "text-os-faint" : "text-os-ink")}
                      title={step.title}
                    >
                      {step.title}
                    </span>
                    {roomy && !row.bar ? <span className="text-[0.625rem] leading-tight text-os-faint">no dates</span> : null}
                  </span>
                </button>
              </div>
            );
          })}
        </div>

        {/* The chart, scrolling sideways under the pinned names. */}
        <div ref={scrollRef} className="min-w-0 flex-1 overflow-x-auto overflow-y-hidden [scrollbar-width:thin]" tabIndex={scrolls ? 0 : undefined} aria-label={scrolls ? "Timeline chart, scrolls sideways" : undefined}>
          {layout ? (
            <div className="relative" style={{ width: layout.contentWidth, height: layout.contentHeight }}>
              <svg width={layout.contentWidth} height={layout.contentHeight} viewBox={`0 0 ${layout.contentWidth} ${layout.contentHeight}`} role="img" aria-label="Timeline of the project’s steps" className="block">
                {/* Week lines + labels. */}
                {layout.ticks.map((tick, i) => (
                  <g key={i}>
                    <line x1={tick.x} y1={0} x2={tick.x} y2={layout.contentHeight} style={{ stroke: "var(--os-hairline)" }} strokeOpacity={0.6} />
                    {tick.labelRect ? (
                      <text x={tick.labelRect.x} y={tick.labelRect.y + 10} fontSize={10} style={{ fill: "var(--os-faint)" }}>
                        {tick.label}
                      </text>
                    ) : null}
                  </g>
                ))}
                {/* Today: a pill on its own header row, a faint line behind the bars. */}
                <line x1={layout.todayX} y1={maxY(layout.todayPill) + 3} x2={layout.todayX} y2={layout.contentHeight} style={{ stroke: "var(--os-ring)" }} />
                <rect x={layout.todayPill.x} y={layout.todayPill.y} width={layout.todayPill.width} height={layout.todayPill.height} rx={layout.todayPill.height / 2} fill="#fff" />
                <text x={midX(layout.todayPill)} y={layout.todayPill.y + 10.5} textAnchor="middle" fontSize={9} fontWeight={700} style={{ fill: "var(--os-sky)" }}>
                  Today
                </text>
                {layout.diamonds.map((d) => {
                  const m = findMilestone(plan, d.milestoneId);
                  return (
                    <path key={d.milestoneId} d={diamondPath(d.rect)} strokeWidth={1.5} style={{ fill: d.reached ? "var(--os-done)" : "var(--os-sky)", stroke: d.reached ? "var(--os-done)" : "var(--os-accent)" }}>
                      <title>{m ? `${m.title}${m.target !== null ? ` · ${dayShort(m.target)}` : ""}` : "Milestone"}</title>
                    </path>
                  );
                })}
                {/* Rows. */}
                {layout.rows.map((row, i) => {
                  if (row.kind.type === "milestone") {
                    const m = findMilestone(plan, row.kind.id);
                    return (
                      <g key={`m-${row.kind.id}`}>
                        {row.span ? <rect x={row.span.x} y={row.span.y} width={row.span.width} height={row.span.height} rx={2} style={{ fill: "var(--os-accent-line)" }} /> : null}
                        {row.diamond ? (
                          <path d={diamondPath(row.diamond)} strokeWidth={1.5} style={{ fill: m?.reached ? "var(--os-done)" : "var(--os-sky)", stroke: m?.reached ? "var(--os-done)" : "var(--os-accent)" }} />
                        ) : null}
                      </g>
                    );
                  }
                  if (row.kind.type !== "step" || !row.bar) return null;
                  const step = findStep(plan, row.kind.id);
                  if (!step) return null;
                  const bar = row.bar;
                  const color = stepColor(plan, step);
                  const dashed = isWaiting(step) || isAwaitingYes(plan, step);
                  const id = `${clip}-${i}`;
                  return (
                    <g key={`s-${step.id}`}>
                      <clipPath id={id}>
                        <rect x={bar.x} y={bar.y} width={bar.width} height={bar.height} rx={bar.height / 2} />
                      </clipPath>
                      <rect x={bar.x} y={bar.y} width={bar.width} height={bar.height} rx={bar.height / 2} style={{ fill: "var(--os-surface-solid)" }} />
                      {row.doneFraction > 0 ? <rect x={bar.x} y={bar.y} width={bar.width * row.doneFraction} height={bar.height} clipPath={`url(#${id})`} style={{ fill: color }} /> : null}
                      <rect
                        x={bar.x + 0.75}
                        y={bar.y + 0.75}
                        width={Math.max(0, bar.width - 1.5)}
                        height={bar.height - 1.5}
                        rx={(bar.height - 1.5) / 2}
                        fill="none"
                        strokeWidth={1.5}
                        strokeDasharray={dashed ? "3 3" : undefined}
                        style={{ stroke: color }}
                      />
                    </g>
                  );
                })}
                {/* Arrows last, so their tips sit on the bars' top edges. */}
                {layout.arrows.map((arrow) => {
                  const tip = arrow.points[arrow.points.length - 1];
                  return (
                    <g key={`${arrow.blockerId}-${arrow.blockedId}`}>
                      <path d={`M${points([...arrow.points.slice(0, -1), { x: tip.x, y: tip.y - 5 }])}`} fill="none" strokeWidth={1.2} strokeOpacity={0.8} style={{ stroke: "var(--os-faint)" }} />
                      <path d={`M${points(arrow.head)} Z`} style={{ fill: "var(--os-muted)" }} />
                    </g>
                  );
                })}
              </svg>
              {/* Click targets over each bar (the drawing itself isn't interactive). */}
              {layout.rows.map((row) => {
                if (row.kind.type !== "step" || !row.bar) return null;
                const step = findStep(plan, row.kind.id);
                if (!step) return null;
                const width = Math.max(row.bar.width, 32);
                return (
                  <button
                    key={`hit-${step.id}`}
                    type="button"
                    onClick={() => onOpenStep(step)}
                    aria-label={describe(plan, step, today)}
                    title={`${step.title} · ${whenLabel(step) ?? ""}`}
                    className="os-focus absolute rounded-full"
                    style={{ left: Math.max(0, row.bar.x - (width - row.bar.width) / 2), top: row.bar.y - 5, width, height: row.bar.height + 10 }}
                  />
                );
              })}
            </div>
          ) : (
            <div style={{ height: 220 }} />
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-[0.75rem] text-os-faint">
        <span className="flex items-center gap-1.5">
          <span className="h-2.5 w-[22px] rounded-full bg-os-done" aria-hidden="true" /> done
        </span>
        <span className="flex items-center gap-1.5">
          <span className="relative h-2.5 w-[22px] overflow-hidden rounded-full border-[1.5px] border-os-accent" aria-hidden="true">
            <span className="absolute inset-y-0 left-0 w-1/2 bg-os-accent" />
          </span>
          in progress
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2.5 w-[22px] rounded-full border-[1.5px] border-dashed border-os-muted" aria-hidden="true" /> asked, or waiting on another step
        </span>
        <span className="flex items-center gap-1.5">
          <svg width="16" height="10" viewBox="0 0 16 10" aria-hidden="true">
            <path d="M0 5h11" fill="none" strokeWidth="1.2" style={{ stroke: "var(--os-faint)" }} />
            <path d="M10 1.5 L15 5 L10 8.5 Z" style={{ fill: "var(--os-muted)" }} />
          </svg>
          has to finish first
        </span>
        <span className="flex items-center gap-1.5">
          <Diamond reached={false} size={10} /> milestone
        </span>
        {scrolls ? <span className="ml-auto text-os-faint">Scroll the chart sideways to see more.</span> : zoom === "whole" ? <span className="ml-auto">The whole project on one screen.</span> : null}
      </div>
    </div>
  );
}
