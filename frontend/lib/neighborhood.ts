/**
 * Pure helpers behind the Open Sky Neighborhood page: the one "Needs you"
 * list, the Messages rows, the grouping on "What your assistant keeps", and
 * the small bits of copy that several sections share. No DOM, no fetching.
 */

import type {
  AbsorbedItem,
  ChatThread,
  HomeNeighbor,
  HomeWave,
  MissionAsk,
  MissionDetail,
  PendingGoalAction,
  PendingShare,
} from "./types";

// ── Counts ────────────────────────────────────────────────────────────────

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "2 NEIGHBORS · 2 CLUSTERS · 1 PROJECT" (upper-cased by CSS). */
export function countsLine(neighbors: number, clusters: number, projects: number): string {
  return [plural(neighbors, "neighbor"), plural(clusters, "cluster"), plural(projects, "project")].join(" · ");
}

// ── Needs you ─────────────────────────────────────────────────────────────

export type NeedKind = "share" | "wave" | "ask" | "action";

export interface Need {
  key: string;
  kind: NeedKind;
  /** Short left-column tag, e.g. "Share to Kiho". */
  tag: string;
  /** The full sentence. */
  text: string;
  /** The one primary action's word. */
  action: string;
  /** A quiet secondary way out, when the existing flow has one. */
  dismiss?: string;
  /** Sort time (ms); newest first within a kind. */
  at: number;
  /** The id the action needs. */
  id: string;
}

// People-first: a person waiting on you, then what your assistant wants to
// send out, then asks to help, then task suggestions.
const KIND_ORDER: Record<NeedKind, number> = { wave: 0, share: 1, ask: 2, action: 3 };

function ms(iso: string | null | undefined): number {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(t) ? t : 0;
}

function quote(s: string, max = 180): string {
  const t = s.trim().replace(/\s+/g, " ");
  return `“${t.length > max ? `${t.slice(0, max - 1).trimEnd()}…` : t}”`;
}

export function buildNeeds(input: {
  shares?: PendingShare[];
  waves?: HomeWave[];
  asks?: MissionAsk[];
  actions?: PendingGoalAction[];
}): Need[] {
  const out: Need[] = [];
  for (const s of input.shares ?? []) {
    const to = s.audience || "a cluster";
    out.push({
      key: `share:${s.id}`,
      kind: "share",
      tag: `Share to ${to}`,
      text: `Your assistant wants to share ${quote(s.lesson_preview)} with ${to}.`,
      action: "Review",
      at: ms(s.created_at),
      id: s.id,
    });
  }
  for (const w of input.waves ?? []) {
    const note = w.note?.trim();
    out.push({
      key: `wave:${w.friendship_id}`,
      kind: "wave",
      tag: "Wave",
      text: note ? `${w.display_name} waved at you: ${quote(note, 140)}` : `${w.display_name} waved at you.`,
      action: "Wave back",
      dismiss: "Not now",
      at: ms(w.created_at),
      id: w.friendship_id,
    });
  }
  for (const a of input.asks ?? []) {
    if (a.my_status !== "invited" || a.status !== "active") continue;
    out.push({
      key: `ask:${a.mission_id}`,
      kind: "ask",
      tag: "Ask",
      text: `You’re asked to help with “${a.title}”${a.target_date ? `, by ${dayLabel(a.target_date)}` : ""}.`,
      action: "I can help",
      at: a.target_date ? ms(`${a.target_date}T12:00:00`) : 0,
      id: a.mission_id,
    });
  }
  for (const g of input.actions ?? []) {
    out.push({
      key: `action:${g.id}`,
      kind: "action",
      tag: "Project step",
      text: `Your assistant suggests adding “${g.suggested.title}” to your tasks, for ${g.mission_title}.`,
      action: "Add it",
      dismiss: "No thanks",
      at: ms(g.created_at),
      id: g.id,
    });
  }
  return out.sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || b.at - a.at || (a.key < b.key ? -1 : 1));
}

export function dayLabel(isoDay: string): string {
  const d = new Date(`${isoDay}T12:00:00`);
  return Number.isNaN(d.getTime()) ? isoDay : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

// ── Messages ──────────────────────────────────────────────────────────────

export interface MessageRow {
  key: string;
  name: string;
  hue: number;
  /** Last message, or "" when there isn't one yet ("Say hello"). */
  last: string;
  at: string | null;
  unread: number;
  thread: ChatThread | null;
  neighbor: HomeNeighbor | null;
}

/**
 * One row per person: conversations with a message first (newest first), then
 * people you haven't talked to yet — your sky first, then A–Z. Cluster chats
 * live on their cluster, not here.
 */
export function messageRows(threads: ChatThread[], neighbors: HomeNeighbor[]): MessageRow[] {
  const byFriend = new Map<string, ChatThread>();
  for (const t of threads) if (t.friendship_id) byFriend.set(t.friendship_id, t);
  const rows: MessageRow[] = [];
  const seen = new Set<string>();
  for (const t of threads) {
    if (!t.friendship_id || !t.last_message) continue;
    seen.add(t.friendship_id);
    const n = neighbors.find((x) => x.friendship_id === t.friendship_id) ?? null;
    rows.push({ key: t.thread_id, name: n?.display_name ?? t.display_name, hue: n?.avatar_hue ?? t.avatar_hue, last: t.last_message, at: t.last_message_at, unread: t.unread, thread: t, neighbor: n });
  }
  rows.sort((a, b) => ms(b.at) - ms(a.at));
  const quiet = neighbors
    .filter((n) => !seen.has(n.friendship_id))
    .sort((a, b) => Number(b.in_my_sky) - Number(a.in_my_sky) || a.display_name.localeCompare(b.display_name));
  for (const n of quiet) {
    rows.push({ key: `n:${n.friendship_id}`, name: n.display_name, hue: n.avatar_hue, last: "", at: null, unread: 0, thread: byFriend.get(n.friendship_id) ?? null, neighbor: n });
  }
  return rows;
}

/** "Tue", "10:24", "Sep 3" — a quiet relative stamp for a message row. */
export function messageTime(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const days = (now.getTime() - d.getTime()) / 86_400_000;
  if (days < 6 && days > 0) return d.toLocaleDateString("en-US", { weekday: "short" });
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) });
}

