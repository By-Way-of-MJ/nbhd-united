/**
 * Small plan builders shared by the project specs (and nothing else): the
 * mockup's garden plan, a dense pseudo-random plan, and a fan-in. Day offsets
 * are from `ORIGIN`, a Monday. Mirrors `PlanBuilder` in the iPhone's
 * ProjectsV2Tests.swift so both suites exercise the same shapes.
 */

import { type Day, dayIso, parseDay, parsePlan, type ProjectPlan } from "./project-plan";
import type { PlanAssignmentStatus, PlanStepStatus, ProjectPlanData } from "./types";

export const ORIGIN: Day = parseDay("2026-10-05") as Day;

export interface StepSeed {
  id: string;
  milestone?: string | null;
  start?: number | null;
  end?: number | null;
  status?: PlanStepStatus;
  owners?: string[];
  /** People asked who haven't answered. */
  asked?: string[];
  title?: string;
}

const iso = (offset: number | null | undefined) => (offset === null || offset === undefined ? null : dayIso(ORIGIN + offset));

export function buildPlanData(input: {
  milestones: [string, number | null][];
  steps: StepSeed[];
  edges: [string, string][];
  members?: string[];
  role?: string;
}): ProjectPlanData {
  const members = input.members ?? ["mb-you", "mb-sam", "mb-rin"];
  const statusOf = (id: string) => input.steps.find((s) => s.id === id)?.status ?? "open";
  return {
    mission_id: "m1",
    title: "Garden",
    description: "Beds",
    my_membership_id: "mb-you",
    my_role: input.role ?? "owner",
    members: members.map((id) => ({
      id,
      handle: id.slice(3),
      display_name: id === "mb-you" ? "You" : id.charAt(3).toUpperCase() + id.slice(4),
      hue: 200,
      role: id === "mb-you" ? "owner" : "member",
      status: "active",
    })),
    milestones: input.milestones.map(([id, day], i) => {
      const mine = input.steps.filter((s) => s.milestone === id);
      const done = mine.filter((s) => s.status === "done").length;
      return { id, title: `M ${id}`, target_date: iso(day), order: i, reached_at: mine.length > 0 && done === mine.length ? "2026-01-01T00:00:00Z" : null, done_count: done, total: mine.length };
    }),
    steps: input.steps.map((s, index) => {
      const owners = s.owners ?? (s.asked ? [] : ["mb-you"]);
      const assignments = [
        ...owners.map((o) => ({ id: `a-${s.id}-${o}`, membership_id: o, status: "accepted" as PlanAssignmentStatus })),
        ...(s.asked ?? []).map((o) => ({ id: `a-${s.id}-${o}`, membership_id: o, status: "asked" as PlanAssignmentStatus })),
      ];
      const parents = input.edges.filter((e) => e[1] === s.id).map((e) => e[0]);
      return {
        id: s.id,
        milestone_id: s.milestone ?? null,
        title: s.title ?? `Step ${s.id}`,
        start_date: iso(s.start),
        due_date: iso(s.end),
        status: s.status ?? "open",
        order: index,
        version: 0,
        assignments,
        owners: owners.map((id) => ({ id })),
        blocked_by_open: parents.filter((p) => statusOf(p) !== "done"),
      };
    }),
    edges: input.edges.map(([a, b], i) => ({ id: `e${i}`, blocker_id: a, blocked_id: b })),
    done_count: input.steps.filter((s) => s.status === "done").length,
    total: input.steps.length,
  };
}

export function buildPlan(input: Parameters<typeof buildPlanData>[0]): ProjectPlan {
  return parsePlan(buildPlanData(input)) as ProjectPlan;
}

/** The mockup's garden plan (today = ORIGIN + 9). */
export function gardenPlan(): ProjectPlan {
  return buildPlan({
    milestones: [["plan", 6], ["beds", 20], ["plant", 34]],
    steps: [
      { id: "measure", milestone: "plan", start: 0, end: 2, status: "done", owners: ["mb-rin"] },
      { id: "layout", milestone: "plan", start: 3, end: 5, status: "done" },
      { id: "buy", milestone: "beds", start: 7, end: 10, status: "in_progress", owners: ["mb-sam"] },
      { id: "frames", milestone: "beds", start: 11, end: 19, owners: ["mb-you", "mb-sam"] },
      { id: "seed", milestone: "plant", start: 14, end: 25, owners: ["mb-rin"] },
      { id: "plant", milestone: "plant", start: 32, end: 34, owners: ["mb-you", "mb-sam", "mb-rin"] },
    ],
    edges: [["measure", "layout"], ["layout", "buy"], ["buy", "frames"], ["frames", "plant"], ["seed", "plant"]],
  });
}

/** mulberry32 — a tiny deterministic generator, so the dense plan never changes. */
function rng(seed: number): (n: number) => number {
  let a = seed >>> 0;
  return (n: number) => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) % n;
  };
}

/**
 * 40 steps across 5 milestones with pseudo-random dates and dependencies,
 * including backward (upward) edges and overlapping dates.
 */
export function densePlan(seed = 42): ProjectPlan {
  const next = rng(seed);
  const milestones: [string, number][] = Array.from({ length: 5 }, (_, i) => [`m${i}`, 10 + i * 12]);
  const steps: StepSeed[] = [];
  for (let i = 0; i < 40; i++) {
    const start = next(60);
    steps.push({ id: `s${i}`, milestone: `m${i % 5}`, start, end: start + next(10), owners: [["mb-you", "mb-sam", "mb-rin"][next(3)]] });
  }
  const edges: [string, string][] = [];
  const seen = new Set<string>();
  for (let i = 0; i < 45; i++) {
    const a = next(40), b = next(40);
    if (a === b || seen.has(`${a}>${b}`) || seen.has(`${b}>${a}`)) continue;
    seen.add(`${a}>${b}`);
    edges.push([`s${a}`, `s${b}`]);
  }
  return buildPlan({ milestones, steps, edges });
}

/** Five blockers all feeding one short step. */
export function fanInPlan(): ProjectPlan {
  return buildPlan({
    milestones: [["a", 20]],
    steps: [...Array.from({ length: 5 }, (_, i) => ({ id: `b${i}`, milestone: "a", start: i * 2, end: i * 2 + 3 })), { id: "target", milestone: "a", start: 14, end: 15 }],
    edges: Array.from({ length: 5 }, (_, i) => [`b${i}`, "target"] as [string, string]),
  });
}
