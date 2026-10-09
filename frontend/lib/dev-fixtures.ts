/**
 * DEV-ONLY fixture API for screenshotting the logged-in web app without a
 * backend. Loaded exclusively through a dynamic import behind
 * `process.env.NODE_ENV === "development" && NEXT_PUBLIC_WEB_FIXTURES === "1"`
 * in `apiFetch`, so production builds never include this module.
 *
 * Run: `NEXT_PUBLIC_WEB_FIXTURES=1 npm run dev`, then in the browser set
 * `localStorage.nbhd_access_token` to any string (the fixture API ignores it).
 * Append `?fixture=empty` to a page URL to render empty states, or
 * `?fixture=legacy` for a tenant without the web redesign (old shell).
 * `?fixture=journal-conflict` rejects the first block write with the 409 contract;
 * `?fixture=task-failure` rejects the first complete/reopen to check rollback.
 * `?fixture=logged-out` renders public pages without fixture authentication.
 * `?fixture=big` gives /constellation a real-account-sized sky (263 lessons, 53 clusters).
 * `?fixture=journal-long` adds afternoon/evening entries for phone scroll checks.
 * `?fixture=v1` is a tenant without Projects v2 (the older project panel).
 * Projects (plans, steps, milestones) live in `dev-fixtures-projects.ts`; cluster
 * conversations in `dev-fixtures-clusters.ts`. Their writes are in-memory too.
 * Journal/task writes are in-memory only and reset on a full reload.
 */

import { clusterFixture, clusterInviteCode, clusterLeft, clusterRemoved, clusterThreadRows } from "./dev-fixtures-clusters";
import { hasProjectFixture, projectFixture, projectFixtureTitle } from "./dev-fixtures-projects";
import { splitJournalBlocks } from "./journal-blocks";
import { localMonday } from "./horizons-tasks";
import type { JournalTask } from "./types";

type Json = unknown;

function empty(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("fixture") === "empty";
}