// ── What your assistant keeps ─────────────────────────────────────────────

export interface KeepItem {
  key: string;
  text: string;
  kindLabel: string;
  ids: string[];
  first: string;
  last: string;
}

export interface KeepGroup {
  key: string;
  name: string;
  handle: string | null;
  /** Your own shares, kept back. */
  mine: boolean;
  count: number;
  items: KeepItem[];
}

type Keepable = AbsorbedItem & { group_key?: string | null; kind_label?: string | null };

/**
 * Group what the assistant keeps by the person it came from, collapsing
 * duplicates (the API's `group_key` when present, else source kind + title).
 * Your own shares-back come last; items with no source handle go in "Other".
 */
export function groupKeeps(items: Keepable[], names: Map<string, string>, myHandle: string | null): KeepGroup[] {
  const groups = new Map<string, KeepGroup>();
  const sorted = [...items].sort((a, b) => ms(a.absorbed_at) - ms(b.absorbed_at));
  for (const it of sorted) {
    const handle = it.from_handle || null;
    const gk = handle ?? "\u0000other";
    let g = groups.get(gk);
    if (!g) {
      const name = !handle ? "Other" : handle === myHandle ? "You shared back" : names.get(handle) ?? `@${handle}`;
      g = { key: gk, name, handle, mine: !!handle && handle === myHandle, count: 0, items: [] };
      groups.set(gk, g);
    }
    const label = (it.label || "").trim() || "A shared note";
    const ik = it.group_key ? `g:${it.group_key}` : `s:${it.source_kind}:${label.toLowerCase()}`;
    let item = g.items.find((x) => x.key === ik);
    if (!item) {
      item = { key: ik, text: label, kindLabel: it.kind_label?.trim() || "", ids: [], first: it.absorbed_at, last: it.absorbed_at };
      g.items.push(item);
    }
    item.ids.push(it.id);
    item.last = it.absorbed_at;
    g.count++;
  }
  const out = [...groups.values()];
  for (const g of out) g.items.sort((a, b) => ms(b.last) - ms(a.last));
  const rank = (g: KeepGroup) => (g.handle === null ? 2 : g.handle === myHandle ? 1 : 0);
  return out.sort((a, b) => rank(a) - rank(b) || b.count - a.count || a.name.localeCompare(b.name));
}

export function keepMeta(item: KeepItem): string {
  const f = shortDate(item.first), l = shortDate(item.last);
  const when = item.ids.length > 1 && f !== l ? `${f} – ${l}` : l;
  return [when, item.kindLabel, item.ids.length > 1 ? `kept ${item.ids.length} times` : ""].filter(Boolean).join(" · ");
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function keepsSummary(groups: KeepGroup[]): string {
  const total = groups.reduce((a, g) => a + g.count, 0);
  const people = groups.filter((g) => g.handle !== null && !g.mine).length;
  if (!total) return "";
  return people ? `${plural(total, "note")} from ${plural(people, "person", "people")}` : plural(total, "note");
}

// ── Projects ──────────────────────────────────────────────────────────────

/** Display name for a mission member's handle, "You" for the viewer. */
export function memberName(handle: string | null, myHandle: string | null, names: Map<string, string>): string {
  if (handle && handle === myHandle) return "You";
  if (!handle) return "A neighbor";
  return names.get(handle) ?? `@${handle}`;
}

/** "You + Kiho", "You + 3", "Kiho + Aiko". */
export function crewLabel(detail: Pick<MissionDetail, "members"> | undefined, myHandle: string | null, names: Map<string, string>): string {
  if (!detail || detail.members.length === 0) return "";
  const list = detail.members.map((m) => memberName(m.handle, myHandle, names));
  const me = list.indexOf("You");
  const others = list.filter((_, i) => i !== me);
  const head = me >= 0 ? "You" : others.shift() ?? "";
  if (others.length === 0) return head;
  return others.length === 1 ? `${head} + ${firstWord(others[0])}` : `${head} + ${others.length}`;
}

function firstWord(s: string): string {
  return s.split(/\s+/)[0] || s;
}

/** Window words for the member rows: "this week" or "last 4 weeks". */
export function windowLabel(windowDays: number): string {
  return windowDays > 7 ? "last 4 weeks" : "this week";
}

/** "0 of 7 days this week · next: talk about an AI workflow". */
export function projectLine(detail: MissionDetail | undefined, myHandle: string | null): string {
  if (!detail) return "";
  const me = detail.members.find((m) => m.handle && m.handle === myHandle) ?? detail.members[0];
  const parts: string[] = [];
  if (me) parts.push(`${me.showed_up} of ${me.window_days} days ${windowLabel(me.window_days)}`);
  const next = me?.next_step || detail.members.find((m) => m.next_step)?.next_step;
  if (next) parts.push(`next: ${next.charAt(0).toLowerCase()}${next.slice(1)}`);
  if (detail.status !== "active") parts.unshift(detail.status === "achieved" ? "Done" : detail.status.charAt(0).toUpperCase() + detail.status.slice(1));
  return parts.join(" · ");
}

/** Cluster join link carried in the invite QR / share sheet. */
export function clusterInviteUrl(origin: string, code: string): string {
  return `${origin.replace(/\/$/, "")}/friends?join=${encodeURIComponent(code)}`;
}
