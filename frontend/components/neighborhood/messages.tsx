"use client";

import clsx from "clsx";
import { useEffect, useRef, useState } from "react";

import { CloseButton, ErrorLine, Initial, SectionHead, SidePanel, textBtnCls } from "@/components/neighborhood/ui";
import { getErrorMessage } from "@/lib/errors";
import { messageTime, type MessageRow } from "@/lib/neighborhood";
import { useMarkThreadReadMutation, usePatchMembershipMutation, useSendMessageMutation, useThreadMessagesQuery, useThreadsQuery } from "@/lib/queries";
import type { ChatThread } from "@/lib/types";

const FIRST_ROWS = 8;

/** One row per person: the last message and when, or "Say hello" if you haven't yet. */
export function MessagesSection({ rows, loading, onOpen }: { rows: MessageRow[]; loading: boolean; onOpen: (row: MessageRow) => void }) {
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.slice(0, FIRST_ROWS);
  return (
    <section aria-labelledby="messages-heading">
      <SectionHead id="messages-heading" label="Messages" />
      {loading ? (
        <p className="os-hairline-top py-3.5 text-[0.9375rem] text-os-muted">Loading&hellip;</p>
      ) : rows.length === 0 ? (
        <p className="os-hairline-top py-3.5 text-[0.9375rem] text-os-muted">When you have neighbors, your conversations with them live here.</p>
      ) : (
        <ul>
          {shown.map((r) => (
            <li key={r.key}>
              <button type="button" onClick={() => onOpen(r)} className="os-focus os-hairline-top group flex min-h-[64px] w-full items-center gap-3.5 py-3 text-left">
                <Initial name={r.name} hue={r.hue} />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-[0.9375rem] text-white">{r.name}</span>
                  {r.last ? (
                    <span className={clsx("truncate text-[0.8125rem]", r.unread ? "text-os-ink" : "text-os-muted")}>{r.last}</span>
                  ) : (
                    <span className="text-[0.8125rem] text-os-accent group-hover:underline">Say hello</span>
                  )}
                </span>
                <span className="flex shrink-0 flex-col items-end gap-1">
                  {r.at ? <span className="text-[0.75rem] text-os-faint">{messageTime(r.at)}</span> : null}
                  {r.unread ? (
                    <span className="flex h-5 min-w-[20px] items-center justify-center rounded-full border border-os-accent-line px-1.5 text-[0.6875rem] text-os-accent" aria-label={`${r.unread} unread`}>
                      {r.unread}
                    </span>
                  ) : null}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {!all && rows.length > FIRST_ROWS ? (
        <button type="button" onClick={() => setAll(true)} className={`${textBtnCls} os-hairline-top w-full`}>
          Everyone else ({rows.length - FIRST_ROWS})
        </button>
      ) : null}
    </section>
  );
}

function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => typeof document === "undefined" || document.visibilityState === "visible");
  useEffect(() => {
    const handler = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  }, []);
  return visible;
}

/**
 * A conversation (1:1 or a cluster's), in a right-side panel. Polls ~4s while
 * open and the tab is visible; marks the thread read once per open.
 */
export function ThreadPanel({ thread, onClose }: { thread: ChatThread; onClose: () => void }) {
  const visible = useDocumentVisible();
  const { data: threads = [] } = useThreadsQuery();
  // A cluster thread comes back from the generic list as "Neighbor"; keep the
  // cluster's own name + hue from the thread we were opened with.
  const isCluster = thread.friendship_id === null;
  const live = threads.find((t) => t.thread_id === thread.thread_id) ?? thread;
  const name = isCluster ? thread.display_name : live.display_name;
  const hue = isCluster ? thread.avatar_hue : live.avatar_hue;
  const handle = isCluster ? null : live.handle;

  const { data, isLoading } = useThreadMessagesQuery(thread.thread_id, { active: visible });
  const send = useSendMessageMutation(thread.thread_id);
  const markRead = useMarkThreadReadMutation();
  const membership = usePatchMembershipMutation(thread.thread_id);
  const [draft, setDraft] = useState("");
  const [sendError, setSendError] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const marked = useRef(false);
  const messages = data?.messages ?? [];

  useEffect(() => {
    if (marked.current) return;
    marked.current = true;
    markRead.mutate(thread.thread_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [thread.thread_id]);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const submit = async () => {
    const text = draft.trim();
    if (!text || send.isPending) return;
    setSendError("");
    setDraft("");
    try {
      await send.mutateAsync({ text, clientMsgId: crypto.randomUUID() });
    } catch (err) {
      setSendError(getErrorMessage(err));
    }
  };

  return (
    <SidePanel label={`Conversation with ${name}`} onClose={onClose} bare>
      <div className="flex min-h-0 flex-1 flex-col">
        <header className="os-hairline-bottom flex items-center gap-3 px-5 py-4 sm:px-11 sm:pt-9">
          <Initial name={name} hue={hue} size={40} />
          <div className="min-w-0 flex-1">
            <h2 className="os-serif truncate text-[1.75rem] leading-tight text-white">{name}</h2>
            {handle ? <p className="truncate text-[0.75rem] text-os-faint">@{handle}</p> : isCluster ? <p className="text-[0.75rem] text-os-faint">Cluster chat</p> : null}
          </div>
          <button type="button" onClick={() => membership.mutate({ muted: !live.muted })} aria-pressed={live.muted} className={clsx("os-focus min-h-[40px] shrink-0 rounded px-1 text-[0.8125rem]", live.muted ? "text-os-accent" : "text-os-muted hover:text-os-ink")}>
            {live.muted ? "Muted" : "Mute"}
          </button>
          <CloseButton onClose={onClose} />
        </header>

        <div ref={scrollRef} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-5 py-5 sm:px-11" aria-live="polite">
          {isLoading && messages.length === 0 ? (
            <p className="pt-8 text-center text-[0.9375rem] text-os-muted">Loading&hellip;</p>
          ) : messages.length === 0 ? (
            <p className="m-auto max-w-[18rem] text-center text-[0.9375rem] leading-relaxed text-os-muted">Say hello &mdash; this is the start of your conversation{isCluster ? " with the cluster" : ""}.</p>
          ) : (
            messages.map((m) => (
              <div key={m.public_id} className={clsx("flex", m.mine ? "justify-end" : "justify-start")}>
                <p className={clsx("max-w-[82%] whitespace-pre-wrap break-words rounded-2xl px-4 py-2 text-[0.9375rem] leading-relaxed", m.mine ? "bg-os-accent-soft text-white" : "border border-os-hairline text-os-ink")}>{m.text}</p>
              </div>
            ))
          )}
        </div>

        {sendError ? (
          <div className="px-5 pb-2 sm:px-11">
            <ErrorLine>{sendError}</ErrorLine>
          </div>
        ) : null}
        <div className="os-hairline-top flex items-end gap-3 px-5 pb-[calc(env(safe-area-inset-bottom)+1rem)] pt-3 sm:px-11 sm:pb-8">
          <label htmlFor="thread-draft" className="sr-only">Message</label>
          <textarea
            id="thread-draft"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit();
              }
            }}
            rows={1}
            placeholder="Message…"
            data-autofocus
            className="max-h-32 min-h-[44px] flex-1 resize-none border-0 border-b border-os-ring bg-transparent py-2.5 text-[0.9375rem] text-os-ink outline-none placeholder:text-os-faint focus:border-os-accent focus-visible:shadow-none focus-visible:outline-none"
          />
          <button type="button" onClick={() => void submit()} disabled={!draft.trim() || send.isPending} className="os-focus inline-flex min-h-[44px] shrink-0 items-center rounded-full border border-os-accent-line px-5 text-[0.875rem] text-os-accent transition hover:bg-os-accent-soft disabled:opacity-40">
            Send
          </button>
        </div>
      </div>
    </SidePanel>
  );
}