function isoDay(offset: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function isoAt(offset: number, hour: number, minute = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}

const tenant = {
  id: "00000000-0000-4000-8000-000000000001",
  user: { id: 1, email: "yuki@example.com", display_name: "Yuki" },
  status: "active",
  model_tier: "starter",
  has_active_subscription: true,
  is_trial: false,
  trial_ends_at: null,
  trial_days_remaining: null,
  container_id: "oc-fixture",
  container_fqdn: "",
  messages_today: 4,
  messages_this_month: 120,
  tokens_this_month: 0,
  estimated_cost_this_month: "0",
  monthly_token_budget: 0,
  monthly_cost_budget: "0",
  preferred_model: "",
  applied_model: "",
  applied_model_at: null,
  effective_model: "",
  free_model_offer: null,
  task_model_preferences: {},
  last_message_at: null,
  provisioned_at: null,
  config_refreshed_at: null,
  config_version: 1,
  pending_config_version: 1,
  hibernated_at: null,
  created_at: "2025-06-01T00:00:00Z",
  pending_deletion: false,
  deletion_scheduled_at: null,
  platform_budget_exceeded: false,
  constellation_enabled: true,
  finance_enabled: false,
  gravity_available: false,
  fuel_enabled: true,
  web_redesign: true,
  core_enabled: true,
  byo_models_enabled: false,
  neighborhood_enabled: true,
  friends_enabled: true,
  projects_v2_enabled: true,
};

const me = {
  id: 1,
  email: "yuki@example.com",
  username: "yuki",
  apple_linked: false,
  display_name: "Yuki Tanaka",
  language: "en",
  timezone: "Asia/Tokyo",
  location_city: "Tokyo",
  location_lat: null,
  location_lon: null,
  telegram_chat_id: null,
  telegram_username: "",
  line_user_id: null,
  line_display_name: "",
  preferred_channel: "telegram",
  tenant,
};

const sleepHours = [7.2, 6.4, 7.8, 5.6, 6.9, 7.5, 6.8, 7.1, 8.0, 6.2, 7.4, 6.6, 7.0, 7.3];
let sleep: { id: string; date: string; duration_hours: string; quality: number | null; notes: string; created_at: string }[] =
  sleepHours.map((h, i) => ({
    id: `sleep-${i}`,
    date: isoDay(-i),
    duration_hours: h.toFixed(2),
    quality: [4, 3, 5, 2, 4, 4, 3, 4, 5, 2, 4, 3, 4, 4][i] ?? null,
    notes: i === 3 ? "Woke up twice" : "",
    created_at: isoAt(-i, 7),
  }));

const weights = [71.6, 71.8, 71.7, 72.0, 72.1, 71.9, 72.2, 72.4, 72.3, 72.6, 72.5, 72.7, 72.9, 72.8];
let bodyWeight: { id: string; date: string; weight_kg: string; created_at: string }[] = weights.map((w, i) => ({
  id: `bw-${i}`,
  date: isoDay(-i * 2),
  weight_kg: w.toFixed(1),
  created_at: isoAt(-i * 2, 8),
}));

function workout(
  id: string,
  offset: number,
  activity: string,
  status: string,
  category: string,
  minutes: number,
  time: [number, number] | null = [18, 30],
) {
  return {
    id,
    date: isoDay(offset),
    scheduled_at: time ? isoAt(offset, time[0], time[1]) : null,
    window_start_at: null,
    window_end_at: null,
    status,
    source: "assistant",
    original_workout: null,
    skip_reason: "",
    category,
    activity,
    duration_minutes: minutes,
    rpe: status === "done" ? 7 : null,
    notes: "",
    notes_thread: [],
    detail_json: {},
    plan_id: null,
    plan_name: null,
    created_at: isoAt(offset - 3, 9),
    updated_at: isoAt(offset, 19),
  };
}

const workouts = [
  workout("w-1", -3, "Zone-2 run", "done", "cardio", 40),
  workout("w-2", -2, "Deadlift 5×5", "done", "strength", 55),
  workout("w-3", 0, "Back squat 5×5", "planned", "strength", 50),
  workout("w-4", 2, "Easy long run", "planned", "cardio", 60),
  workout("w-5", 0, "Morning mobility", "done", "mobility", 20, [7, 0]),
  workout("w-6", -2, "Pull-ups and rows", "planned", "strength", 35, null),
  workout("w-7", 4, "Tempo intervals", "planned", "cardio", 45, [6, 30]),
];

const feed = [
  {
    id: "cron:morning-1",
    role: "assistant",
    text: "Good morning. Short night, **6h 24m**. Push day is on for 6:30pm, and the fabricator cutoff is Friday.\n\nYour calendar is light until the afternoon call. There’s room to make a little progress on the kitchen plan before you head out.\n\nKeep the workout steady today. An easy evening and an earlier night will help you find your rhythm again.",
    created_at: isoAt(0, 7, 40),
    source: "cron",
    thread_id: "main",
    has_image: false,
    has_document: false,
    panels: [
      { kind: "sleep", params: { range: "last_night" }, title: "Last night's sleep" },
      { kind: "workout", params: { day: isoDay(0) }, title: "Today's workout" },
      { kind: "schedule", params: { range: "today" }, title: "Today's calendar" },
      { kind: "log_table", params: { metric: "body_weight", range: "this_month" }, title: "Weight this month" },
    ],
  },
  {
    id: "app:12:1",
    role: "assistant",
    text: "Your week at a glance: a steady wind-down would help the shorter nights.",
    created_at: isoAt(-1, 21, 5),
    source: "app",
    thread_id: "main",
    has_image: false,
    has_document: false,
    panels: [{ kind: "sleep", params: { range: "this_week" }, title: "Sleep this week" }],
  },
];

// Neighborhood: two in "your sky", five others; bonds are buckets only.
let people = [
  ["f-1", "Aiko", "aiko", 12, true, "strong", "2024-03-02"],
  ["f-2", "Ren", "ren", 200, true, "steady", "2025-01-15"],
  ["f-3", "Mika", "mika", 320, false, "light", "2025-06-20"],
  ["f-4", "Daniel", "dan", 140, false, "steady", "2023-11-08"],
  ["f-5", "Sora", "sora", 40, false, "light", "2025-08-30"],
  ["f-6", "Hana", "hana", 280, false, "strong", "2024-09-12"],
  ["f-7", "Kenji", "kenji", 90, false, "light", "2025-02-01"],
].map(([id, name, handle, hue, sky, bond, since], i) => ({
  friendship_id: id as string,
  display_name: name as string,
  handle: handle as string,
  avatar_hue: hue as number,
  bio: "",
  spark_count: 0,
  in_my_sky: sky as boolean,
  bond: bond as string,
  friends_since: since as string,
  has_unread_thread: false,
  thread_id: null as string | null,
  reach: (["10+", "5+", null, "25+", "3+", "10+", null] as (string | null)[])[i],
}));
// `?fixture=crowded` (12 sky + 80 others) and `?fixture=medium` (6 sky + 25
// others) exercise the map at scale. Names are deterministic, some long.
const FIRST = ["Aiko", "Ren", "Mika", "Daniel", "Sora", "Hana", "Kenji", "Tomo", "Mei", "Yuto", "Akari", "Haruto", "Lucia", "Omar", "Priya", "Chidi", "Ingrid", "Mateo", "Noor", "Kwame", "Sakura", "Takumi", "Elena", "Rahul", "Fatima", "Jonas", "Amara", "Diego", "Leilani", "Magnus", "Anneliese", "Bartholomew", "Guadalupe", "Maximilian", "Oluwaseun", "Wilhelmina", "Seo-yeon", "Nguyen", "Zanele", "Isabella"];
const LAST = ["Tanaka", "Sato", "Kowalski", "Okafor", "García", "Nakamura-Whitfield", "Lindqvist", "Haddad", "Fernández de la Cruz", "Mbeki", "Ito", "O'Sullivan", "Park", "Rossi", "Van der Berg", "Abernathy-Montgomery", "Chen", "Dubois", "Yamamoto", "Singh"];
function crowd(sky: number, others: number) {
  const bonds = ["light", "light", "steady", "light", "strong", "steady"];
  return Array.from({ length: sky + others }, (_, i) => {
    const first = FIRST[(i * 7) % FIRST.length];
    const last = LAST[(i * 11) % LAST.length];
    const name = i % 3 === 0 ? `${first} ${last}` : first;
    return {
      friendship_id: `c-${i}`,
      display_name: name,
      handle: `${first.toLowerCase().replace(/[^a-z]/g, "")}${i}`,
      avatar_hue: (i * 47) % 360,
      bio: "",
      spark_count: 0,
      in_my_sky: i < sky,
      bond: bonds[(i * 5) % bonds.length],
      friends_since: `${2019 + (i % 8)}-${String(1 + (i % 12)).padStart(2, "0")}-10`,
      has_unread_thread: false,
      thread_id: null as string | null,
      reach: ([null, "3+", "5+", "10+", "25+", "10+", "50+", "5+", "100+"] as (string | null)[])[(i * 7) % 9],
    };
  });
}
// `?fixture=two` is the approved two-friend Neighborhood (Kiho close, Dudley
// further out); `?fixture=many` is ~150 friends with friends-of-friends.
function two() {
  return [
    { friendship_id: "k-1", display_name: "Kiho", handle: "kiho", avatar_hue: 345, bio: "", spark_count: 3, in_my_sky: true, bond: "strong", friends_since: "2025-04-12", has_unread_thread: false, thread_id: null as string | null, reach: "10+" as string | null },
    { friendship_id: "k-2", display_name: "Dudley", handle: "dudley", avatar_hue: 210, bio: "", spark_count: 0, in_my_sky: false, bond: "light", friends_since: "2026-02-03", has_unread_thread: false, thread_id: null as string | null, reach: "3+" as string | null },
  ];
}
let crowdState: { mode: string; list: ReturnType<typeof crowd> } | null = null;
function crowdFor(mode: string | null) {
  if (mode !== "crowded" && mode !== "medium" && mode !== "many" && mode !== "two") return null;
  if (!crowdState || crowdState.mode !== mode) {
    crowdState = { mode, list: mode === "crowded" ? crowd(12, 80) : mode === "many" ? crowd(12, 138) : mode === "two" ? two() : crowd(6, 25) };
  }
  return crowdState;
}
function neighborsNow() {
  return crowdFor(fixtureMode())?.list ?? people;
}

// Clusters (Circles) with members drawn from whichever neighborhood is showing,
// plus a few members who aren't your friends (they're never placed on the map).
const STRANGERS = ["Emi", "Nora", "Taro", "Wen", "Leo", "Jun", "Sora", "Hina", "Rio", "Yui", "Kaito"];
function clusterList() {
  const mode = fixtureMode();
  const list = neighborsNow();
  const pick = (from: number, n: number) => list.slice(from, from + n).map((x) => x.handle);
  const defs =
    mode === "two"
      ? [
          { circle_id: "c-kita", name: "Kita-ku parents", hue: 340, my_role: "member", description: "Saturday market rota and the school run.", friends: ["kiho"], strangers: 5, admin: "kiho" },
          { circle_id: "c-b3", name: "Building 3", hue: 212, my_role: "admin", description: "Borrowing tools, parcels and the bike room.", friends: ["dudley"], strangers: 9, admin: "" },
        ]
      : mode === "many" || mode === "crowded"
        ? [
            { circle_id: "c-run", name: "Sunday run club", hue: 150, my_role: "member", description: "", friends: pick(3, 16), strangers: 4, admin: list[3]?.handle ?? "" },
            { circle_id: "c-b3", name: "Building 3", hue: 212, my_role: "admin", description: "Borrowing tools, parcels and the bike room.", friends: pick(30, 22), strangers: 6, admin: "" },
            { circle_id: "c-kita", name: "Kita-ku parents", hue: 340, my_role: "member", description: "", friends: pick(60, 9), strangers: 3, admin: list[60]?.handle ?? "" },
          ]
        : [
            { circle_id: "c-1", name: "Sunday run club", hue: 150, my_role: "member", description: "", friends: ["aiko", "ren", "hana"], strangers: 3, admin: "aiko" },
            { circle_id: "c-2", name: "Book swap", hue: 30, my_role: "admin", description: "One in, one out, every other Thursday.", friends: ["mika", "dan"], strangers: 1, admin: "" },
          ];
  return defs
    .filter((d) => !clusterLeft(d.circle_id))
    .map((d) => ({
      ...d,
      invite_code: clusterInviteCode(d.circle_id, d.my_role === "admin" ? `NBHD-${d.circle_id.slice(2).toUpperCase()}7` : null),
      member_count: d.friends.length + d.strangers + 1 - clusterRemoved(d.circle_id).size,
    }));
}
function clusterDetail(id: string) {
  const c = clusterList().find((x) => x.circle_id === id);
  if (!c) return undefined;
  const byHandle = new Map(neighborsNow().map((n) => [n.handle, n]));
  const members = [
    { handle: "yuki", display_name: "Yuki", avatar_hue: 260, role: c.my_role, is_me: true },
    ...c.friends.map((h) => ({ handle: h, display_name: byHandle.get(h)?.display_name ?? h, avatar_hue: byHandle.get(h)?.avatar_hue ?? 200, role: h === c.admin ? "admin" : "member", is_me: false })),
    ...STRANGERS.slice(0, c.strangers).map((n, i) => ({ handle: `${n.toLowerCase()}${i}`, display_name: n, avatar_hue: (i * 53) % 360, role: "member", is_me: false })),
  ].filter((m) => !clusterRemoved(c.circle_id).has(m.handle));
  return { circle_id: c.circle_id, name: c.name, description: c.description, hue: c.hue, members, my_role: c.my_role, thread_id: `t-${c.circle_id}`, invite_code: clusterInviteCode(c.circle_id, c.invite_code) };
}

const postedUpdates = new Map<string, { id: string; kind: string; text: string; created_at: string; author_name: string }[]>();

function missionDetail(id: string) {
  const m = missionAsks.find((x) => x.mission_id === id);
  if (!m) return undefined;
  const list = neighborsNow();
  const other = list[0];
  const days = m.target && (m.target as { cadence?: string }).cadence === "weekly" ? 28 : 7;
  const invited = m.my_status === "invited";
  return {
    mission_id: m.mission_id,
    title: m.title,
    status: m.status,
    cadence: days === 28 ? "weekly" : "daily",
    window_days: days,
    target: m.target,
    overall_pct: 21,
    description: m.mission_id === "m-pm" ? "Consistency towards working on a project together." : "",
    version: m.version,
    my_commitment: m.my_commitment,
    my_role: m.my_role,
    my_status: m.my_status,
    members: invited ? [] : [
      { handle: "yuki", showed_up: m.mission_id === "m-pm" ? 0 : 3, window_days: days, streak: m.mission_id === "m-pm" ? 0 : 2, last_activity: null, next_step: m.mission_id === "m-pm" ? null : "Try the river loop", commitment: m.my_commitment, is_creator: m.my_role === "owner" },
      ...(other ? [{ handle: other.handle, showed_up: m.mission_id === "m-pm" ? 1 : 5, window_days: days, streak: 1, last_activity: null, next_step: m.mission_id === "m-pm" ? "Talk about an AI workflow" : null, commitment: "", is_creator: m.my_role !== "owner" }] : []),
    ],
    updates: invited ? [] : [
      ...(postedUpdates.get(m.mission_id) ?? []),
      ...(m.mission_id === "m-pm"
        ? [
            { id: "u1", kind: "note", text: `${other?.display_name ?? "Kiho"} said yes to “Write down the workflow”`, created_at: isoAt(0, 8, 5), author_name: other?.display_name ?? "Kiho" },
            { id: "u2", kind: "note", text: "Talk about an AI workflow", created_at: isoAt(-3, 10, 30), author_name: "Yuki" },
            { id: "u3", kind: "note", text: "Created the project", created_at: isoAt(-3, 10, 12), author_name: "Yuki" },
          ]
        : m.mission_id === "m-garden"
          ? [
              { id: "u1", kind: "progress", text: "Timber is ordered — the yard delivers Thursday.", created_at: isoAt(-1, 17, 20), author_name: "Sam" },
              { id: "u2", kind: "milestone", text: "Plan agreed. Four beds, two rows.", created_at: isoAt(-4, 9, 0), author_name: "Yuki" },
            ]
          : [
              { id: "u1", kind: "progress", text: "Did the long loop this morning, felt easy.", created_at: isoAt(-1, 7, 40), author_name: other?.display_name ?? "Neighbor" },
              { id: "u2", kind: "note", text: "Rain tomorrow — shall we go at 7 instead?", created_at: isoAt(-3, 20, 10), author_name: "Yuki" },
            ]),
    ],
  };
}

function threadList() {
  const list = neighborsNow();
  const at = (d: number, h: number) => isoAt(d, h);
  const t = (n: (typeof list)[number] | undefined, last: string, when: string | null, unread = 0) =>
    n ? { thread_id: `t-${n.friendship_id}`, friendship_id: n.friendship_id, display_name: n.display_name, handle: n.handle, avatar_hue: n.avatar_hue, unread, last_message: last, last_message_at: when, muted: false, agent_absorb_enabled: false } : null;
  return [
    t(list[0], "See you at the park on Sunday.", at(-2, 18), 1),
    t(list[3], "Thanks for the book! Nearly done.", at(-5, 21)),
    t(list[5], "Shall we do the market again?", at(-12, 9)),
  ].filter((x): x is NonNullable<typeof x> => !!x);
}

function absorbedList() {
  const list = neighborsNow();
  const a = list[0]?.handle ?? "aiko", b = list[1]?.handle ?? "ren";
  const kept = (id: string, from: string | null, label: string, day: number, extra: Record<string, string> = {}) => ({ id, source_kind: "spark", source_id: id, from_handle: from, label, absorbed_at: isoAt(day, 9), ...extra });
  return [
    kept("ab1", a, `Chat summary with ${list[0]?.display_name ?? "Aiko"}`, -19, { group_key: `chat:${a}`, kind_label: "Chat summary" }),
    kept("ab2", a, `Chat summary with ${list[0]?.display_name ?? "Aiko"}`, -14, { group_key: `chat:${a}`, kind_label: "Chat summary" }),
    kept("ab3", a, `Chat summary with ${list[0]?.display_name ?? "Aiko"}`, -8, { group_key: `chat:${a}`, kind_label: "Chat summary" }),
    kept("ab4", a, `Chat summary with ${list[0]?.display_name ?? "Aiko"}`, -3, { group_key: `chat:${a}`, kind_label: "Chat summary" }),
    kept("ab5", a, "Wants to channel climate awareness into their art practice.", -11),
    kept("ab6", a, "Prefers mornings for anything that needs thinking.", -22),
    kept("ab7", a, "Prefers mornings for anything that needs thinking.", -20),
    kept("ab8", b, "Keeps a spare bike pump in the hall cupboard.", -6),
    kept("ab9", "yuki", "Security work and family errands can share the same day — deep work just needs discipline, not an empty calendar.", -7),
  ];
}
/** Same shape apiFetch throws for a non-2xx response: body text in .message, plus .status. */
function httpError(status: number, body: Json): Error {
  const err = new Error(JSON.stringify(body));
  (err as Error & { status: number }).status = status;
  return err;
}

function fixtureMode(): string | null {
  return typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("fixture");
}

let wavesIn = [
  { friendship_id: "w-1", direction: "incoming", display_name: "Tomo", handle: "tomo", avatar_hue: 170, note: "We met at the running club!", created_at: "" },
];
const missionAsks = [
  { mission_id: "m-1", title: "Help Aiko move on Saturday", status: "active", target: {}, target_date: null as string | null, version: 1, my_commitment: "", my_status: "invited", my_role: "member" },
  { mission_id: "m-2", title: "Ren's 10k training buddy", status: "active", target: { cadence: "weekly" }, target_date: null as string | null, version: 1, my_commitment: "", my_status: "invited", my_role: "member" },
  { mission_id: "m-3", title: "Morning walks", status: "active", target: { cadence: "daily" }, target_date: null as string | null, version: 1, my_commitment: "Walk 20 min", my_status: "active", my_role: "owner" },
  { mission_id: "m-pm", title: "nbhd project management", status: "active", target: { cadence: "daily" }, target_date: null as string | null, version: 1, my_commitment: "", my_status: "active", my_role: "owner" },
  { mission_id: "m-garden", title: "Fix up the shared garden", status: "active", target: {}, target_date: null as string | null, version: 0, my_commitment: "", my_status: "active", my_role: "owner" },
];

function json(body: Json): Json {
  return JSON.parse(JSON.stringify(body));
}

function bodyOf(init?: RequestInit): Record<string, unknown> {
  try {
    return init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Returns a fixture response for `path`, or undefined to fall through to the network. */
// Mutable in-memory fixtures exercise writes without a backend. Reload resets them.
const dailyMarkdown = "# A little room for today\n\n### 07:40 — Yuki\nTook the long way to coffee. The city was unusually quiet, and I had time to notice.\n\n## Morning Report\nA clear morning, a little breathing room. Your first meeting is at **10:00**.\n\n## Today’s notes\n- [x] Sent the revised kitchen measurements\n- [ ] Ask about the counter template visit\n\n### 12:15\nAn easy run by the river. Less about the pace, more about getting outside.\n";
const longDayMarkdown = dailyMarkdown + [
  ["13:30", "Lunch outside, away from the screen. The afternoon felt easier after a real pause."],
  ["14:45", "Finished the first draft. There are still rough edges, but the shape is finally there."],
  ["16:10", "A call with a friend. We made a small plan for the weekend and left the rest open."],
  ["18:20", "Walked home by the river. The light changed while I was crossing the bridge."],
  ["20:30", "Dinner, a little music, and a tidy kitchen. Nothing urgent needs to follow me into tonight."],
  ["22:10", "Last thought of a long day: leave a little room for tomorrow. Time to put the notebook down."],
].map(([time, body]) => `\n### ${time} — Yuki\n${body}\n`).join("");
const journalDocuments = new Map<string, string>();
let conflictShown = false;
let taskFailureShown = false;
let fixtureTasks: JournalTask[] = [
  { id: "t1", title: "Book the counter template visit", due_date: isoDay(-1), parent_goal_id: null, status: "open" },
  { id: "t2", title: "Make room for an easy long run", due_date: isoDay(2), parent_goal_id: "g1", status: "open" },
  { id: "t3", title: "Try the route with a friend", due_date: null, parent_goal_id: "g1", status: "open" },
  ...["Confirm the sink cutout", "Send 2–3 Osaka outreach DMs", "Finish The Core Insight section", "Book the physio check-in", "Buy new running shoes", "Draft the launch post", "Renew the car insurance", "Send the kitchen invoice", "Pick up the passport forms", "Plan the weekend route"].map((title, index) => ({ id: `due-${index}`, title, due_date: isoDay(Math.min(index, 7)), parent_goal_id: index % 3 === 0 ? "g1" : null, status: "open" })),
  ...["Study on Burp Academy", "Talk about an AI workflow", "Research the laptop write-off", "Review the Academy Watch report", "Research 10 more Osaka teams", "Gather citizenship documents", "Start the passport forms", "Follow up with the team", "Review the weekly plan", "Check the shared document", "Reply to the launch feedback", "Remove the old class reminder", "Accept the team invite", "Sort the garage shelves"].map((title, index) => ({ id: `any-${index}`, title, due_date: null, parent_goal_id: null, status: "open" })),
  { id: "t4", title: "Choose a local 10k", due_date: null, parent_goal_id: "g1", status: "done" },
  { id: "t5", title: "Send the revised kitchen measurements", due_date: null, parent_goal_id: null, status: "done" },
].map((task) => ({ description: "", pillar: "", related_ref: "", created_at: isoAt(-7, 9), updated_at: isoAt(0, 9), ...task, status: task.status as JournalTask["status"], completed_at: task.status === "done" ? localMonday() : null }));
let pendingSuggestions = [{ id: "suggestion-1", kind: "task", text: "Find a quiet café for a writing morning", confidence: "0.85", source_date: isoDay(-1), created_at: isoAt(-1, 20) }];

// Chart your galaxy (/constellation/play): ~250 lessons over 8 clusters with real
// edges, so the flight can be screenshotted at scale. Texts are built from a few
// patterns per cluster; ids and stages are deterministic.
const GALAXY_CLUSTERS: [string, string[], string[]][] = [
  ["Health", ["sleep", "a walk", "the gym", "stretching", "water", "breakfast"], ["Protect {s} before the day gets loud.", "When {s} slips, shrink it instead of skipping it.", "Plan {s} the night before and it happens.", "After ten days without {s}, restart lighter than you think.", "Move {s}, don't drop it.", "{S} is the first workout."]],
  ["Work", ["the first hour", "the rough draft", "a hard email", "the weekly review", "deep work", "a small ship"], ["Start {s} before you feel ready.", "Guard {s} from meetings.", "Finish {s} first, polish later.", "Say no to the good to keep {s}.", "Put {s} on the calendar or it won't exist.", "Ship {s} small, ship it often."]],
  ["Growth", ["feedback", "a hard question", "boredom", "the slow week", "a mistake", "a new habit"], ["Ask for {s} you fear.", "Notice what {s} is telling you.", "Reflect on {s} weekly, not daily.", "Sit with {s} before fixing it.", "Treat {s} as data, not a verdict.", "Give {s} a month before judging."]],
  ["Family", ["dinner", "the weekend", "a sick day", "the school run", "bedtime", "a long call"], ["Division of labor beats doing everything together on {s}.", "Keep {s} phone-free.", "Say the plan for {s} out loud.", "Protect {s} from work creep.", "Let {s} be simple.", "Ask about {s} before offering fixes."]],
  ["Craft", ["the first paragraph", "the outline", "the ugly version", "the second pass", "a constraint", "the ending"], ["Cut {s}.", "Make {s} work, then make it good.", "Steal {s} like an artist.", "Write {s} before you feel ready.", "Give {s} a deadline.", "Rest before {s}, not after."]],
  ["Money", ["the subscription", "a big purchase", "the tax folder", "the emergency fund", "the invoice", "a raise"], ["Sleep on {s}.", "Automate {s} so you never decide twice.", "Review {s} on the first of the month.", "Ask for {s} with numbers, not feelings.", "Send {s} the day the work is done.", "Name {s} and it stops being scary."]],
  ["Friends", ["a reply", "the group chat", "an invitation", "a quiet friend", "the reunion", "a favor"], ["Send {s} today, not perfectly.", "Leave {s} on read less often.", "Say yes to {s} before you're sure.", "Check on {s} first.", "Plan {s} two months out.", "Ask for {s}; people like being needed."]],
  ["Place", ["the morning light", "the river path", "a new café", "the neighborhood", "the balcony", "the commute"], ["Walk {s} without headphones.", "Learn {s} one street at a time.", "Let {s} set the pace.", "Sit in {s} for ten minutes.", "Photograph {s} once a season.", "Say hello in {s}."]],
];
const GALAXY_STAGES = ["proto", "proto", "proto", "ignited", "proto", "radiant", "ignited", "proto", "supernova", "proto", "ignited", "proto"];
const galaxyStars = GALAXY_CLUSTERS.flatMap(([label, subjects, patterns], ci) =>
  patterns.flatMap((pattern, pi) =>
    subjects.map((subject, si) => {
      const n = ci * 36 + pi * 6 + si;
      if (n % 8 === 7 && ci % 2 === 1) return null; // thin a few so clusters differ in size
      const text = pattern.replace("{s}", subject).replace("{S}", subject.charAt(0).toUpperCase() + subject.slice(1));
      const stage = GALAXY_STAGES[(n * 7) % GALAXY_STAGES.length];
      return {
        id: 1000 + n,
        text,
        tags: [label.toLowerCase(), subject.split(" ").pop() ?? subject],
        cluster_id: ci + 1,
        cluster_label: label,
        star_stage: stage,
        x: 60 + si * 70 + ((n * 37) % 23),
        y: 40 + pi * 55 + ((n * 53) % 19),
        journal_count: stage === "proto" ? 0 : 1 + (n % 3),
        connection_count: 2,
        last_tutored_at: null,
        last_visited_at: null,
        galaxy_note: "",
        source_type: n % 5 === 0 ? "fuel" : "journal",
        context: n % 3 === 0 ? `You wrote this after ${subject} went sideways twice in a row. Naming it got you back the same week.` : "",
        created_at: isoAt(-((n * 11) % 90) - 1, 9),
      };
    }).filter((s): s is NonNullable<typeof s> => s !== null),
  ),
);
const galaxyEdges = galaxyStars.flatMap((s, i) => {
  const out: { source: number; target: number; similarity: number; connection_type: string }[] = [];
  const next = galaxyStars[(i + 6) % galaxyStars.length];
  if (next.cluster_id === s.cluster_id) out.push({ source: s.id, target: next.id, similarity: 0.6 + ((i * 13) % 30) / 100, connection_type: "similar" });
  if (i % 9 === 0) out.push({ source: s.id, target: galaxyStars[(i + 41) % galaxyStars.length].id, similarity: 0.52, connection_type: "builds_on" });
  return out;
});
const starNotes = new Map<number, { id: string; star: number; text: string; entry_type: string; tags: string[]; created_at: string }[]>([
  [1002, [{ id: "n-1", star: 1002, text: "Tuesday mornings are the ones that slip.", entry_type: "revisit", tags: [], created_at: isoAt(-4, 21) }]],
]);

// Constellation night sky (/constellation, Open Sky): ~120 lessons over 10
// clusters whose newest lessons span a year (so depth reads), plus two loose
// lessons and three awaiting review. Deterministic texts and dates.
const SKY_CLUSTERS: [string, number, string[]][] = [
  ["Workout Planning", 2, ["When a scheduled mobility session gets skipped, move it, don't drop it.", "Planning the gym visit the night before makes it happen.", "When a gym gap exceeds ten days, restart lighter than you think.", "Shrink the plan when life gets loud, don't skip it.", "Sleep is the first workout.", "Two short sessions beat one heroic one you never start.", "Warm up longer on cold mornings; the knee remembers.", "Log the set while it's fresh, not after the shower.", "A walk counts when the week is too full for the gym.", "Book the class; paying for it is half the motivation.", "Stretch before bed on heavy leg days.", "Rest days are part of the plan, not a break from it.", "Tired legs on Monday mean Sunday was too long."]],
  ["Nutrition Tracking", 9, ["Logging body weight the morning after a heavy meal shows commitment to data over ego — the trend is the story.", "Protein first at breakfast makes the afternoon slump smaller.", "Weigh in before coffee, not after, or the week reads noisy.", "Prep lunch on Sunday or Wednesday becomes takeaway.", "Water before the second coffee.", "A snack in the bag stops the four o'clock raid.", "Eat the vegetables first when you're hungriest.", "Track the weekend too; that's where the drift hides.", "Dinner after nine costs the next morning's energy.", "One treat planned beats three unplanned.", "Shopping hungry fills the cart with regret."]],
  ["Juggling Projects And Life", 20, ["Deep work doesn't need an empty calendar, just a protected first hour.", "Errands and focus can share a day if the order is right.", "Kids' pickup is a fixed star; plan around it, not against it.", "Write tomorrow's first task before closing the laptop.", "Say the week's one big thing out loud on Monday.", "A tired afternoon is for admin, not for decisions.", "Batch the small replies into one sitting.", "When two deadlines collide, ask which one can move.", "Leave a gap after every meeting.", "The calendar is a promise to yourself, too.", "Finish one thing before opening the next tab.", "Friday afternoon is for tidying, not starting."]],
  ["Sleep And Recovery", 38, ["Screens off at ten makes the whole next day easier.", "A short nap before three beats a third coffee.", "When you're tired, lower the bar instead of skipping the habit.", "Same wake time on weekends keeps Monday gentle.", "A cool room sleeps better than a warm one.", "Late workouts cost an hour of sleep.", "Write the worry down so it stops circling at night.", "Recovery weeks are planned, not earned.", "Alcohol steals the second half of the night.", "Energy follows light; get outside before nine.", "Exhausted is a signal, not a character flaw."]],
  ["App Store Identity Setup", 70, ["Accept the team invite before building, or the build signs with the wrong identity.", "Screenshot every certificate step; you'll need it again in a year.", "Keep the bundle IDs in one note, not three.", "Renew the certificate a month early.", "Test the release build on a real phone before submitting.", "Read the rejection twice before replying.", "Write the review notes like the reviewer has five minutes.", "Keep a clean test account ready for App Review."]],
  ["Bill Payment Discipline", 105, ["Pay the card the day the statement lands, not the day it's due.", "A money check-in on Sunday beats a panic on Thursday.", "Automate the rent so it never needs a decision.", "Cancel the trial the day you start it.", "One folder for every receipt that might matter at tax time.", "Round up the savings transfer; you won't miss it.", "Look at the subscriptions every quarter.", "Call the provider before the price rise lands.", "Name the emergency fund and it stays untouched.", "Invoice the day the work is done."]],
  ["Writing Practice", 150, ["Write the ugly first draft before breakfast.", "Cut the first paragraph; the piece usually starts at the second.", "Read it out loud and the clumsy sentences confess.", "Stop mid-sentence so tomorrow starts easy.", "A constraint makes the page less scary.", "Rest before the second pass, not after.", "Steal the structure, not the sentences.", "Three hundred words a day adds up to a book.", "Show the draft to one kind reader first.", "Delete the adverbs you notice on the third read.", "Give the ending a deadline.", "Tired writing is still writing; edit it tomorrow."]],
  ["Family Logistics", 200, ["Say the weekend plan out loud on Thursday.", "A shared list beats remembering for each other.", "Pack the school bags the night before.", "Split the jobs instead of doing everything together.", "Keep dinner phone-free, even when it's rushed.", "Book the dentist for everyone in one go.", "A spare key at the neighbor's saves an evening.", "Ask about their day before offering fixes.", "Let bedtime be simple on long days.", "Put the birthdays in the calendar in January.", "The sick day plan works best when it's made on a healthy day."]],
  ["Crisis Response", 280, ["When a partner gets sick while travelling, split the jobs instead of doing everything together.", "In a crisis, write the next three steps, not the whole plan.", "Ask for a quiet friend; people like being needed.", "Keep the insurance number in your phone, not the drawer.", "Eat something even when you're not hungry; the day is long.", "Tell work early and plainly.", "One person talks to the doctors, one to the family.", "Sleep in shifts when it goes on for days.", "Afterwards, write down what helped while you still remember."]],
  ["Deep Work Habits", 350, ["Guard the first hour from meetings.", "Phone in another room is worth an hour of willpower.", "Start before you feel ready.", "One tab, one task, one timer.", "A walk unsticks what staring can't.", "Decide the next step before stopping.", "Turn off notifications for the whole morning.", "Protect the slow week; it's where the good ideas come from.", "Say no to the good to keep the great.", "When tired, switch to the easy part instead of quitting.", "Ship small, ship often."]],
];
const SKY_SOURCES = ["journal", "conversation", "reflection", "journal", "experience", "article"];
const skyLessons = [
  ...SKY_CLUSTERS.flatMap(([label, newest, texts], ci) =>
    texts.map((text, i) => {
      const id = 2000 + ci * 40 + i;
      // Newest lesson on `newest` days ago; earlier ones stretch back a few weeks.
      const age = newest + (texts.length - 1 - i) * (3 + (ci % 3)) + ((id * 7) % 3);
      return { id, text, context: "", tags: [label.toLowerCase()], cluster_id: ci + 1, cluster_label: label, source_type: SKY_SOURCES[(id * 5) % SKY_SOURCES.length], source_ref: "", x: null, y: null, created_at: isoAt(-(i === texts.length - 1 ? newest : age), 9) };
    }),
  ),
  { id: 2900, text: "Notice which days you hum.", context: "", tags: ["joy"], cluster_id: null, cluster_label: "", source_type: "reflection", source_ref: "", x: null, y: null, created_at: isoAt(-5, 20) },
  { id: 2901, text: "A slow morning once a week pays for itself.", context: "", tags: ["rest"], cluster_id: null, cluster_label: "", source_type: "journal", source_ref: "", x: null, y: null, created_at: isoAt(-12, 8) },
];
const SKY_MEANING: Record<string, string[]> = {
  tired: ["sleep", "slump", "tired", "exhausted", "a nap", "energy"],
  money: ["card", "pay", "money", "invoice", "savings", "subscriptions"],
  family: ["partner", "kids", "family", "bedtime", "dinner"],
  focus: ["deep work", "focus", "first hour", "one task"],
};
const skyPending = [
  { id: 2950, text: "Morning pages go easier with the window open.", context: "", tags: [], cluster_id: null, cluster_label: "", source_type: "journal", source_ref: "", status: "pending", suggested_at: isoAt(-1, 21), approved_at: null, created_at: isoAt(-1, 21) },
  { id: 2951, text: "Ask the question you think is obvious.", context: "", tags: [], cluster_id: null, cluster_label: "", source_type: "conversation", source_ref: "", status: "pending", suggested_at: isoAt(-2, 18), approved_at: null, created_at: isoAt(-2, 18) },
  { id: 2952, text: "Leave the bike by the door and you'll ride it.", context: "", tags: [], cluster_id: null, cluster_label: "", source_type: "journal", source_ref: "", status: "pending", suggested_at: isoAt(-2, 9), approved_at: null, created_at: isoAt(-2, 9) },
];

// `?fixture=big`: the shape of a real, active account — 263 lessons in 53
// clusters, a few big ones (15–30) and many small ones (2–6), newest lessons
// spread over a year but bunched in the last three months. Deterministic.
const BIG_LABELS = ["Workout Planning", "Nutrition Tracking", "Juggling Projects And Life", "Sleep And Recovery", "Deep Work Habits", "Family Logistics", "Writing Practice", "Bill Payment Discipline", "App Store Identity Setup", "Crisis Response", "Morning Routine", "Client Communication", "Running Form", "Meal Prep", "Language Study", "Travel Planning", "Home Repairs", "Tax Season", "Hiring Conversations", "Code Review Habits", "Launch Week", "Saying No", "Friendship Upkeep", "Weekend Rest", "Commute Reading", "Garden Care", "Kids' Homework", "Pricing Decisions", "Inbox Zero", "Public Speaking", "Car Maintenance", "Anxiety Before Calls", "Budget Reviews", "Meeting Hygiene", "Learning Guitar", "Cooking For Friends", "Phone Boundaries", "Team Retros", "Moving House", "Doctor Visits", "Side Project Scope", "Rainy Day Plans", "Gift Giving", "Long Flights", "Back Pain", "Newsletter Writing", "Debugging Patience", "Coffee Limits", "Evening Walks", "Visa Paperwork", "Negotiating Rent", "Birthday Planning", "Stretching"];
const BIG_SUBJECTS = ["the plan", "the first step", "the hard part", "the routine", "the list", "the next hour", "the small win", "the check-in"];
const BIG_PATTERNS = [
  "When you're tired, shrink {s} instead of skipping it.",
  "Write {s} down the night before and it happens.",
  "Protect {s} before the day gets loud.",
  "Low energy days still count if {s} gets done.",
  "Ask for help with {s} earlier than feels comfortable.",
  "Sleep first; {s} goes better rested.",
  "Say {s} out loud on Monday.",
  "Finish {s} before opening anything new.",
  "Exhausted is a signal to lower the bar for {s}, not to quit.",
  "Review {s} on Sunday, not in a Thursday panic.",
  "Make {s} small enough to start today.",
  "Leave a gap after {s}.",
];
const bigSky = (() => {
  const big = [30, 24, 19, 16, 15];
  const small = [2, 3, 2, 4, 3, 6, 2, 3, 5, 2, 4, 3];
  const sizes = BIG_LABELS.map((_, ci) => (ci < big.length ? big[ci] : small[(ci - big.length) % small.length] + (ci < big.length + 3 ? 1 : 0)));
  const nodes = BIG_LABELS.flatMap((label, ci) => {
    const r = ((ci * 2654435761) % 1000) / 1000;
    // ~70% of clusters had their newest lesson in the last 3 months (bunched recent), the rest over the year.
    const newest = ci % 10 < 7 ? Math.floor(90 * Math.pow(r, 1.8)) : 90 + Math.floor(275 * r);
    return Array.from({ length: sizes[ci] }, (_, i) => {
      const id = 5000 + ci * 40 + i;
      const back = sizes[ci] - 1 - i;
      const text = BIG_PATTERNS[(ci * 5 + i) % BIG_PATTERNS.length].replace("{s}", BIG_SUBJECTS[(ci + i) % BIG_SUBJECTS.length]);
      return { id, text, context: "", tags: [label.toLowerCase()], cluster_id: ci + 1, cluster_label: label, source_type: SKY_SOURCES[(id * 5) % SKY_SOURCES.length], source_ref: "", x: null, y: null, created_at: isoAt(-(newest + back * (2 + (ci % 5)) + (back ? (id * 7) % 3 : 0)), 9) };
    });
  });
  return { nodes, clusters: BIG_LABELS.map((label, ci) => ({ id: ci + 1, label, count: sizes[ci], tags: [label.toLowerCase()] })) };
})();

function bigFixture(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("fixture") === "big";
}

export function fixtureResponse(path: string, init?: RequestInit): Json | undefined {
  const method = (init?.method ?? "GET").toUpperCase();
  const url = new URL(path, "http://fixture.local");
  const p = url.pathname;
  const isEmpty = empty();
  if (p.startsWith("/api/v1/friends/")) {
    const body = bodyOf(init);
    if (fixtureMode() !== "v1") {
      const project = projectFixture(p, method, url, body);
      if (project !== undefined) {
        // A project made here shows up in the Neighborhood list too.
        const made = (project as { mission_id?: string }).mission_id;
        if (p === "/api/v1/friends/missions/" && made) missionAsks.push({ mission_id: made, title: String(body.title ?? "New project"), status: "active", target: {}, target_date: null, version: 0, my_commitment: "", my_status: "active", my_role: "owner" });
        if (/\/project-drafts\/[^/]+\/publish\/$/.test(p) && made) missionAsks.push({ mission_id: made, title: projectFixtureTitle(made) ?? "New project", status: "active", target: {}, target_date: null, version: 0, my_commitment: "", my_status: "active", my_role: "owner" });
        return json(project);
      }
    }
    const cluster = clusterFixture(p, method, body, (threadId) =>
      clusterDetail(threadId.slice(2))?.members.filter((m) => !m.is_me).map((m) => ({ handle: m.handle, display_name: m.display_name, avatar_hue: m.avatar_hue })),
    );
    if (cluster !== undefined) return json(cluster);
  }
  if (p === "/api/v1/lessons/galaxy/") {
    return json({
      stars: isEmpty ? [] : galaxyStars,
      edges: isEmpty ? [] : galaxyEdges,
      clusters: isEmpty ? [] : GALAXY_CLUSTERS.map(([label], ci) => ({ id: ci + 1, label, count: galaxyStars.filter((s) => s.cluster_id === ci + 1).length, tags: [label.toLowerCase()] })),
    });
  }
  const starJournal = p.match(/^\/api\/v1\/lessons\/(\d+)\/journal\/(create\/)?$/);
  if (starJournal) {
    const starId = Number(starJournal[1]);
    const list = starNotes.get(starId) ?? [];
    if (starJournal[2] && method === "POST") {
      const note = { id: `n-${starId}-${list.length + 1}`, star: starId, text: String(bodyOf(init).text ?? ""), entry_type: "revisit", tags: [], created_at: new Date().toISOString() };
      starNotes.set(starId, [note, ...list]);
      return json(note);
    }
    return json(list);
  }

  if (p === "/api/v1/auth/me/") {
    // `?fixture=legacy` = a tenant without the web redesign (old shell).
    const legacy = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("fixture") === "legacy";
    if (fixtureMode() === "v1") return json({ ...me, tenant: { ...tenant, projects_v2_enabled: false } });
    return json(legacy ? { ...me, tenant: { ...tenant, web_redesign: false } } : me);
  }
  if (p === "/api/v1/chat/messages/") {
    return json({ messages: isEmpty ? [] : feed, cursor: null });
  }
  if (p === "/api/v1/fuel/sleep/") {
    if (method === "POST") {
      const b = bodyOf(init);
      const row = {
        id: `sleep-new-${Date.now()}`,
        date: String(b.date ?? isoDay(0)),
        duration_hours: Number(b.duration_hours ?? 0).toFixed(2),
        quality: (b.quality as number | null) ?? null,
        notes: String(b.notes ?? ""),
        created_at: new Date().toISOString(),
      };
      sleep = [row, ...sleep];
      return json(row);
    }
    return json(isEmpty ? [] : sleep);
  }
  const sleepDetail = p.match(/^\/api\/v1\/fuel\/sleep\/([^/]+)\/$/);
  if (sleepDetail) {
    const id = sleepDetail[1];
    if (method === "PATCH") {
      const b = bodyOf(init);
      if (String(b.notes ?? "").includes("fail")) {
        throw Object.assign(new Error("Couldn't save (fixture failure)."), { status: 400 });
      }
      sleep = sleep.map((r) =>
        r.id === id
          ? {
              ...r,
              ...(b.duration_hours !== undefined ? { duration_hours: Number(b.duration_hours).toFixed(2) } : {}),
              ...(b.quality !== undefined ? { quality: b.quality as number | null } : {}),
              ...(b.notes !== undefined ? { notes: String(b.notes) } : {}),
              ...(b.date !== undefined ? { date: String(b.date) } : {}),
            }
          : r,
      );
      return json(sleep.find((r) => r.id === id));
    }
    if (method === "DELETE") {
      sleep = sleep.filter((r) => r.id !== id);
      return json({});
    }
  }
  if (p === "/api/v1/fuel/body-weight/") {
    if (method === "POST") {
      const b = bodyOf(init);
      const row = {
        id: `bw-new-${Date.now()}`,
        date: String(b.date ?? isoDay(0)),
        weight_kg: Number(b.weight_kg ?? 0).toFixed(1),
        created_at: new Date().toISOString(),
      };
      bodyWeight = [row, ...bodyWeight];
      return json(row);
    }
    return json(isEmpty ? [] : bodyWeight);
  }
  const bwDetail = p.match(/^\/api\/v1\/fuel\/body-weight\/([^/]+)\/$/);
  if (bwDetail) {
    const id = bwDetail[1];
    if (method === "PATCH") {
      const b = bodyOf(init);
      if (Number(b.weight_kg) > 400) {
        throw Object.assign(new Error("Weight looks wrong (fixture failure)."), { status: 400 });
      }
      bodyWeight = bodyWeight.map((r) =>
        r.id === id
          ? {
              ...r,
              ...(b.weight_kg !== undefined ? { weight_kg: Number(b.weight_kg).toFixed(1) } : {}),
              ...(b.date !== undefined ? { date: String(b.date) } : {}),
            }
          : r,
      );
      return json(bodyWeight.find((r) => r.id === id));
    }
    if (method === "DELETE") {
      bodyWeight = bodyWeight.filter((r) => r.id !== id);
      return json({});
    }
  }
  if (p === "/api/v1/fuel/workouts/") return json(isEmpty ? [] : workouts);
  if (p === "/api/v1/fuel/workouts/count/") return json({ count: isEmpty ? 0 : 42 });
  if (p === "/api/v1/fuel/resting-hr/") {
    return json(isEmpty ? [] : [58, 57, 59, 56, 57, 55, 56].map((bpm, i) => ({ id: `rhr-${i}`, date: isoDay(-i * 2), bpm, created_at: isoAt(-i * 2, 7) })));
  }
  if (p === "/api/v1/fuel/weekly-summary/") {
    return json({
      week_start: isoDay(-4),
      week_end: isoDay(2),
      by_category: isEmpty ? [] : [{ category: "strength", count: 1, total_minutes: 55 }, { category: "cardio", count: 1, total_minutes: 40 }],
      totals: isEmpty ? { sessions: 0, minutes: 0 } : { sessions: 2, minutes: 95 },
    });
  }
  if (p === "/api/v1/fuel/profile/") {
    return json({
      id: "fp-1", onboarding_status: "completed", fitness_level: "intermediate",
      goals: ["Run a 10k", "Stay strong"], limitations: [], equipment: ["barbell", "dumbbells"],
      days_per_week: 4, additional_context: "", distance_unit: "km",
      created_at: "2025-06-01T00:00:00Z", updated_at: isoAt(-3, 9),
    });
  }
  if (p === "/api/v1/journal/tree/") {
    return json([
      { kind: "daily", label: "Daily notes", items: isEmpty ? [] : [0, -1, -2].map((d) => ({ slug: isoDay(d), title: isoDay(d), updated_at: isoAt(d, 21) })) },
      { kind: "weekly", label: "Weekly reviews", items: isEmpty ? [] : [{ slug: "2026-w38", title: "Week 38", updated_at: isoAt(-5, 20) }] },
      { kind: "project", label: "Projects", items: isEmpty ? [] : [{ slug: "home-renovation", title: "Home Renovation", updated_at: isoAt(-1, 12) }] },
      { kind: "goal", label: "Goals", items: isEmpty ? [] : [{ slug: "run-a-10k", title: "Run a 10k", updated_at: isoAt(-2, 8) }] },
      { kind: "ideas", label: "Ideas", items: [] },
    ]);
  }
  if (p === "/api/v1/journal/documents/") {
    const projects = [
      { id: "project-home", kind: "project", slug: "home-renovation", title: "Home Renovation", updated_at: isoAt(-1, 12) },
      { id: "project-academy", kind: "project", slug: "academy-watch-japan", title: "Academy Watch Japan", updated_at: isoAt(-4, 9) },
    ];
    return json(isEmpty ? [] : projects.filter((doc) => !url.searchParams.get("kind") || doc.kind === url.searchParams.get("kind")));
  }
  const doc = p.match(/^\/api\/v1\/journal\/documents\/([^/]+)\/([^/]+)\/(append\/|blocks\/replace\/)?$/);
  if (doc) {
    const [, kind, slug, action] = doc;
    const bodies: Record<string, string> = {
      daily: new URLSearchParams(window.location.search).get("fixture") === "journal-long" ? longDayMarkdown : dailyMarkdown,
      project: "Kitchen first, then the back porch.\n\n## Milestones\n\n- [x] Demo and haul-away\n- [ ] Counter template, fabricator Friday\n",
      goal: "Base-building block: four days a week, mostly zone 2.\n",
      weekly: "## Wins\n\n- Four sessions\n\n## Lessons\n\n- Reflect weekly, not daily\n",
    };
    const key = `${kind}/${slug}`;
    const document = () => ({ id: `doc-${kind}-${slug}`, kind, slug, title: slug, markdown: journalDocuments.get(key) ?? (isEmpty ? "" : bodies[kind] ?? ""), created_at: isoAt(-1, 8), updated_at: isoAt(0, 8) });
    if (method === "POST" && action === "blocks/replace/") {
      const body = bodyOf(init);
      let blocks = splitJournalBlocks(document().markdown);
      const index = Number(body.index);
      if (!conflictShown && new URLSearchParams(window.location.search).get("fixture") === "journal-conflict") {
        conflictShown = true;
        blocks[index] += "A new detail arrived from your assistant.\n";
        journalDocuments.set(key, blocks.join(""));
        blocks = splitJournalBlocks(document().markdown);
      }
      if (blocks[index] === undefined || blocks[index] !== body.original) {
        throw Object.assign(new Error(JSON.stringify({ error: "block_changed", document: document() })), { status: 409 });
      }
      let replacement = String(body.replacement);
      if (replacement && index < blocks.length - 1 && !replacement.endsWith("\n")) replacement += "\n\n";
      blocks[index] = replacement;
      journalDocuments.set(key, blocks.join(""));
    } else if (method === "POST" && action === "append/") {
      const time = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
      journalDocuments.set(key, `${document().markdown}\n\n### ${time} — Yuki\n${bodyOf(init).content}\n`);
    } else if (method === "PATCH") {
      journalDocuments.set(key, String(bodyOf(init).markdown ?? document().markdown));
    }
    return json(document());
  }
  if (p === "/api/v1/journal/goals/") return json(isEmpty ? [] : [{ id: "g1", title: "Run a 10k" }]);
  if (p === "/api/v1/journal/tasks/") {
    if (method === "POST") {
      const body = bodyOf(init);
      const created: JournalTask = { id: `task-${fixtureTasks.length + 1}`, title: String(body.title), due_date: body.due_date ? String(body.due_date) : null, parent_goal_id: body.parent_goal_id ? String(body.parent_goal_id) : null, status: "open", completed_at: null, description: "", pillar: "", related_ref: "", created_at: isoAt(0, 9), updated_at: isoAt(0, 9) };
      fixtureTasks = [...fixtureTasks, created];
      return json(created);
    }
    const status = url.searchParams.get("status");
    const after = url.searchParams.get("completed_after");
    return json(isEmpty ? [] : fixtureTasks.filter((task) => (!status || task.status === status) && (!after || (task.completed_at && new Date(task.completed_at) >= new Date(after)))));
  }
  const taskAction = p.match(/^\/api\/v1\/journal\/tasks\/([^/]+)\/(complete|reopen)\/$/);
  if (taskAction && method === "POST") {
    if (!taskFailureShown && new URLSearchParams(window.location.search).get("fixture") === "task-failure") {
      taskFailureShown = true;
      throw Object.assign(new Error("Task update failed (fixture)."), { status: 503 });
    }
    fixtureTasks = fixtureTasks.map((task) => task.id === taskAction[1] ? { ...task, status: taskAction[2] === "complete" ? "done" : "open", completed_at: taskAction[2] === "complete" ? new Date().toISOString() : null } : task);
    return json(fixtureTasks.find((task) => task.id === taskAction[1]));
  }
  const extraction = p.match(/^\/api\/v1\/journal\/extractions\/([^/]+)\/(approve|dismiss)\/$/);
  if (extraction && method === "POST") {
    const suggestion = pendingSuggestions.find((item) => item.id === extraction[1]);
    if (suggestion && extraction[2] === "approve") fixtureResponse("/api/v1/journal/tasks/", { method: "POST", body: JSON.stringify({ title: suggestion.text }) });
    pendingSuggestions = pendingSuggestions.filter((item) => item.id !== extraction[1]);
    return json({ status: "ok" });
  }
  if (p === "/api/v1/journal/status/") {
    return json({
      as_of: new Date().toISOString(), typed_lifecycle: true, finance_enabled: false,
      open_tasks: isEmpty ? [] : [
        { id: "t1", title: "Book the counter template visit", status: "open", due_date: isoDay(1), pillar: "home" },
        { id: "t2", title: "Try the route with a friend", status: "open", due_date: null, pillar: "fitness" },
      ],
      active_goals: isEmpty ? [] : [{ id: "g1", title: "Run a 10k", status: "active", target_date: isoDay(40), pillar: "fitness" }],
      obligations: [],
    });
  }
  if (p === "/api/v1/dashboard/horizons/") {
    return json({
      north_star: isEmpty ? [] : [{ id: "ns1", source: "purpose", statement: "Build a body and a life that can go the distance. Steady, not frantic.", pillars: ["fuel", "journal"], status: "confirmed", origin: "chat", created_at: "2026-08-01T00:00:00Z" }],
      goals: isEmpty ? [] : [{
        id: "g1", title: "Run a 10k", slug: "run-a-10k", preview: "Base-building block: four days a week, mostly zone 2.", status: "active",
        tasks: [
          { id: "gt1", title: "Choose a local 10k", status: "done", due_date: null },
          { id: "gt2", title: "Find a comfortable running rhythm", status: "done", due_date: null },
          { id: "gt3", title: "Make room for an easy long run", status: "open", due_date: null },
          { id: "gt4", title: "Try the route with a friend", status: "open", due_date: null },
        ],
        created_at: "2026-08-10T00:00:00Z", updated_at: isoAt(-2, 8),
      }],
      pending_extractions: isEmpty ? [] : pendingSuggestions,
      weekly_pulse: isEmpty ? [] : [{ week_start: isoDay(-11), week_end: isoDay(-5), week_rating: "thumbs-up", top_win: "Four sessions and no skipped mornings" }],
      weekly_documents: [],
      mood_trend: isEmpty ? [] : Array.from({ length: 14 }, (_, i) => ({ date: isoDay(-13 + i), mood: ["steady", "good", "low", "good"][i % 4], energy: String(5 + (i % 4)) })),
      momentum: Array.from({ length: 14 }, (_, i) => ({ date: isoDay(-13 + i), message_count: isEmpty ? 0 : (i * 7) % 11, has_journal: !isEmpty && i % 2 === 0 })),
      current_streak: isEmpty ? 0 : 14,
      assistant_insights: isEmpty ? [] : [
        { id: "ai1", pillar: "journal", topic_slug: "deep-work", topic_display_name: "Deep work", statement: "You write the most on focused mornings.", status: "open", confidence: 0.72, created_at: isoAt(-3, 9), last_confirmed_at: null },
        { id: "ai2", pillar: "fuel", topic_slug: "fitness", topic_display_name: "Fitness", statement: "Cardio days line up with brighter mood entries.", status: "confirmed", confidence: 0.81, created_at: isoAt(-9, 9), last_confirmed_at: isoAt(-2, 9) },
      ],
      topic_signals: [],
    });
  }
  if (p === "/api/v1/lessons/constellation/") {
    if (bigFixture()) return json({ ...bigSky, edges: [], affinity_edges: [] });
    return json({
      nodes: isEmpty ? [] : skyLessons,
      edges: [],
      affinity_edges: [],
      clusters: isEmpty ? [] : SKY_CLUSTERS.map(([label], ci) => ({ id: ci + 1, label, count: skyLessons.filter((l) => l.cluster_id === ci + 1).length, tags: [label.toLowerCase()] })),
    });
  }
  if (p === "/api/v1/lessons/search/") {
    // Stand-in for meaning search (the real one ranks by embeddings): lessons
    // sharing a word with the query's "meaning" score high, the rest low, so
    // the page's similarity floor is exercised too.
    const q = (url.searchParams.get("q") ?? "").trim().toLowerCase();
    const limit = Number(url.searchParams.get("limit") ?? 10);
    const words = [q, ...(SKY_MEANING[q] ?? q.split(/\s+/))].filter((w) => w.length > 2);
    const scored = (bigFixture() ? bigSky.nodes : skyLessons).map((l) => {
      const text = l.text.toLowerCase();
      const hits = words.filter((w) => text.includes(w)).length;
      return { ...l, status: "approved", suggested_at: l.created_at, approved_at: l.created_at, similarity: hits ? Math.min(0.52, 0.36 + hits * 0.04 - (l.id % 5) * 0.01) : 0.1 + ((l.id * 37) % 13) / 100 };
    });
    return json(scored.sort((a, b) => b.similarity - a.similarity).slice(0, limit));
  }
  if (p === "/api/v1/datebook/agenda/") {
    // `?agenda=stale` = last complete sync too old to cover the week; `?agenda=disabled` = not connected.
    const mode = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("agenda") : null;
    if (mode === "disabled") return json({ state: "datebook_disabled" });
    const days = Array.from({ length: 7 }, (_, i) => isoDay(i));
    const zoned = (id: string, day: number, h: number, m: number, mins: number, title: string, calendar = "Home") => ({
      entity: "event", id, day: isoDay(day), title, location: "", notes: "", calendar_title: calendar, source_title: "iCloud", display_text: title, authorization: "full_access", read_only: false,
      time: { kind: "zoned", start_at: isoAt(day, h, m), end_at: new Date(new Date(isoAt(day, h, m)).getTime() + mins * 60000).toISOString(), tz_id: "Asia/Tokyo" },
    });
    const items = isEmpty || mode === "stale" ? [] : [
      zoned("e1", 0, 9, 30, 30, "Stand-up", "Work"),
      zoned("e2", 0, 18, 0, 60, "Dinner with Aiko"),
      { entity: "reminder", id: "r1", day: isoDay(1), title: "Renew passport", location: "", notes: "", list_title: "Errands", due: { kind: "all_day", date: isoDay(1) } },
      zoned("e3", 1, 7, 0, 45, "Run club"),
      { entity: "event", id: "e4", day: isoDay(3), title: "Kyoto trip", location: "", notes: "", calendar_title: "Home", source_title: "iCloud", display_text: "Kyoto trip", authorization: "full_access", read_only: false, time: { kind: "all_day", start_date: isoDay(3), end_date_exclusive: isoDay(5) } },
      zoned("e5", 6, 10, 0, 60, "Dentist"),
    ];
    return json({
      state: "ok",
      server_now: new Date().toISOString(),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      requested: { start_day: days[0], end_day_exclusive: isoDay(7), start_at: isoAt(0, 0), end_at: isoAt(7, 0) },
      covered: mode === "stale" ? null : { start_at: isoAt(0, 0), end_at: isoAt(isEmpty ? 7 : 5, 0) },
      covered_days: mode === "stale" ? [] : days.slice(0, isEmpty ? 7 : 5),
      freshness: {
        events_last_complete_sync_at: mode === "stale" ? new Date(Date.now() - 3 * 86400000).toISOString() : new Date(Date.now() - 12 * 60000).toISOString(),
        reminders_last_complete_sync_at: null,
        events_authorization: "full_access",
        gateway_status: "active",
      },
      includes: { events: true, reminders: true },
      items,
      truncated: false,
    });
  }
  if (p === "/api/v1/friends/home/") {
    missionAsks[0].target_date = isoDay(2);
    wavesIn = wavesIn.map((w) => ({ ...w, created_at: w.created_at || isoAt(-1, 18) }));
    return json({
      profile: { handle: "yuki", display_name: "Yuki", avatar_hue: 260 },
      neighbors: isEmpty ? [] : neighborsNow(),
      reach_total: isEmpty ? null : fixtureMode() === "two" ? "10+" : fixtureMode() === "many" ? "100+" : "25+",
      pending_in: isEmpty ? [] : wavesIn,
      pending_out: isEmpty ? [] : [{ friendship_id: "w-2", direction: "outgoing", display_name: "Mei", handle: "mei", avatar_hue: 330, note: "", created_at: isoAt(-3, 9) }],
      moments: [],
      cursor: null,
    });
  }
  if (p === "/api/v1/friends/") {
    const legacy = (x: (typeof people)[number]) => ({ friendship_id: x.friendship_id, display_name: x.display_name, handle: x.handle, avatar_hue: x.avatar_hue, status: "accepted", since: x.friends_since });
    return json({ profile: null, neighbors: isEmpty ? [] : people.map(legacy), pending_incoming: isEmpty ? [] : wavesIn, pending_outgoing: [] });
  }
  const skyEdge = p.match(/^\/api\/v1\/friends\/([^/]+)\/sky\/$/);
  if (skyEdge) {
    const inSky = method === "POST";
    if (inSky && people.filter((x) => x.in_my_sky).length >= 12) throw httpError(409, { error: "sky_full", cap: 12 });
    const crowded = crowdFor(fixtureMode());
    if (crowded) {
      if (inSky && crowded.list.filter((x) => x.in_my_sky).length >= 12) throw httpError(409, { error: "sky_full", cap: 12 });
      crowded.list = crowded.list.map((x) => (x.friendship_id === skyEdge[1] ? { ...x, in_my_sky: inSky } : x));
      return json({ friendship_id: skyEdge[1], in_my_sky: inSky });
    }
    people = people.map((x) => (x.friendship_id === skyEdge[1] ? { ...x, in_my_sky: inSky } : x));
    return json({ friendship_id: skyEdge[1], in_my_sky: inSky });
  }
  const waveAct = p.match(/^\/api\/v1\/friends\/waves\/([^/]+)\/(accept|decline)\/$/);
  if (waveAct) {
    const w = wavesIn.find((x) => x.friendship_id === waveAct[1]);
    wavesIn = wavesIn.filter((x) => x.friendship_id !== waveAct[1]);
    if (w && waveAct[2] === "accept") {
      people = [...people, { friendship_id: w.friendship_id, display_name: w.display_name, handle: w.handle, avatar_hue: w.avatar_hue, bio: "", spark_count: 0, in_my_sky: false, bond: "light", friends_since: isoDay(0), has_unread_thread: false, thread_id: null, reach: null }];
    }
    return json({ friendship_id: waveAct[1], status: waveAct[2] === "accept" ? "accepted" : "declined" });
  }
  if (p === "/api/v1/friends/missions/" && method === "GET") {
    const v1 = fixtureMode() === "v1";
    const planned = (id: string) => ["m-pm", "m-garden", "m-3"].includes(id) || /^m-(fx|cleanup)/.test(id);
    const live = missionAsks
      // The garden only exists as a plan; a planned project you left or deleted is gone.
      .filter((m) => (v1 ? m.mission_id !== "m-garden" : !planned(m.mission_id) || hasProjectFixture(m.mission_id)))
      .map((m) => (v1 ? m : { ...m, title: projectFixtureTitle(m.mission_id) ?? m.title }));
    const rows = isEmpty ? [] : fixtureMode() === "two" ? live.filter((m) => m.mission_id === "m-pm" || /^m-(fx|cleanup)/.test(m.mission_id)) : live;
    return json(url.searchParams.get("include_invited") ? rows : rows.filter((m) => m.my_status === "active"));
  }
  const joinM = p.match(/^\/api\/v1\/friends\/missions\/([^/]+)\/join\/$/);
  if (joinM) {
    const m = missionAsks.find((x) => x.mission_id === joinM[1]);
    if (m) m.my_status = "active";
    return json({ mission_id: joinM[1], status: "active" });
  }
  const missionWrite = p.match(/^\/api\/v1\/friends\/missions\/([^/]+)\/(updates|tasks|decline|leave)\/$/);
  if (missionWrite && method === "POST") {
    const [, id, action] = missionWrite;
    const b = bodyOf(init);
    if (action === "updates") {
      const row = { id: `u-new-${Date.now()}`, kind: String(b.kind ?? "note"), text: String(b.text ?? ""), created_at: new Date().toISOString(), author_name: "Yuki" };
      postedUpdates.set(id, [row, ...(postedUpdates.get(id) ?? [])]);
      return json({ id: row.id, kind: row.kind });
    }
    if (action === "tasks") return json({ task_id: `task-${Date.now()}`, title: String(b.title ?? "") });
    const at = missionAsks.findIndex((m) => m.mission_id === id);
    if (at >= 0) missionAsks.splice(at, 1);
    return json({ mission_id: id, status: action === "decline" ? "declined" : "left" });
  }
  if (p === "/api/v1/friends/missions/" && method === "POST") {
    const b = bodyOf(init);
    const id = `m-new-${missionAsks.length + 1}`;
    missionAsks.push({ mission_id: id, title: String(b.title ?? "New project"), status: "active", target: (b.target as Record<string, string>) ?? {}, target_date: null, version: 1, my_commitment: "", my_status: "active", my_role: "owner" });
    return json({ mission_id: id });
  }
  if (p === "/api/v1/friends/circles/" && method === "GET") {
    return json(isEmpty ? [] : clusterList().map(({ circle_id, name, hue, member_count, my_role, invite_code }) => ({ circle_id, name, hue, member_count, my_role, invite_code })));
  }
  if (p === "/api/v1/friends/circles/join/" && method === "POST") {
    const code = String(bodyOf(init).invite_code ?? "");
    if (!code.startsWith("NBHD-")) throw httpError(404, { detail: "That code doesn't match a cluster." });
    return json({ circle_id: "c-b3", status: "active" });
  }
  if (p === "/api/v1/friends/circles/" && method === "POST") return json({ circle_id: "c-b3" });
  const circleM = p.match(/^\/api\/v1\/friends\/circles\/([^/]+)\/$/);
  if (circleM && method === "GET") {
    const d = clusterDetail(circleM[1]);
    if (!d) throw httpError(404, { detail: "Not found." });
    return json(d);
  }
  const missionM = p.match(/^\/api\/v1\/friends\/missions\/([^/]+)\/$/);
  if (missionM && method === "GET") {
    const d = missionDetail(missionM[1]);
    if (!d) throw httpError(404, { detail: "Not found." });
    return json(d);
  }
  if (p === "/api/v1/friends/invites/" && method === "POST") {
    return json({ token: "fx-invite-7Qk2", url: `${typeof window !== "undefined" ? window.location.origin : "https://hoodunited.org"}/friends/invite/fx-invite-7Qk2`, expires_at: isoAt(14, 12), max_uses: 5, uses: 0 });
  }
  if (p === "/api/v1/friends/threads/") {
    if (method === "POST") return json({ thread_id: `t-${String(bodyOf(init).friendship_id ?? "x")}`, friendship_id: bodyOf(init).friendship_id });
    return json(isEmpty ? [] : [...threadList(), ...clusterThreadRows(clusterList())]);
  }
  if (/^\/api\/v1\/friends\/threads\/[^/]+\/messages\/$/.test(p)) {
    if (method === "POST") return json({ public_id: `m-${Date.now()}`, seq: 9, text: String(bodyOf(init).text ?? ""), mine: true, created_at: new Date().toISOString() });
    const first = neighborsNow()[0];
    return json({
      messages: p.includes(first ? `t-${first.friendship_id}` : "none")
        ? [
            { public_id: "c1", seq: 1, text: "Are you going to the park on Sunday?", mine: true, created_at: isoAt(-3, 17) },
            { public_id: "c2", seq: 2, text: "Yes! Bringing the kids around 10.", mine: false, created_at: isoAt(-2, 17) },
            { public_id: "c3", seq: 3, text: "See you at the park on Sunday.", mine: false, created_at: isoAt(-2, 18) },
          ]
        : [],
      next_cursor: null,
    });
  }
  if (/^\/api\/v1\/friends\/threads\/[^/]+\/read\/$/.test(p)) return json({ ok: true });
  if (p === "/api/v1/friends/shares/pending/") {
    const first = neighborsNow()[0];
    return json(isEmpty || !first ? [] : [
      { id: "ps-1", lesson_id: 11, lesson_preview: "When fasting, plan for the hunger window in advance — going in without prep leads to snacking.", proposed_by: "agent", friendship_id: first.friendship_id, audience: first.display_name, created_at: isoAt(-1, 8) },
      { id: "ps-2", lesson_id: 12, lesson_preview: "Even on a full family day, carving out an hour for project work keeps momentum alive.", proposed_by: "agent", friendship_id: first.friendship_id, audience: first.display_name, created_at: isoAt(-2, 8) },
    ]);
  }
  if (p === "/api/v1/friends/absorbed/") return json(isEmpty ? [] : absorbedList());
  if (p === "/api/v1/friends/mission-actions/") {
    return json(isEmpty ? [] : [{ id: "ga-1", mission_id: "m-3", mission_title: "Morning walks", suggested: { title: "Look up the river loop distance", description: "", due_date: null }, created_at: isoAt(-1, 12) }]);
  }
  if (p === "/api/v1/lessons/" && url.searchParams.get("status") === "approved") return json([]);
  if (p === "/api/v1/friends/profile/") return json({ handle: "yuki", display_name: "Yuki", bio: "", avatar_hue: 260, discoverable: true });
  if (p === "/api/v1/lessons/pending/") return json(isEmpty ? [] : skyPending);
  if (p === "/api/v1/core/sessions/") {
    return json(isEmpty ? [] : [{
      id: "med-1", date: isoDay(0), status: "ready", completed_at: null,
      lesson: { summary: "Begin again, gently." }, phase_arc: null,
      title: "Begin again, gently", theme: "rest", voice: "calm", model: "", guidance_text: "",
      audio_url: "", ogg_url: "", duration_ms: 660000, ambient_bed: "lakeshore",
      error: "", user_feedback: "", feedback_note: "", feedback_at: null,
      created_at: isoAt(0, 6), updated_at: isoAt(0, 6),
    }]);
  }
  if (p === "/api/v1/core/profile/") {
    return json({
      id: "cp-1", onboarding_status: "completed", preferred_voice: "calm", preferred_duration_minutes: 11,
      ambient_bed_enabled: true, daily_cron_enabled: true, preferred_time: "07:00", additional_context: "",
      created_at: "2025-06-01T00:00:00Z", updated_at: isoAt(-3, 9),
    });
  }
  if (p === "/api/v1/tenants/personas/") {
    return json([
      { key: "neighbor", label: "Neighbor", description: "Warm and practical", emoji: "" },
      { key: "coach", label: "Coach", description: "Direct and encouraging", emoji: "" },
    ]);
  }
  if (p === "/api/v1/tenants/preferences/") return json({ agent_persona: "neighbor" });
  if (p === "/api/v1/tenants/refresh-config/") {
    return json({ can_refresh: true, last_refreshed: isoAt(-2, 10), cooldown_seconds: 0, status: "ok", has_pending_update: false, container_image_tag: "v1", latest_image_tag: "v1", image_outdated: false });
  }
  if (p === "/api/v1/fuel/goals/") return json([]);
  return undefined;
}
