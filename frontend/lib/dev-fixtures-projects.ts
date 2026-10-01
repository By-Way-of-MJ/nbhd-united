/**
 * DEV-ONLY: a no-network Projects v2 server for the fixture API (see
 * lib/dev-fixtures.ts — only ever loaded behind NODE_ENV=development +
 * NEXT_PUBLIC_WEB_FIXTURES=1). Mirrors the iPhone's ProjectFixtureServer: it
 * seeds a few plans with dates relative to today and applies writes in memory,
 * so Mark done, asks, answers, milestones and dependencies visibly work.
 *
 *   m-pm      the approved web mockup's plan (you + Kiho, two milestones)
 *   m-garden  the iPhone's garden (three people, an ask for you, a suggestion)
 *   m-3       a project from before plans: steps, no dates, no milestones
 */

type Json = unknown;
type Body = Record<string, unknown>;

interface Step {
  id: string;
  milestone: string | null;
  title: string;
  description: string;
  start: number | null;
  end: number | null;
  status: string;
  order: number;
  version: number;
}
interface Assignment {
  id: string;
  step: string;
  member: string;
  status: string;
  counterStart: number | null;
  counterDue: number | null;
  note: string;
}
interface Milestone {
  id: string;
  title: string;
  day: number | null;
  order: number;
}
interface Member {
  id: string;
  name: string;
  handle: string;
  hue: number;
  role: string;
}
interface Project {
  title: string;
  goal: string;
  version: number;
  /** Day offsets below are relative to today + this. */
  origin: number;
  members: Member[];
  invited: Set<string>;
  milestones: Milestone[];
  steps: Step[];
  assignments: Assignment[];
  edges: { id: string; blocker: string; blocked: string }[];
}

const ME = "mb-you";
const CLOSED = new Set(["done", "skipped"]);
const GOALS = [
  { id: "g1", title: "Run a 10k" },
  { id: "g-outside", title: "Get outside more" },
  { id: "g-build", title: "Learn to build things" },
];

function httpError(status: number, body: Json): Error {
  const err = new Error(JSON.stringify(body));
  (err as Error & { status: number }).status = status;
  return err;
}

