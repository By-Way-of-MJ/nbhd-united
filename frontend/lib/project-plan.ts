/**
 * Pure model behind the project page (Projects v2, apps/friends/PROJECTS_V2.md).
 * Mirrors the iPhone's ProjectModels/ProjectStyle so both apps say the same
 * thing about the same plan: who has a step, when, what it waits on, and the
 * small word at the end of each row. No DOM, no fetching.
 */

import type { PlanAssignmentStatus, PlanHealth, PlanStepStatus, ProjectPlanData } from "./types";

// ── Calendar days ─────────────────────────────────────────────────────────
// The backend sends plan dates as YYYY-MM-DD and every schedule rule is
// whole-day math, so a day is just a count of days since 1970-01-01.

export type Day = number;

const DAY_MS = 86_400_000;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function parseDay(iso: string | null | undefined): Day | null {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return null;
  const month = Number(m[2]), day = Number(m[3]);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  return Math.floor(Date.UTC(Number(m[1]), month - 1, day) / DAY_MS);
}

function utc(day: Day): Date {
  return new Date(day * DAY_MS);
}

export function dayIso(day: Day): string {
  const d = utc(day);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
}

/** "Oct 5" */
export function dayShort(day: Day): string {
  const d = utc(day);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

/** "Mon, Oct 12" */
export function dayLong(day: Day): string {
  const d = utc(day);
  return `${WEEKDAYS[d.getUTCDay()]}, ${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

export function dayOfMonth(day: Day): number {
  return utc(day).getUTCDate();
}

export function monthOf(day: Day): number {
  return utc(day).getUTCMonth();
}

/** The viewer's own calendar day (their clock, not UTC's). */
export function localToday(now: Date = new Date()): Day {
  return Math.floor(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / DAY_MS);
}

// ── Model ─────────────────────────────────────────────────────────────────

export interface PlanMember {
  /** Membership id — the only identity the plan carries. */
  id: string;
  handle: string | null;
  displayName: string;
  hue: number;
  role: string;
  status: string;
  /** My own Horizons goal this project serves (only ever on my membership). */
  linkedGoalId: string | null;
  linkedGoalTitle: string | null;
}

export interface PlanAssignment {
  id: string;
  membershipId: string;
  status: PlanAssignmentStatus;
  counterStart: Day | null;
  counterDue: Day | null;
  note: string;
}

export interface PlanMilestone {
  id: string;
  title: string;
  target: Day | null;
  order: number;
  reached: boolean;
  doneCount: number;
  total: number;
}

export interface PlanStep {
  id: string;
  milestoneId: string | null;
  title: string;
  description: string;
  start: Day | null;
  due: Day | null;
  status: PlanStepStatus;
  order: number;
  version: number;
  assignments: PlanAssignment[];
  /** Accepted, active owners (server-computed). */
  ownerIds: string[];
  blockedByOpen: string[];
  slackDays: number | null;
  movesIfLate: string[];
}

export interface PlanEdge {
  id: string;
  blockerId: string;
  blockedId: string;
}

export interface ProjectPlan {
  missionId: string;
  title: string;
  goal: string;
  status: string;
  /** The project's own version — renames send it back (409 if someone got there first). */
  version: number;
  target: Day | null;
  members: PlanMember[];
  milestones: PlanMilestone[];
  steps: PlanStep[];
  edges: PlanEdge[];
  health: PlanHealth;
  doneCount: number;
  total: number;
  myMembershipId: string;
  myRole: string;
  /** Only the person who started the project (while an owner) can add people. */
  canInvite: boolean;
}

const BIG = Number.MAX_SAFE_INTEGER;

export function parsePlan(data: ProjectPlanData | null | undefined): ProjectPlan | null {
  if (!data || !data.mission_id || !data.my_membership_id) return null;
  const steps: PlanStep[] = (data.steps ?? []).map((s) => ({
    id: String(s.id),
    milestoneId: s.milestone_id ? String(s.milestone_id) : null,
    title: s.title || "Step",
    description: s.description ?? "",
    start: parseDay(s.start_date),
    due: parseDay(s.due_date),
    status: (["open", "in_progress", "done", "skipped"] as PlanStepStatus[]).includes(s.status) ? s.status : "open",
    order: s.order ?? 0,
    version: s.version ?? 0,
    assignments: (s.assignments ?? []).map((a) => ({
      id: String(a.id),
      membershipId: String(a.membership_id),
      status: a.status,
      counterStart: parseDay(a.counter_start),
      counterDue: parseDay(a.counter_due),
      note: a.note ?? "",
    })),
    ownerIds: (s.owners ?? []).map((o) => String(o.id)),
    blockedByOpen: (s.blocked_by_open ?? []).map(String),
    slackDays: typeof s.slack_days === "number" ? s.slack_days : null,
    movesIfLate: (s.moves_if_late ?? []).map(String),
  }));
  const milestones: PlanMilestone[] = (data.milestones ?? [])
    .map((m) => ({
      id: String(m.id),
      title: m.title || "Milestone",
      target: parseDay(m.target_date),
      order: m.order ?? 0,
      reached: !!m.reached_at,
      doneCount: m.done_count ?? 0,
      total: m.total ?? 0,
    }))
    .sort((a, b) => a.order - b.order || (a.target ?? BIG) - (b.target ?? BIG));
  return {
    missionId: String(data.mission_id),
    title: data.title || "Shared project",
    goal: data.description ?? "",
    status: data.status ?? "active",
    version: data.version ?? 0,
    target: parseDay(data.target_date),
    members: (data.members ?? []).map((m) => ({
      id: String(m.id),
      handle: m.handle ?? null,
      displayName: m.display_name || "Neighbor",
      hue: typeof m.hue === "number" ? m.hue : 210,
      role: m.role ?? "member",
      status: m.status ?? "active",
      linkedGoalId: m.linked_goal_id ? String(m.linked_goal_id) : null,
      linkedGoalTitle: m.linked_goal_title ?? null,
    })),
    milestones,
    steps,
    edges: (data.edges ?? []).map((e) => ({ id: String(e.id), blockerId: String(e.blocker_id), blockedId: String(e.blocked_id) })),
    health: data.health === "late" || data.health === "at_risk" ? data.health : "on_track",
    doneCount: data.done_count ?? steps.filter((s) => s.status === "done").length,
    total: data.total ?? steps.length,
    myMembershipId: String(data.my_membership_id),
    myRole: data.my_role ?? "member",
    canInvite: !!data.can_invite,
  };
}

// ── Reading a plan ────────────────────────────────────────────────────────

export function isClosed(step: Pick<PlanStep, "status">): boolean {
  return step.status === "done" || step.status === "skipped";
}

/**
 * The bar's first and last day. A step with only one date is a one-day bar; a
 * step with neither has no bar.
 */
export function stepSpan(step: Pick<PlanStep, "start" | "due">): { start: Day; end: Day } | null {
  const { start, due } = step;
  if (start !== null && due !== null) return { start: Math.min(start, due), end: Math.max(start, due) };
  if (start !== null) return { start, end: start };
  if (due !== null) return { start: due, end: due };
  return null;
}

export interface PlanGroup {
  id: string;
  milestone: PlanMilestone | null;
  steps: PlanStep[];
}

function sortSteps(steps: PlanStep[]): PlanStep[] {
  return [...steps].sort(
    (a, b) => a.order - b.order || (stepSpan(a)?.start ?? BIG) - (stepSpan(b)?.start ?? BIG) || (a.title < b.title ? -1 : a.title > b.title ? 1 : 0),
  );
}

/**
 * Steps in plan order: under their milestone (milestone order), then the steps
 * with no milestone ("Other steps"), each group by (order, start, title).
 */
export function planGroups(plan: ProjectPlan): PlanGroup[] {
  const groups: PlanGroup[] = plan.milestones.map((m) => ({ id: m.id, milestone: m, steps: sortSteps(plan.steps.filter((s) => s.milestoneId === m.id)) }));
  const known = new Set(plan.milestones.map((m) => m.id));
  const loose = plan.steps.filter((s) => !s.milestoneId || !known.has(s.milestoneId));
  if (loose.length) groups.push({ id: "unscheduled", milestone: null, steps: sortSteps(loose) });
  return groups;
}

export function orderedSteps(plan: ProjectPlan): PlanStep[] {
  return planGroups(plan).flatMap((g) => g.steps);
}

export function findStep(plan: ProjectPlan, id: string | null | undefined): PlanStep | undefined {
  return id ? plan.steps.find((s) => s.id === id) : undefined;
}

export function findMember(plan: ProjectPlan, id: string): PlanMember | undefined {
  return plan.members.find((m) => m.id === id);
}

export function findMilestone(plan: ProjectPlan, id: string | null | undefined): PlanMilestone | undefined {
  return id ? plan.milestones.find((m) => m.id === id) : undefined;
}

export function myMember(plan: ProjectPlan): PlanMember | undefined {
  return findMember(plan, plan.myMembershipId);
}

/** Steps that have to finish before this one. */
export function blockersOf(plan: ProjectPlan, stepId: string): PlanStep[] {
  return plan.edges.filter((e) => e.blockedId === stepId).map((e) => findStep(plan, e.blockerId)).filter((s): s is PlanStep => !!s);
}

/** Steps this one unlocks. */
export function dependentsOf(plan: ProjectPlan, stepId: string): PlanStep[] {
  return plan.edges.filter((e) => e.blockerId === stepId).map((e) => findStep(plan, e.blockedId)).filter((s): s is PlanStep => !!s);
}

/**
 * Every step that (directly or further down) waits on this one. None of them
 * can become one of this step's blockers — that would be a loop.
 */
export function downstreamOf(plan: ProjectPlan, stepId: string): Set<string> {
  const seen = new Set<string>();
  const stack = [stepId];
  while (stack.length) {
    const current = stack.pop() as string;
    for (const e of plan.edges) {
      if (e.blockerId === current && !seen.has(e.blockedId)) {
        seen.add(e.blockedId);
        stack.push(e.blockedId);
      }
    }
  }
  return seen;
}

/** Steps with no dates don't appear on the timeline. */
export function undatedOpenSteps(plan: ProjectPlan): PlanStep[] {
  return orderedSteps(plan).filter((s) => stepSpan(s) === null && !isClosed(s));
}

export function isActiveMember(m: PlanMember): boolean {
  return m.status === "active";
}

export function isInvitedMember(m: PlanMember): boolean {
  return m.status === "invited";
}

/** Me first, then everyone active or invited. */
export function people(plan: ProjectPlan): PlanMember[] {
  return [...plan.members.filter((m) => m.id === plan.myMembershipId), ...plan.members.filter((m) => m.id !== plan.myMembershipId && (isActiveMember(m) || isInvitedMember(m)))];
}

export function ownersOf(plan: ProjectPlan, step: PlanStep): PlanMember[] {
  return step.ownerIds.map((id) => findMember(plan, id)).filter((m): m is PlanMember => !!m);
}

export function isOwner(plan: ProjectPlan, step: PlanStep): boolean {
  return step.ownerIds.includes(plan.myMembershipId);
}

/** My own assignment on a step, if anyone asked me. */
export function myAssignment(plan: ProjectPlan, step: PlanStep): PlanAssignment | undefined {
  return step.assignments.find((a) => a.membershipId === plan.myMembershipId);
}

/** Steps somebody asked me to take and I haven't answered. */
export function asksForMe(plan: ProjectPlan): PlanStep[] {
  return plan.steps.filter((s) => myAssignment(plan, s)?.status === "asked");
}

/**
 * Mirrors the backend edit-rights rule closely enough to hide controls the
 * server would refuse. The server stays authoritative: the plan doesn't say who
 * created a step, so a creator-only edit is offered there, not here.
 */
export function canEditStep(plan: ProjectPlan, step: PlanStep): boolean {
  return plan.myRole === "owner" || isOwner(plan, step);
}

/** Only an accepted owner can mark a step done or reopen it. */
export function canCompleteStep(plan: ProjectPlan, step: PlanStep): boolean {
  return isOwner(plan, step);
}

/** Rename the project, change or remove milestones, delete it. */
export function canEditProject(plan: ProjectPlan): boolean {
  return plan.myRole === "owner";
}

export function memberName(plan: ProjectPlan, member: PlanMember): string {
  return member.id === plan.myMembershipId ? "You" : member.displayName;
}

/** "A", "A and B", "A, B, and C". */
export function joinNames(names: string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

/** People who were asked to take a step and haven't answered. */
export function askedOf(plan: ProjectPlan, step: PlanStep): PlanMember[] {
  return step.assignments
    .filter((a) => a.status === "asked")
    .map((a) => findMember(plan, a.membershipId))
    .filter((m): m is PlanMember => !!m);
}

/** "You", "You + Sam", "Everyone", "Asked Kiho", "Anyone". */
export function ownerLabel(plan: ProjectPlan, step: PlanStep): string {
  const owners = ownersOf(plan, step);
  if (owners.length === 0) {
    const asked = askedOf(plan, step);
    return asked.length === 0 ? "Anyone" : `Asked ${asked.map((m) => (m.id === plan.myMembershipId ? "you" : m.displayName)).join(", ")}`;
  }
  const active = plan.members.filter(isActiveMember);
  // "Everyone" only once there are three or more of you; two people read better by name.
  if (owners.length > 2 && owners.length === active.length) return "Everyone";
  const mine = owners.filter((m) => m.id === plan.myMembershipId).map(() => "You");
  const others = owners.filter((m) => m.id !== plan.myMembershipId).map((m) => m.displayName);
  return [...mine, ...others].join(" + ");
}

/** The initial(s) in a step's small owner dot: "Y", "K", "All", "Y+S", "?". */
export function ownerInitials(plan: ProjectPlan, step: PlanStep): string {
  const owners = ownersOf(plan, step);
  const letter = (m: PlanMember) => (m.id === plan.myMembershipId ? "Y" : initialOf(m.displayName));
  if (owners.length === 0) return "?";
  if (owners.length === 1) return letter(owners[0]);
  if (owners.length > 2 && owners.length === plan.members.filter(isActiveMember).length) return "All";
  if (owners.length > 2) return String(owners.length);
  return owners.map(letter).join("+");
}

export function initialOf(name: string): string {
  return name.trim().charAt(0).toLocaleUpperCase() || "?";
}

/** "Oct 12 – 15", "Oct 30 – Nov 2", "Oct 12", or null. */
export function whenLabel(step: Pick<PlanStep, "start" | "due">): string | null {
  const span = stepSpan(step);
  if (!span) return null;
  if (span.start === span.end) return dayShort(span.start);
  return monthOf(span.start) === monthOf(span.end) ? `${dayShort(span.start)} – ${dayOfMonth(span.end)}` : `${dayShort(span.start)} – ${dayShort(span.end)}`;
}

/** "Mon, Oct 12 – Thu, Oct 15", or null. */
export function whenLong(step: Pick<PlanStep, "start" | "due">): string | null {
  const span = stepSpan(step);
  if (!span) return null;
  return span.start === span.end ? dayLong(span.start) : `${dayLong(span.start)} – ${dayLong(span.end)}`;
}

/** Open, with something else still to finish first. */
export function isWaiting(step: PlanStep): boolean {
  return !isClosed(step) && step.blockedByOpen.length > 0;
}

/** Asked of someone, with nobody having said yes yet. */
export function isAwaitingYes(plan: ProjectPlan, step: PlanStep): boolean {
  return !isClosed(step) && ownersOf(plan, step).length === 0 && askedOf(plan, step).length > 0;
}

// ── The words on a row ────────────────────────────────────────────────────

/** `done` is teal, `attn` is amber, the rest are shades of ink. */
export type Tone = "done" | "attn" | "ink" | "muted" | "faint";

export interface StateWord {
  word: string;
  tone: Tone;
}

/**
 * The small word at the end of a step row. One answer per step, most useful
 * first: finished, waiting on me, under way, late, waiting on someone or
 * something, undated, this week, not started.
 */
export function stepState(plan: ProjectPlan, step: PlanStep, today: Day): StateWord {
  if (step.status === "done") return { word: "done", tone: "done" };
  if (step.status === "skipped") return { word: "skipped", tone: "faint" };
  if (myAssignment(plan, step)?.status === "asked") return { word: "asked you", tone: "attn" };
  const overdue = step.due !== null && step.due < today;
  if (step.status === "in_progress") return overdue ? { word: "overdue", tone: "attn" } : { word: "in progress", tone: "ink" };
  if (isAwaitingYes(plan, step)) return { word: `waiting on ${joinNames(askedOf(plan, step).map((m) => m.displayName))}`, tone: "attn" };
  if (overdue) return { word: "overdue", tone: "attn" };
  if (isWaiting(step)) return { word: `after ${step.blockedByOpen.length} ${step.blockedByOpen.length === 1 ? "step" : "steps"}`, tone: "faint" };
  const span = stepSpan(step);
  if (!span) return { word: "no dates", tone: "faint" };
  if (span.start <= today + 6 && span.end >= today) return { word: "this week", tone: "muted" };
  return { word: "not started", tone: "faint" };
}

/** The Status line on a step's own page: one or two plain words. */
export function stepStatus(plan: ProjectPlan, step: PlanStep): StateWord {
  if (step.status === "done") return { word: "Done", tone: "done" };
  if (step.status === "skipped") return { word: "Skipped", tone: "faint" };
  if (step.status === "in_progress") return { word: "In progress", tone: "ink" };
  if (isWaiting(step)) return { word: "Waiting", tone: "attn" };
  if (ownersOf(plan, step).length === 0) return { word: "Needs someone", tone: "attn" };
  return { word: "Not started", tone: "muted" };
}

/** The word on a person's step in the People tab. */
export function personStepState(plan: ProjectPlan, step: PlanStep, memberId: string, today: Day): StateWord {
  if (step.status === "done") return { word: "done", tone: "done" };
  const assignment = step.assignments.find((a) => a.membershipId === memberId);
  if (assignment?.status === "asked") return { word: "asked", tone: "attn" };
  if (assignment?.status === "countered") return { word: "suggested changes", tone: "attn" };
  if (isWaiting(step)) return { word: "waiting", tone: "attn" };
  const start = stepSpan(step)?.start;
  if (start !== undefined && start > today) return { word: `starts ${dayShort(start)}`, tone: "muted" };
  if (step.status === "in_progress") return { word: "in progress", tone: "ink" };
  return stepSpan(step) ? { word: "this week", tone: "muted" } : { word: "no dates", tone: "faint" };
}

/** A person's steps: the ones they own, were asked to take, or suggested changes to. */
export function personSteps(plan: ProjectPlan, memberId: string): PlanStep[] {
  return orderedSteps(plan).filter(
    (s) => s.ownerIds.includes(memberId) || s.assignments.some((a) => a.membershipId === memberId && (a.status === "asked" || a.status === "countered")),
  );
}

/** "4 steps · 1 done · next: Write down the workflow, Oct 5", or "invited". */
export function personLine(plan: ProjectPlan, member: PlanMember): string {
  if (isInvitedMember(member)) return "Invited — they haven’t joined yet";
  const steps = personSteps(plan, member.id);
  if (steps.length === 0) return member.id === plan.myMembershipId ? "Nothing on your plate here yet" : "Nothing on their plate here yet";
  const done = steps.filter((s) => s.status === "done").length;
  const next = steps.filter((s) => !isClosed(s) && stepSpan(s)).sort((a, b) => (stepSpan(a)?.start ?? BIG) - (stepSpan(b)?.start ?? BIG))[0];
  const parts = [`${steps.length} ${steps.length === 1 ? "step" : "steps"}`, `${done} done`];
  if (next) parts.push(`next: ${next.title}, ${dayShort(stepSpan(next)?.start ?? 0)}`);
  return parts.join(" · ");
}

/** "Measure the beds (Rin, done)" — how one step names another it depends on. */
export function describeStep(plan: ProjectPlan, step: PlanStep): string {
  const who = ownerLabel(plan, step);
  return step.status === "done" ? `${step.title} (${who}, done)` : `${step.title} (${who})`;
}

// ── Milestones and health ─────────────────────────────────────────────────

/** "Workflow agreed · Oct 9", or "Other steps". */
export function groupLabel(group: PlanGroup): string {
  if (!group.milestone) return "Other steps";
  return group.milestone.target !== null ? `${group.milestone.title} · ${dayShort(group.milestone.target)}` : group.milestone.title;
}

/** The word at the end of a milestone's heading: "Reached", "3 to go", "All done", "No steps yet". */
export function groupStatus(group: PlanGroup): StateWord {
  if (group.milestone?.reached) return { word: "Reached", tone: "done" };
  if (group.steps.length === 0) return { word: "No steps yet", tone: "faint" };
  const open = group.steps.filter((s) => !isClosed(s)).length;
  return open ? { word: `${open} to go`, tone: "faint" } : { word: "All done", tone: "done" };
}

/** "Oct 9 · 1 of 4 done" — the line under a milestone in the strip. */
export function milestoneLine(plan: ProjectPlan, milestone: PlanMilestone): string {
  const steps = plan.steps.filter((s) => s.milestoneId === milestone.id);
  const done = steps.filter((s) => s.status === "done").length;
  const progress = milestone.reached ? "reached" : steps.length ? `${done} of ${steps.length} done` : "no steps yet";
  return milestone.target !== null ? `${dayShort(milestone.target)} · ${progress}` : progress;
}

export interface HealthWords {
  /** In a sentence: "on track". */
  short: string;
  /** On its own line: "Running late". */
  long: string;
  tone: Tone;
}

export function healthWords(health: PlanHealth): HealthWords {
  if (health === "late") return { short: "running late", long: "Running late", tone: "attn" };
  if (health === "at_risk") return { short: "at risk", long: "At risk of slipping", tone: "attn" };
  return { short: "on track", long: "On track", tone: "done" };
}

/** "2 of 7 steps done" */
export function doneLine(plan: Pick<ProjectPlan, "doneCount" | "total">): string {
  return `${plan.doneCount} of ${plan.total} ${plan.total === 1 ? "step" : "steps"} done`;
}

/** The step I should look at next: my earliest open one. */
export function upNext(plan: ProjectPlan): PlanStep | undefined {
  return orderedSteps(plan)
    .filter((s) => isOwner(plan, s) && !isClosed(s))
    .sort((a, b) => (stepSpan(a)?.start ?? BIG) - (stepSpan(b)?.start ?? BIG))[0];
}

/** "You + Kiho", "You + 3", "Just you" — who the project is with. */
export function crewLine(plan: ProjectPlan): string {
  const others = plan.members.filter((m) => m.id !== plan.myMembershipId && (isActiveMember(m) || isInvitedMember(m)));
  if (others.length === 0) return "Just you";
  if (others.length === 1) return `You + ${others[0].displayName.split(/\s+/)[0]}`;
  return `You + ${others.length}`;
}

/** The line under a project's name on the Neighborhood page. */
export function projectRowLine(plan: ProjectPlan): string {
  if (plan.total === 0) return "No steps yet";
  const parts = [doneLine(plan)];
  const next = upNext(plan) ?? orderedSteps(plan).find((s) => !isClosed(s));
  if (next) parts.push(`next: ${next.title}`);
  if (plan.health !== "on_track") parts.push(healthWords(plan.health).short);
  return parts.join(" · ");
}

// ── Copy that depends on the plan ─────────────────────────────────────────

/** Plan tab: steps without dates aren't on the timeline. */
export function undatedPlanHint(plan: ProjectPlan): string {
  const n = undatedOpenSteps(plan).length;
  if (n === 0) return "";
  return `${n} step${n === 1 ? " has" : "s have"} no dates yet, so ${n === 1 ? "it isn’t" : "they aren’t"} on the timeline.`;
}

/** Timeline tab: say what's missing, and why the chart is short. */
export function undatedTimelineHint(plan: ProjectPlan): string {
  const n = undatedOpenSteps(plan).length;
  if (n === 0) return "";
  if (plan.steps.every((s) => stepSpan(s) === null)) return "Nothing has dates yet. Give steps a start and finish to see them here.";
  return `${n} step${n === 1 ? " has" : "s have"} no dates, so ${n === 1 ? "it isn’t" : "they aren’t"} shown.`;
}

/** Who can mark a step done, said plainly. */
export function stepFootnote(plan: ProjectPlan, step: PlanStep): string {
  const owners = ownersOf(plan, step);
  if (isOwner(plan, step)) return "It’s in your journal too. Only the people who took a step can mark it done.";
  if (owners.length === 0) return "Nobody has taken this yet. Ask someone, or take it yourself.";
  return `Only ${joinNames(owners.map((m) => m.displayName))} can mark this done.`;
}

/** "If this slips, Build the frames and Planting day move too." */
export function slipLine(plan: ProjectPlan, step: PlanStep): string {
  if (isClosed(step)) return "";
  const titles = step.movesIfLate.map((id) => findStep(plan, id)?.title).filter((t): t is string => !!t);
  if (titles.length === 0) return "";
  return `If this slips, ${joinNames(titles)} move${titles.length === 1 ? "s" : ""} too.`;
}

export function leaveMessage(plan: ProjectPlan): string {
  const otherOwners = plan.members.some((m) => m.id !== plan.myMembershipId && isActiveMember(m) && m.role === "owner");
  if (plan.myRole === "owner" && !otherOwners) return "You’re the owner — whoever joined first takes over. Steps you took stay in your journal.";
  return "You won’t see it anymore. Steps you took stay in your journal.";
}

/** Steps this one could wait on: not itself, and nothing already waiting on it. */
export function blockerChoices(plan: ProjectPlan, editingId: string | null): PlanStep[] {
  if (!editingId) return orderedSteps(plan);
  const downstream = downstreamOf(plan, editingId);
  return orderedSteps(plan).filter((s) => s.id !== editingId && !downstream.has(s.id));
}

/** Members who could still be asked to take a step. */
export function askCandidates(plan: ProjectPlan, step: PlanStep): PlanMember[] {
  const taken = new Set(step.assignments.filter((a) => a.status === "accepted" || a.status === "asked").map((a) => a.membershipId));
  return people(plan).filter((m) => !taken.has(m.id));
}

/** Under "Who's doing it" on a new step. */
export function askNote(plan: ProjectPlan, picked: string[]): string {
  const others = picked.filter((id) => id !== plan.myMembershipId).map((id) => findMember(plan, id)?.displayName).filter((n): n is string => !!n);
  if (others.length === 0) return picked.length === 0 ? "Leave it open and anyone can take it." : "It goes on your own task list too.";
  return `We’ll ask ${joinNames(others)} first. It shows as “asked” until they say yes.`;
}

/** The request to send your assistant from "Start a project → Ask my assistant". */
export function assistantSeed(title: string, goal: string, names: string[]): string {
  let seed = `Help me plan ${title.trim()}`;
  if (names.length) seed += ` with ${joinNames(names)}`;
  if (goal.trim()) seed += `. The goal: ${goal.trim()}`;
  return `${seed}. Draft milestones, steps with rough dates, who could do each, and what waits on what.`;
}

// ── Starter templates (deterministic; offsets are days from today) ─────────

export interface ProjectTemplate {
  id: string;
  title: string;
  goal: string;
  milestones: { title: string; day: number; steps: { title: string; start: number; end: number }[] }[];
}

export const PROJECT_TEMPLATES: ProjectTemplate[] = [
  {
    id: "trip",
    title: "A trip",
    goal: "A trip everyone enjoys, planned without the stress.",
    milestones: [
      { title: "Plan agreed", day: 7, steps: [{ title: "Pick dates", start: 0, end: 3 }, { title: "Agree a budget", start: 2, end: 6 }] },
      { title: "Booked", day: 21, steps: [{ title: "Book travel", start: 7, end: 14 }, { title: "Book a place to stay", start: 7, end: 14 }] },
      { title: "Ready to go", day: 35, steps: [{ title: "Share a packing list", start: 22, end: 30 }] },
    ],
  },
  {
    id: "event",
    title: "An event",
    goal: "A good day for everyone who comes.",
    milestones: [
      { title: "Date and place set", day: 7, steps: [{ title: "Choose a date", start: 0, end: 3 }, { title: "Find a place", start: 2, end: 7 }] },
      { title: "Invites out", day: 14, steps: [{ title: "Send invitations", start: 8, end: 12 }] },
      { title: "The day", day: 28, steps: [{ title: "Food and drinks", start: 18, end: 26 }, { title: "Set up and clean up", start: 27, end: 28 }] },
    ],
  },
  {
    id: "home",
    title: "A home project",
    goal: "Done well, on time, with no surprises.",
    milestones: [
      { title: "Plan agreed", day: 7, steps: [{ title: "Measure up", start: 0, end: 2 }, { title: "Draw the plan", start: 3, end: 6 }] },
      { title: "Materials in", day: 14, steps: [{ title: "Buy materials", start: 7, end: 11 }] },
      { title: "Finished", day: 28, steps: [{ title: "Build it", start: 12, end: 24 }, { title: "Tidy up", start: 25, end: 27 }] },
    ],
  },
  {
    id: "group",
    title: "A group build",
    goal: "Something we’re proud of, made together.",
    milestones: [
      { title: "Kickoff", day: 5, steps: [{ title: "Agree who does what", start: 0, end: 4 }] },
      { title: "First version", day: 21, steps: [{ title: "Build the first part", start: 5, end: 14 }, { title: "Build the second part", start: 5, end: 18 }] },
      { title: "Shared", day: 30, steps: [{ title: "Share it", start: 22, end: 29 }] },
    ],
  },
];

export type TemplateWrite =
  | { kind: "milestone"; key: string; body: { title: string; target_date: string; order: number } }
  | { kind: "step"; key: string; milestoneKey: string; body: { title: string; start_date: string; due_date: string; order: number } }
  | { kind: "dependency"; blockerKey: string; blockedKey: string };

/**
 * The writes that lay a template into a fresh project, in order: each
 * milestone, its steps, and a finish-to-start link from the previous step into
 * the first step of the next milestone so the timeline has arrows from day one.
 */
export function templateWrites(template: ProjectTemplate, today: Day): TemplateWrite[] {
  const out: TemplateWrite[] = [];
  let previous: string | null = null;
  template.milestones.forEach((m, mi) => {
    const mKey = `m${mi}`;
    out.push({ kind: "milestone", key: mKey, body: { title: m.title, target_date: dayIso(today + m.day), order: mi } });
    m.steps.forEach((s, si) => {
      const sKey = `m${mi}s${si}`;
      out.push({ kind: "step", key: sKey, milestoneKey: mKey, body: { title: s.title, start_date: dayIso(today + s.start), due_date: dayIso(today + s.end), order: si } });
      if (previous && si === 0) out.push({ kind: "dependency", blockerKey: previous, blockedKey: sKey });
      previous = sKey;
    });
  });
  return out;
}
