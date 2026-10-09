/**
 * DEV-ONLY: in-memory cluster (circle) conversations for the fixture API (see
 * lib/dev-fixtures.ts — only ever loaded behind NODE_ENV=development +
 * NEXT_PUBLIC_WEB_FIXTURES=1). Mirrors the iPhone's MutualAidFixtureClient: an
 * ask, an offer and something learned, with authors; sending, reporting,
 * conversation choices, removing a member and a new invite code all apply in
 * memory and reset on reload.
 */

type Json = unknown;
type Body = Record<string, unknown>;

interface Author {
  handle: string | null;
  display_name: string;
  avatar_hue: number;
}
interface Message {
  public_id: string;
  seq: number;
  text: string;
  mine: boolean;
  created_at: string;
  author: Author;
}

const threads = new Map<string, Message[]>();
const choices = new Map<string, { muted: boolean; agent_absorb_enabled: boolean }>();
const hidden = new Set<string>();
const removed = new Map<string, Set<string>>();
const codes = new Map<string, string>();
const left = new Set<string>();

function at(dayOffset: number, hour: number, minute: number): string {
  const d = new Date();
  d.setDate(d.getDate() + dayOffset);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}

function httpError(status: number, body: Json): Error {
  const err = new Error(JSON.stringify(body));
  (err as Error & { status: number }).status = status;
  return err;
}

function seed(threadId: string, people: Author[]): Message[] {
  const [a, b, c] = [people[0], people[1] ?? people[0], people[2] ?? people[0]];
  const me: Author = { handle: "yuki", display_name: "Yuki", avatar_hue: 260 };
  const say = (seq: number, who: Author, mine: boolean, day: number, h: number, m: number, text: string): Message => ({ public_id: `${threadId}-${seq}`, seq, text, mine, created_at: at(day, h, m), author: who });
  return [
    say(1, a, false, -3, 9, 0, "I could use a hand with meals this week. Something simple I can reheat would help most."),
    say(2, b, false, -3, 9, 2, "I’m cooking Sunday and can make a little extra. Let’s find something you’d enjoy."),
    say(3, me, true, -3, 9, 20, "I can drop it round on my way back from the market — just say when."),
    say(4, a, false, -2, 18, 4, "Something I’ve learned: asking for a specific small thing makes it easier to say yes. Thank you for making room."),
    say(5, c, false, -1, 7, 45, "I can offer the school run on Thursday and Friday. I have room for two more."),
  ];
}

/** Thread rows for the clusters you're in, so conversation choices have something to read. */
export function clusterThreadRows(clusters: { circle_id: string; name: string; hue: number }[]): Json[] {
  return clusters
    .filter((c) => !left.has(c.circle_id))
    .map((c) => {
      const id = `t-${c.circle_id}`;
      const pref = choices.get(id) ?? { muted: false, agent_absorb_enabled: false };
      const last = threads.get(id)?.filter((m) => !hidden.has(m.public_id)).slice(-1)[0];
      return { thread_id: id, friendship_id: null, display_name: "Neighbor", handle: null, avatar_hue: c.hue, unread: 0, last_message: last?.text.slice(0, 80) ?? "", last_message_at: last?.created_at ?? null, ...pref };
    });
}

export function clusterLeft(circleId: string): boolean {
  return left.has(circleId);
}

export function clusterRemoved(circleId: string): Set<string> {
  return removed.get(circleId) ?? new Set();
}

export function clusterInviteCode(circleId: string, fallback: string | null): string | null {
  return fallback === null ? null : (codes.get(circleId) ?? fallback);
}

/**
 * Returns a response for a cluster-conversation path, or `undefined` to fall
 * through. `people(threadId)` supplies who else is in that cluster.
 */
export function clusterFixture(p: string, method: string, body: Body, people: (threadId: string) => Author[] | undefined): Json | undefined {
  const messages = p.match(/^\/api\/v1\/friends\/threads\/(t-c-[^/]+)\/messages\/$/);
  if (messages) {
    const id = messages[1];
    const others = people(id);
    if (!others) return undefined;
    if (!threads.has(id)) threads.set(id, others.length ? seed(id, others) : []);
    const list = threads.get(id) as Message[];
    if (method === "POST") {
      const clientId = String(body.client_msg_id ?? `m-${list.length + 1}`);
      const existing = list.find((m) => m.public_id === clientId);
      if (existing) return { public_id: existing.public_id, seq: existing.seq, created: false };
      const created: Message = { public_id: clientId, seq: list.length + 1, text: String(body.text ?? ""), mine: true, created_at: new Date().toISOString(), author: { handle: "yuki", display_name: "Yuki", avatar_hue: 260 } };
      list.push(created);
      return { public_id: created.public_id, seq: created.seq, created: true };
    }
    return { messages: list.filter((m) => !hidden.has(m.public_id)), next_cursor: null };
  }
  const membership = p.match(/^\/api\/v1\/friends\/threads\/(t-c-[^/]+)\/membership\/$/);
  if (membership && method === "PATCH") {
    const current = choices.get(membership[1]) ?? { muted: false, agent_absorb_enabled: false };
    const next = {
      muted: typeof body.muted === "boolean" ? body.muted : current.muted,
      agent_absorb_enabled: typeof body.agent_absorb_enabled === "boolean" ? body.agent_absorb_enabled : current.agent_absorb_enabled,
    };
    choices.set(membership[1], next);
    return { thread_id: membership[1], ...next };
  }
  if (p === "/api/v1/friends/report/" && method === "POST") {
    if (body.target_kind === "friend_message") hidden.add(String(body.target_id ?? ""));
    return { report_id: `r-${hidden.size}`, hidden: body.target_kind !== "general" };
  }
  if (p === "/api/v1/friends/waves/" && method === "POST") {
    if (!String(body.handle ?? "").trim()) throw httpError(400, { detail: "A handle is required." });
    return { friendship_id: `w-${String(body.handle)}`, status: "pending" };
  }
  const circle = p.match(/^\/api\/v1\/friends\/circles\/([^/]+)\/(leave|remove|invite-code|members)\/$/);
  if (circle && method === "POST") {
    const [, id, action] = circle;
    if (action === "leave") {
      left.add(id);
      return { circle_id: id, status: "left", purged: !body.keep };
    }
    if (action === "remove") {
      const handle = String(body.handle ?? "");
      removed.set(id, new Set([...(removed.get(id) ?? []), handle]));
      return { circle_id: id, removed: handle };
    }
    if (action === "invite-code") {
      const code = `NBHD-${id.slice(2).toUpperCase()}${Math.floor(10 + Math.random() * 89)}`;
      codes.set(id, code);
      return { circle_id: id, invite_code: code };
    }
    return { circle_id: id, added: String(body.handle ?? "") };
  }
  return undefined;
}