function isoDay(offset: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function shortDay(offset: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function dayOffset(iso: unknown): number | null {
  if (typeof iso !== "string" || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return null;
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  const now = new Date();
  return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / 86_400_000);
}

const step = (id: string, milestone: string | null, title: string, start: number | null, end: number | null, status = "open", description = ""): Omit<Step, "order"> => ({
  id,
  milestone,
  title,
  description,
  start,
  end,
  status,
  version: 0,
});
const assign = (stepId: string, member: string, status = "accepted"): Assignment => ({ id: `a-${stepId}-${member}`, step: stepId, member, status, counterStart: null, counterDue: null, note: "" });
const ordered = (steps: Omit<Step, "order">[]): Step[] => steps.map((s, order) => ({ ...s, order }));

function seed(): Map<string, Project> {
  const projects = new Map<string, Project>();
  // The web mockup's project: today sits in its first week.
  projects.set("m-pm", {
    title: "nbhd project management",
    goal: "Consistency towards working on a project together",
    version: 1,
    origin: -3,
    members: [
      { id: ME, name: "Yuki", handle: "yuki", hue: 260, role: "owner" },
      { id: "mb-kiho", name: "Kiho", handle: "kiho", hue: 345, role: "member" },
    ],
    invited: new Set(),
    milestones: [
      { id: "ms-flow", title: "Workflow agreed", day: 11, order: 0 },
      { id: "ms-week", title: "First week together", day: 25, order: 1 },
    ],
    steps: ordered([
      step("s-goal", "ms-flow", "Agree on the goal", 0, 0, "done"),
      step("s-talk", "ms-flow", "Talk about an AI workflow", 1, 4, "in_progress", "A first pass at how we hand work to each other: who drafts, who reviews, and where it lives."),
      step("s-write", "ms-flow", "Write down the workflow", 7, 10),
      step("s-board", "ms-flow", "Set up the shared board", 7, 9),
      step("s-try", "ms-week", "Try it for one week", 14, 20),
      step("s-look", "ms-week", "Look back: what worked", 22, 24),
      step("s-call", null, "Pick a weekly call time", null, null),
    ]),
    assignments: [assign("s-goal", ME), assign("s-goal", "mb-kiho"), assign("s-talk", ME), assign("s-write", "mb-kiho"), assign("s-board", ME), assign("s-try", ME), assign("s-try", "mb-kiho"), assign("s-look", "mb-kiho", "asked"), assign("s-call", ME)],
    edges: [
      { id: "e1", blocker: "s-talk", blocked: "s-write" },
      { id: "e2", blocker: "s-write", blocked: "s-try" },
      { id: "e3", blocker: "s-board", blocked: "s-try" },
      { id: "e4", blocker: "s-try", blocked: "s-look" },
    ],
  });
  // The iPhone's garden: today lands in week 2.
  projects.set("m-garden", {
    title: "Fix up the shared garden",
    goal: "Four raised beds, planted before the first frost.",
    version: 0,
    origin: -9,
    members: [
      { id: ME, name: "Yuki", handle: "yuki", hue: 260, role: "owner" },
      { id: "mb-sam", name: "Sam", handle: "sam", hue: 210, role: "member" },
      { id: "mb-rin", name: "Rin", handle: "rin", hue: 330, role: "member" },
    ],
    invited: new Set(),
    milestones: [
      { id: "ms-plan", title: "Plan agreed", day: 6, order: 0 },
      { id: "ms-beds", title: "Beds built", day: 20, order: 1 },
      { id: "ms-plant", title: "First planting", day: 34, order: 2 },
    ],
    steps: ordered([
      step("s-measure", "ms-plan", "Measure the beds", 0, 2, "done"),
      step("s-layout", "ms-plan", "Draw the layout", 3, 5, "done"),
      step("s-buy", "ms-beds", "Buy timber & soil", 7, 10, "in_progress", "Enough cedar for four 1.2 × 2.4 m beds, plus 3 m³ of soil mix. The yard delivers Thursdays."),
      step("s-frames", "ms-beds", "Build the frames", 11, 19),
      step("s-seed", "ms-plant", "Pick the seedlings", 14, 25),
      step("s-compost", "ms-plant", "Pick up compost", 26, 28),
      step("s-plant", "ms-plant", "Planting day", 32, 34),
    ]),
    assignments: [
      assign("s-measure", "mb-rin"),
      assign("s-layout", ME),
      assign("s-buy", "mb-sam"),
      assign("s-frames", ME),
      assign("s-frames", "mb-sam"),
      assign("s-seed", "mb-rin"),
      assign("s-compost", ME, "asked"),
      assign("s-plant", ME),
      assign("s-plant", "mb-sam"),
      assign("s-plant", "mb-rin"),
    ],
    edges: [
      { id: "e1", blocker: "s-measure", blocked: "s-layout" },
      { id: "e2", blocker: "s-layout", blocked: "s-buy" },
      { id: "e3", blocker: "s-buy", blocked: "s-frames" },
      { id: "e4", blocker: "s-frames", blocked: "s-plant" },
      { id: "e5", blocker: "s-seed", blocked: "s-plant" },
    ],
  });
  // What a project from before plans looks like: steps, nothing dated.
  projects.set("m-3", {
    title: "Morning walks",
    goal: "",
    version: 1,
    origin: 0,
    members: [
      { id: ME, name: "Yuki", handle: "yuki", hue: 260, role: "owner" },
      { id: "mb-aiko", name: "Aiko", handle: "aiko", hue: 12, role: "member" },
    ],
    invited: new Set(),
    milestones: [],
    steps: ordered([step("s-loop", null, "Try the river loop", null, null), step("s-shoes", null, "Look up the river loop distance", null, null, "done")]),
    assignments: [assign("s-loop", ME), assign("s-shoes", "mb-aiko")],
    edges: [],
  });
  return projects;
}

let projects = seed();
let seq = 0;
const gone = new Set<string>();
const linkedGoal = new Map<string, string>();
let proposalPending = true;
let draftPending = true;
const nextId = (prefix: string) => `${prefix}-fx${++seq}`;

/** Missions this server has a plan for (the fixture list shows them as v2 projects). */
export function hasProjectFixture(id: string): boolean {
  return projects.has(id) && !gone.has(id);
}

/** Title as renamed here, so the Neighborhood list stays in step. */
export function projectFixtureTitle(id: string): string | undefined {
  return projects.get(id)?.title;
}

function descendants(p: Project, id: string): string[] {
  const seen: string[] = [];
  const stack = [id];
  while (stack.length) {
    const current = stack.pop() as string;
    for (const e of p.edges) {
      if (e.blocker === current && !seen.includes(e.blocked)) {
        seen.push(e.blocked);
        stack.push(e.blocked);
      }
    }
  }
  return seen;
}

function plan(id: string, p: Project): Json {
  const iso = (offset: number | null) => (offset === null ? null : isoDay(p.origin + offset));
  const today = -p.origin;
  const byId = new Map(p.steps.map((s) => [s.id, s]));
  const member = (m: Member) => {
    const row: Record<string, Json> = { id: m.id, handle: m.handle, display_name: m.name, hue: m.hue, role: m.role, status: p.invited.has(m.id) ? "invited" : "active", photo_url: null };
    if (m.id === ME) {
      const goal = GOALS.find((g) => g.id === linkedGoal.get(id));
      row.muted = false;
      row.linked_goal_id = goal?.id ?? null;
      row.linked_goal_title = goal?.title ?? null;
    }
    return row;
  };
  const steps = p.steps.map((s) => {
    const parents = p.edges.filter((e) => e.blocked === s.id).map((e) => e.blocker);
    const children = p.edges.filter((e) => e.blocker === s.id).map((e) => e.blocked);
    const blocked = parents.filter((x) => !CLOSED.has(byId.get(x)?.status ?? "done"));
    const assignments = p.assignments.filter((a) => a.step === s.id);
    const owners = assignments.filter((a) => a.status === "accepted" && !p.invited.has(a.member)).map((a) => p.members.find((m) => m.id === a.member)).filter((m): m is Member => !!m);
    // Same-day handoff slack: days until the earliest open child starts.
    const childStarts = children.map((c) => byId.get(c)).filter((c): c is Step => !!c && !CLOSED.has(c.status) && c.start !== null).map((c) => c.start as number);
    const slack = !CLOSED.has(s.status) && s.end !== null && childStarts.length ? Math.min(...childStarts) - s.end - 1 : null;
    return {
      id: s.id,
      milestone_id: s.milestone,
      title: s.title,
      description: s.description,
      start_date: iso(s.start),
      due_date: iso(s.end),
      status: s.status,
      order: s.order,
      version: s.version,
      assignments: assignments.map((a) => ({ id: a.id, step_id: a.step, membership_id: a.member, status: a.status, counter_start: iso(a.counterStart), counter_due: iso(a.counterDue), note: a.note })),
      owners: owners.map(member),
      blocked_by_open: blocked,
      ready: !CLOSED.has(s.status) && blocked.length === 0,
      slack_days: slack,
      on_critical_path: slack !== null && slack <= 0,
      moves_if_late: !CLOSED.has(s.status) && slack !== null && slack <= 0 ? descendants(p, s.id) : [],
      projected_date: iso(s.end),
    };
  });
  const open = p.steps.filter((s) => !CLOSED.has(s.status));
  const late = open.some((s) => s.end !== null && s.end < today) || p.milestones.some((m) => m.day !== null && m.day < today && open.some((s) => s.milestone === m.id));
  const risk = open.some((s) => s.milestone && s.end !== null && (p.milestones.find((m) => m.id === s.milestone)?.day ?? Infinity) < s.end);
  return {
    mission_id: id,
    title: p.title,
    description: p.goal,
    status: "active",
    version: p.version,
    target_date: null,
    members: p.members.map(member),
    milestones: p.milestones.map((m) => {
      const mine = p.steps.filter((s) => s.milestone === m.id);
      const done = mine.filter((s) => s.status === "done").length;
      const ends = mine.map((s) => s.end).filter((e): e is number => e !== null);
      return { id: m.id, title: m.title, target_date: iso(m.day), order: m.order, reached_at: mine.length && done === mine.length ? new Date().toISOString() : null, done_count: done, total: mine.length, projected_date: ends.length ? iso(Math.max(...ends)) : null };
    }),
    steps,
    edges: p.edges.map((e) => ({ id: e.id, blocker_id: e.blocker, blocked_id: e.blocked })),
    health: late ? "late" : risk ? "at_risk" : "on_track",
    done_count: p.steps.filter((s) => s.status === "done").length,
    total: p.steps.length,
    my_membership_id: ME,
    my_role: "owner",
    can_invite: true,
  };
}

function proposals(missionId: string | null): Json {
  if (!proposalPending || (missionId && missionId !== "m-garden") || gone.has("m-garden")) return [];
  const p = projects.get("m-garden") as Project;
  return [
    {
      proposal_id: "pp-frames",
      mission_id: "m-garden",
      project_title: p.title,
      summary: "Give the frames three more days — the timber comes Thursday",
      changes: [`Move “Build the frames” to ${shortDay(p.origin + 14)} – ${shortDay(p.origin + 22)}`],
      touches_others: false,
      from_project_text: false,
    },
  ];
}

const DRAFT = {
  draft_id: "d-cleanup",
  payload: {
    title: "Autumn clean-up day",
    goal: "The street tidy before winter",
    milestones: [{ key: "m1", title: "The day", target_date: isoDay(21) }],
    steps: [
      { key: "s1", title: "Borrow rakes and bags", owner: "me", milestone_key: "m1", due_date: isoDay(16) },
      { key: "s2", title: "Book the skip", owner: "@kiho", milestone_key: "m1", due_date: isoDay(17) },
      { key: "s3", title: "Clean-up morning", milestone_key: "m1", depends_on: ["s1", "s2"], start_date: isoDay(21), due_date: isoDay(21) },
    ],
  },
};

/**
 * Returns a response for a Projects v2 path, `undefined` to fall through to the
 * rest of the fixture API, or throws the same error shape `apiFetch` does.
 */
export function projectFixture(p: string, method: string, url: URL, body: Body): Json | undefined {
  if (p === "/api/v1/friends/project-proposals/" && method === "GET") return proposals(url.searchParams.get("mission_id"));
  const decide = p.match(/^\/api\/v1\/friends\/project-proposals\/([^/]+)\/(approve|reject)\/$/);
  if (decide) {
    if (!proposalPending) throw httpError(404, { detail: "No such suggestion." });
    proposalPending = false;
    if (decide[2] === "reject") return { status: "rejected" };
    const frames = projects.get("m-garden")?.steps.find((s) => s.id === "s-frames");
    if (frames) Object.assign(frames, { start: 14, end: 22, version: frames.version + 1 });
    return { status: "approved", changes: [{ change: "move_step", outcome: "applied" }] };
  }
  if (p === "/api/v1/friends/project-drafts/" && method === "GET") {
    return draftPending ? [{ draft_id: DRAFT.draft_id, title: DRAFT.payload.title, goal: DRAFT.payload.goal, step_count: DRAFT.payload.steps.length }] : [];
  }
  const draft = p.match(/^\/api\/v1\/friends\/project-drafts\/([^/]+)\/(publish\/)?$/);
  if (draft) {
    if (!draftPending) throw httpError(404, { detail: "No such draft." });
    if (method === "GET") return DRAFT;
    draftPending = false;
    if (!draft[2]) return {};
    projects.set("m-cleanup", {
      title: DRAFT.payload.title,
      goal: DRAFT.payload.goal,
      version: 0,
      origin: 0,
      members: [
        { id: ME, name: "Yuki", handle: "yuki", hue: 260, role: "owner" },
        { id: "mb-kiho", name: "Kiho", handle: "kiho", hue: 345, role: "member" },
      ],
      invited: new Set(["mb-kiho"]),
      milestones: [{ id: "ms-day", title: "The day", day: 21, order: 0 }],
      steps: ordered([step("s-rakes", "ms-day", "Borrow rakes and bags", null, 16), step("s-skip", "ms-day", "Book the skip", null, 17), step("s-morning", "ms-day", "Clean-up morning", 21, 21)]),
      assignments: [assign("s-rakes", ME), assign("s-skip", "mb-kiho", "asked")],
      edges: [
        { id: "e1", blocker: "s-rakes", blocked: "s-morning" },
        { id: "e2", blocker: "s-skip", blocked: "s-morning" },
      ],
    });
    return { mission_id: "m-cleanup", not_asked: [] };
  }

  const mission = p.match(/^\/api\/v1\/friends\/missions\/([^/]+)\/(.*)$/);
  if (!mission) {
    // Creating a project the v2 way: any number of neighbors, or none.
    if (p === "/api/v1/friends/missions/" && method === "POST" && Array.isArray(body.member_friendship_ids)) {
      const id = nextId("m");
      const ids = body.member_friendship_ids as unknown[];
      projects.set(id, {
        title: String(body.title ?? "New project"),
        goal: String(body.description ?? ""),
        version: 0,
        origin: 0,
        members: [{ id: ME, name: "Yuki", handle: "yuki", hue: 260, role: "owner" }, ...ids.map((fid, i) => ({ id: `mb-${String(fid)}`, name: ["Kiho", "Dudley", "Aiko", "Ren"][i % 4], handle: `n${i}`, hue: [345, 210, 12, 200][i % 4], role: "member" }))],
        invited: new Set(ids.map((fid) => `mb-${String(fid)}`)),
        milestones: [],
        steps: [],
        assignments: [],
        edges: [],
      });
      return { mission_id: id };
    }
    return undefined;
  }
  const [, id, rest] = mission;
  const project = projects.get(id);
  // Only the Projects v2 paths; the legacy detail (`missions/<id>/`) stays with the main fixtures.
  const v2Path = /^(plan|steps|milestones|dependencies|membership|members|delete)\//.test(rest) || (rest === "" && method === "PATCH" && !!project) || (rest === "leave/" && !!project);
  if (!v2Path) return undefined;
  if (!project || gone.has(id)) throw httpError(404, { detail: "No such mission." });

  if (rest === "plan/") return plan(id, project);
  if (rest === "leave/" || rest === "delete/") {
    gone.add(id);
    return { mission_id: id, status: rest === "leave/" ? "left" : "abandoned" };
  }
  if (rest === "" && method === "PATCH") {
    if (body.version !== project.version) throw httpError(409, { detail: "Someone else changed this project.", version: project.version });
    if (typeof body.title === "string" && body.title.trim()) project.title = body.title.trim();
    project.version += 1;
    return { mission_id: id, version: project.version, title: project.title };
  }
  if (rest === "membership/") {
    const goal = body.linked_goal_id ? String(body.linked_goal_id) : null;
    if (goal && !GOALS.some((g) => g.id === goal)) throw httpError(404, { detail: "No such goal." });
    if (goal) linkedGoal.set(id, goal);
    else linkedGoal.delete(id);
    return { linked_goal_id: goal };
  }
  if (rest === "members/") {
    const ids = Array.isArray(body.member_friendship_ids) ? (body.member_friendship_ids as unknown[]) : [];
    const names = ["Dudley", "Mika", "Daniel", "Sora"];
    ids.forEach((fid, i) => {
      const mid = `mb-${String(fid)}`;
      if (project.members.some((m) => m.id === mid)) return;
      project.members.push({ id: mid, name: names[(project.members.length + i) % names.length], handle: `n-${String(fid)}`, hue: (project.members.length * 67) % 360, role: "member" });
      project.invited.add(mid);
    });
    return { invited: ids.length };
  }
  if (rest === "steps/" && method === "POST") {
    if (project.steps.length >= 60) throw httpError(400, { detail: "A project can have up to 60 steps." });
    const created: Step = {
      id: nextId("s"),
      milestone: body.milestone_id ? String(body.milestone_id) : null,
      title: String(body.title ?? "Step"),
      description: String(body.description ?? ""),
      start: dayOffset(body.start_date) === null ? null : (dayOffset(body.start_date) as number) - project.origin,
      end: dayOffset(body.due_date) === null ? null : (dayOffset(body.due_date) as number) - project.origin,
      status: "open",
      order: project.steps.length,
      version: 0,
    };
    project.steps.push(created);
    return { step_id: created.id, version: 0 };
  }
  const stepPath = rest.match(/^steps\/([^/]+)\/(ask|respond|complete|reopen)?\/?$/);
  if (stepPath) {
    const target = project.steps.find((s) => s.id === stepPath[1]);
    if (!target) throw httpError(404, { detail: "No such step." });
    const action = stepPath[2] ?? "";
    const rel = (value: unknown) => (dayOffset(value) === null ? null : (dayOffset(value) as number) - project.origin);
    if (action === "" && method === "PATCH") {
      if (body.version !== target.version) throw httpError(409, { detail: "This step changed. Refresh and try again.", version: target.version });
      if (typeof body.title === "string") target.title = body.title;
      if ("start_date" in body) target.start = rel(body.start_date);
      if ("due_date" in body) target.end = rel(body.due_date);
      if ("milestone_id" in body) target.milestone = body.milestone_id ? String(body.milestone_id) : null;
      if (typeof body.description === "string") target.description = body.description;
      target.version += 1;
      return { step_id: target.id, version: target.version };
    }
    if (action === "" && method === "DELETE") {
      project.steps = project.steps.filter((s) => s.id !== target.id);
      project.assignments = project.assignments.filter((a) => a.step !== target.id);
      project.edges = project.edges.filter((e) => e.blocker !== target.id && e.blocked !== target.id);
      return {};
    }
    if (action === "ask") {
      for (const m of Array.isArray(body.membership_ids) ? (body.membership_ids as unknown[]).map(String) : []) {
        const existing = project.assignments.find((a) => a.step === target.id && a.member === m);
        if (existing) {
          if (existing.status !== "accepted") Object.assign(existing, { status: "asked", counterStart: null, counterDue: null, note: "" });
        } else project.assignments.push(assign(target.id, m, "asked"));
      }
      return { step_id: target.id, status: "asked" };
    }
    const mine = project.assignments.find((a) => a.step === target.id && a.member === ME);
    if (action === "respond") {
      if (!mine) throw httpError(403, { detail: "You weren't asked to take this step." });
      if (body.answer === "yes") mine.status = "accepted";
      else if (body.answer === "no") mine.status = "declined";
      else Object.assign(mine, { status: "countered", counterStart: rel(body.start), counterDue: rel(body.due), note: String(body.note ?? "") });
      return { assignment_id: mine.id, status: mine.status };
    }
    if (action === "complete" || action === "reopen") {
      if (mine?.status !== "accepted") throw httpError(403, { detail: "Only the step's owner can do that." });
      target.status = action === "complete" ? "done" : "open";
      target.version += 1;
      return { step_id: target.id, status: target.status, version: target.version };
    }
  }
  if (rest === "milestones/" && method === "POST") {
    if (project.milestones.length >= 8) throw httpError(400, { detail: "A project can have up to 8 milestones." });
    const off = dayOffset(body.target_date);
    const created: Milestone = { id: nextId("ms"), title: String(body.title ?? "Milestone"), day: off === null ? null : off - project.origin, order: typeof body.order === "number" ? body.order : project.milestones.length };
    project.milestones.push(created);
    return { milestone_id: created.id };
  }
  const milestonePath = rest.match(/^milestones\/([^/]+)\/$/);
  if (milestonePath) {
    const target = project.milestones.find((m) => m.id === milestonePath[1]);
    if (!target) throw httpError(404, { detail: "No such milestone." });
    if (method === "DELETE") {
      project.milestones = project.milestones.filter((m) => m.id !== target.id);
      project.steps.forEach((s) => {
        if (s.milestone === target.id) s.milestone = null;
      });
      return {};
    }
    if (typeof body.title === "string") target.title = body.title;
    if ("target_date" in body) {
      const off = dayOffset(body.target_date);
      target.day = off === null ? null : off - project.origin;
    }
    return { milestone_id: target.id };
  }
  if (rest === "dependencies/" && method === "POST") {
    const blocker = String(body.blocker_id ?? ""), blocked = String(body.blocked_id ?? "");
    if (descendants(project, blocked).includes(blocker) || blocker === blocked) throw httpError(400, { detail: "That would make these steps wait on each other." });
    const created = { id: nextId("e"), blocker, blocked };
    project.edges.push(created);
    return { dependency_id: created.id };
  }
  const edgePath = rest.match(/^dependencies\/([^/]+)\/$/);
  if (edgePath && method === "DELETE") {
    project.edges = project.edges.filter((e) => e.id !== edgePath[1]);
    return {};
  }
  return {};
}

/** Test seam: back to the seeded plans. */
export function resetProjectFixtures(): void {
  projects = seed();
  gone.clear();
  linkedGoal.clear();
  proposalPending = true;
  draftPending = true;
  seq = 0;
}
