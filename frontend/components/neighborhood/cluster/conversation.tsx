"use client";

import clsx from "clsx";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { capsuleCls, sectionLabelCls } from "@/components/neighborhood/project/parts";
import { ErrorLine, fieldCls, ghostBtnCls, PanelHeader, quietBtnCls, SidePanel } from "@/components/neighborhood/ui";
import { AID_INTENTS, messageAuthor, messagesByDay, REPORT_REASONS, withReply, withStarter } from "@/lib/cluster";
import { getErrorMessage } from "@/lib/errors";
import { useMarkThreadReadMutation, useReportMessageMutation, useSendMessageMutation, useThreadMessagesQuery } from "@/lib/queries";
import type { ChatMessage } from "@/lib/types";

function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => typeof document === "undefined" || document.visibilityState === "visible");
  useEffect(() => {
    const handler = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  }, []);
  return visible;
}

const clock = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
};

/** Reply / Report on someone else's message. */
function MessageMenu({ author, onReply, onReport }: { author: string; onReply: () => void; onReport: () => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  const item = (label: string, action: () => void, tone = "text-os-ink") => (
    <button
      type="button"
      role="menuitem"
      onClick={() => {
        setOpen(false);
        action();
      }}
      className={clsx("os-focus flex min-h-[44px] w-full items-center rounded-xl px-3 text-left text-[0.875rem] hover:bg-os-accent-soft", tone)}
    >
      {label}
    </button>
  );
  return (
    <div className="relative shrink-0" ref={ref}>
      <button type="button" onClick={() => setOpen((o) => !o)} aria-label={`More for the message from ${author}`} aria-haspopup="menu" aria-expanded={open} className="os-focus -my-2 flex h-10 w-10 items-center justify-center rounded-full text-os-faint hover:text-os-ink">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="5" cy="12" r="1.5" />
          <circle cx="12" cy="12" r="1.5" />
          <circle cx="19" cy="12" r="1.5" />
        </svg>
      </button>
      {open ? (
        <div role="menu" className="absolute right-0 top-full z-20 mt-1 w-48 rounded-2xl border border-os-hairline bg-os-surface-solid p-1.5">
          {item("Reply with context", onReply)}
          {item("Report", onReport, "text-os-danger")}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The cluster's shared conversation: ask clearly, offer freely, share what
 * you've learned. Everyone's name is on what they wrote. Polls ~4s while the
 * tab is visible; marks the thread read once per open.
 */
export function ClusterConversation({ threadId, clusterName }: { threadId: string; clusterName: string }) {
  const visible = useDocumentVisible();
  const { data, isLoading, isError, refetch } = useThreadMessagesQuery(threadId, { active: visible });
  const send = useSendMessageMutation(threadId);
  const markRead = useMarkThreadReadMutation();
  const [draft, setDraft] = useState("");
  const [sendError, setSendError] = useState("");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [reporting, setReporting] = useState<ChatMessage | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const marked = useRef(false);
  const messages = (data?.messages ?? []).filter((m) => !hidden.has(m.public_id));
  const runs = messagesByDay(messages);

  useEffect(() => {
    if (marked.current) return;
    marked.current = true;
    markRead.mutate(threadId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const compose = (next: string) => {
    setDraft(next);
    inputRef.current?.focus();
  };
  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    const text = draft.trim();
    if (!text || send.isPending) return;
    setSendError("");
    setDraft("");
    try {
      await send.mutateAsync({ text, clientMsgId: crypto.randomUUID() });
    } catch (err) {
      setSendError(getErrorMessage(err));
      setDraft((current) => current || text);
    }
  };

  return (
    <section aria-labelledby="cluster-talk-heading" className="flex flex-col">
      <h2 id="cluster-talk-heading" className={sectionLabelCls}>
        Gather here
      </h2>
      <p className="pt-1.5 text-[0.875rem] leading-relaxed text-os-muted">A shared space for {clusterName}. Ask clearly, offer freely, and let people choose what they can take on.</p>

      <div ref={scrollRef} className="mt-3 flex max-h-[min(58dvh,560px)] min-h-[220px] flex-col gap-5 overflow-y-auto border-y border-os-hairline py-4 pr-1 [scrollbar-width:thin]" aria-live="polite">
        {isLoading && messages.length === 0 ? (
          <p className="m-auto text-[0.9375rem] text-os-muted">Loading&hellip;</p>
        ) : isError && messages.length === 0 ? (
          <div className="m-auto flex flex-col items-center gap-2">
            <p className="text-[0.9375rem] text-os-muted">Couldn&rsquo;t load the conversation.</p>
            <button type="button" className={quietBtnCls} onClick={() => void refetch()}>
              Try again
            </button>
          </div>
        ) : messages.length === 0 ? (
          <p className="os-serif m-auto max-w-[22rem] text-center text-[1.5rem] leading-snug text-os-ink">What would make this week a little easier?</p>
        ) : (
          runs.map((run) => (
            <div key={`${run.day}-${run.messages[0].public_id}`} className="flex flex-col gap-4">
              <p className="text-center text-[0.6875rem] uppercase tracking-[0.14em] text-os-faint">{run.day}</p>
              {run.messages.map((m) => {
                const author = messageAuthor(m);
                const pending = m.public_id.startsWith("pending-");
                return (
                  <article key={m.public_id} className="flex flex-col gap-1">
                    <header className="flex items-center justify-between gap-3">
                      <span className="flex min-w-0 items-baseline gap-2">
                        <span className="truncate text-[0.8125rem] font-semibold" style={{ color: m.mine ? "var(--os-accent)" : `hsl(${m.author?.avatar_hue ?? 210} 78% 86%)` }}>
                          {author}
                        </span>
                        <span className="shrink-0 text-[0.6875rem] text-os-faint">{pending ? "Sending…" : clock(m.created_at)}</span>
                      </span>
                      {!m.mine ? <MessageMenu author={author} onReply={() => compose(withReply(draft, author, m.text))} onReport={() => setReporting(m)} /> : null}
                    </header>
                    <p className="max-w-[62ch] whitespace-pre-wrap break-words text-[0.9375rem] leading-relaxed text-os-ink">{m.text}</p>
                  </article>
                );
              })}
            </div>
          ))
        )}
      </div>

      <div className="flex flex-wrap gap-2 pt-3" aria-label="Start a message">
        {AID_INTENTS.map((intent) => (
          <button key={intent.kind} type="button" title={intent.subtitle} onClick={() => compose(withStarter(draft, intent))} className={clsx(capsuleCls, "min-h-[36px] px-3.5")}>
            {intent.title}
          </button>
        ))}
      </div>
      {sendError ? (
        <div className="pt-2">
          <ErrorLine>{sendError}</ErrorLine>
        </div>
      ) : null}
      <form onSubmit={submit} className="flex items-end gap-3 pt-2">
        <label htmlFor="cluster-draft" className="sr-only">
          Share with this cluster
        </label>
        <textarea
          id="cluster-draft"
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
          rows={Math.min(6, Math.max(1, draft.split("\n").length))}
          placeholder="Share with this cluster…"
          className="min-h-[44px] flex-1 resize-none border-0 border-b border-os-ring bg-transparent py-2.5 text-[0.9375rem] leading-relaxed text-os-ink outline-none placeholder:text-os-faint focus:border-os-accent focus-visible:shadow-none focus-visible:outline-none"
        />
        <button type="submit" disabled={!draft.trim() || send.isPending} className={clsx(ghostBtnCls, "min-h-[44px] px-5 text-[0.875rem]")}>
          Send
        </button>
      </form>

      {reporting ? (
        <ReportPanel
          message={reporting}
          onClose={() => setReporting(null)}
          onReported={(id) => setHidden((s) => new Set(s).add(id))}
        />
      ) : null}
    </section>
  );
}

/** Report a message: what's wrong, anything to add. It's hidden for you straight away. */
function ReportPanel({ message, onClose, onReported }: { message: ChatMessage; onClose: () => void; onReported: (publicId: string) => void }) {
  const report = useReportMessageMutation();
  const [reason, setReason] = useState<(typeof REPORT_REASONS)[number]>(REPORT_REASONS[0]);
  const [detail, setDetail] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await report.mutateAsync({ publicId: message.public_id, reason, detail: detail.trim() });
      onReported(message.public_id);
      setDone(true);
    } catch (err) {
      setError(getErrorMessage(err));
    }
  };
  return (
    <SidePanel label="Report this message" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow={done ? "Reported" : "Report this"} title={done ? "Thank you" : "What’s wrong?"} />
        {done ? (
          <>
            <p className="text-[0.9375rem] leading-relaxed text-os-muted">We&rsquo;ve hidden this message for you and recorded your report for review.</p>
            <button type="button" className={`${ghostBtnCls} self-start`} onClick={onClose} data-autofocus>
              Done
            </button>
          </>
        ) : (
          <>
            <blockquote className="border-l border-os-ring pl-3.5 text-[0.9375rem] leading-relaxed text-os-muted">
              <span className="line-clamp-3">&ldquo;{message.text}&rdquo;</span>
              <footer className="pt-1 text-[0.75rem] text-os-faint">{messageAuthor(message)}</footer>
            </blockquote>
            <div role="radiogroup" aria-label="What’s wrong?" className="flex flex-col border-b border-os-hairline">
              {REPORT_REASONS.map((r) => (
                <button key={r} type="button" role="radio" aria-checked={reason === r} onClick={() => setReason(r)} className="os-focus flex min-h-[48px] w-full items-center justify-between gap-3 border-t border-os-hairline px-1 text-left text-[0.9375rem] text-os-ink">
                  {r}
                  <span aria-hidden="true" className={clsx("flex h-[18px] w-[18px] items-center justify-center rounded-full border", reason === r ? "border-os-accent" : "border-os-ring")}>
                    {reason === r ? <span className="h-2 w-2 rounded-full bg-os-accent" /> : null}
                  </span>
                </button>
              ))}
            </div>
            <label className="flex flex-col gap-1">
              <span className={sectionLabelCls}>Anything to add? (optional)</span>
              <textarea value={detail} onChange={(e) => setDetail(e.target.value)} rows={3} maxLength={200} placeholder="A little more context…" className={`${fieldCls} resize-none py-2.5`} />
            </label>
            {error ? <ErrorLine>{error}</ErrorLine> : null}
            <div className="mt-auto flex items-center gap-4 pt-2">
              <button type="submit" className={ghostBtnCls} disabled={report.isPending}>
                {report.isPending ? "Sending…" : "Report"}
              </button>
              <button type="button" className={quietBtnCls} disabled={report.isPending} onClick={onClose}>
                Cancel
              </button>
            </div>
          </>
        )}
      </form>
    </SidePanel>
  );
}
