/**
 * Pure helpers behind a cluster's own page (Circles in the API): the three
 * ways to start a message (ask · offer · learn), quoting a message you're
 * answering, who wrote what and on which day, and the small map of who shares
 * the cluster. Mirrors the iPhone's MutualAidModels. No DOM, no fetching.
 */

import type { ChatMessage, CircleMember, HomeNeighbor } from "./types";

export interface AidIntent {
  kind: "ask" | "offer" | "learn";
  title: string;
  subtitle: string;
  /** The lines a message starts from. */
  draft: string;
}

export const AID_INTENTS: AidIntent[] = [
  { kind: "ask", title: "Ask for support", subtitle: "A little help can change a whole week.", draft: "I could use a hand with…\nWhat would help most is…\nA time that works for me is…" },
  { kind: "offer", title: "Offer a hand", subtitle: "Time, a skill, or a listening ear.", draft: "I can offer…\nI have room for…\nIf that would help, let’s talk." },
  { kind: "learn", title: "Learn together", subtitle: "Trade experience. Try something together.", draft: "Something I’ve learned is…\nI’d love to hear your experience with…\nOne thing we could try together is…" },
];

export function aidIntent(kind: string | null | undefined): AidIntent | undefined {
  return AID_INTENTS.find((i) => i.kind === kind);
}

/** Start (or add to) a message from one of the three openers. */
export function withStarter(draft: string, intent: AidIntent): string {
  return draft.trim() ? `${draft.replace(/\s+$/, "")}\n\n${intent.draft}` : intent.draft;
}

/** Answer someone in a busy conversation: their name and a short quote, then your reply. */
export function withReply(draft: string, authorName: string, text: string): string {
  const quote = text.trim().replace(/\s+/g, " ").slice(0, 180);
  return `${draft.trim() ? `${draft.replace(/\s+$/, "")}\n\n` : ""}Replying to ${authorName || "a cluster member"}: “${quote}”\n\n`;
}

export function messageAuthor(message: Pick<ChatMessage, "mine" | "author">): string {
  return message.mine ? "You" : message.author?.display_name?.trim() || "Cluster member";
}

function localDay(d: Date): number {
  return Math.floor(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86_400_000);
}

/** "Today", "Yesterday", "Mon, Sep 28" — the day a run of messages was written. */
export function messageDay(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diff = localDay(now) - localDay(d);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) });
}

export interface MessageRun<M> {
  day: string;
  messages: M[];
}

/** Messages grouped under the day they were written, oldest first. */
export function messagesByDay<M extends Pick<ChatMessage, "created_at">>(messages: M[], now: Date = new Date()): MessageRun<M>[] {
  const runs: MessageRun<M>[] = [];
  for (const m of messages) {
    const day = messageDay(m.created_at, now);
    const last = runs[runs.length - 1];
    if (last && last.day === day) last.messages.push(m);
    else runs.push({ day, messages: [m] });
  }
  return runs;
}

export const REPORT_REASONS = ["Harassment", "Hate or abuse", "Spam", "Sexual content", "Other"] as const;

/** "Cluster · 7 people · you host" (upper-cased by CSS). */
export function clusterEyebrow(members: number, host: boolean): string {
  return ["Cluster", `${members} ${members === 1 ? "person" : "people"}`, ...(host ? ["you host"] : [])].join(" · ");
}

export function memberRole(member: Pick<CircleMember, "role">): string {
  return member.role === "admin" ? "Cluster host" : "Cluster member";
}

/** You first, then hosts, then everyone else A–Z. */
export function sortedMembers<M extends Pick<CircleMember, "is_me" | "role" | "display_name">>(members: M[]): M[] {
  const rank = (m: M) => (m.is_me ? 0 : m.role === "admin" ? 1 : 2);
  return [...members].sort((a, b) => rank(a) - rank(b) || a.display_name.localeCompare(b.display_name));
}

/**
 * How a cluster member relates to you. Sharing a cluster is not the same as
 * being neighbors: a direct connection begins when two people choose it.
 */
export function memberRelation(member: Pick<CircleMember, "is_me" | "handle">, neighbors: Pick<HomeNeighbor, "handle">[]): "me" | "neighbor" | "member" {
  if (member.is_me) return "me";
  const handle = member.handle?.toLowerCase();
  return handle && neighbors.some((n) => n.handle.toLowerCase() === handle) ? "neighbor" : "member";
}

/** Points on a ring around (cx, cy), the first at the top — the "who shares this" map. */
export function ringPoints(count: number, cx: number, cy: number, radius: number): { x: number; y: number }[] {
  return Array.from({ length: count }, (_, i) => {
    const angle = (i / Math.max(count, 1)) * Math.PI * 2 - Math.PI / 2;
    return { x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius };
  });
}

/** What leaving does, said before you do it. */
export const LEAVE_CLUSTER_NOTE = "You’ll lose its conversation and shares. Messages you already sent stay for the others.";
