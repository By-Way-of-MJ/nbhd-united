"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { MarkdownRenderer } from "@/components/markdown-renderer";
import { JournalBlockConflict } from "@/lib/api";
import { blockDraft, parseJournalBlocks, rebuildJournalBlock, type JournalBlock } from "@/lib/journal-blocks";
import { useCreateTaskMutation, useReplaceDocumentBlockMutation } from "@/lib/queries";
import type { DocumentResponse } from "@/lib/types";

export function DailyBlocks({ document, slug }: { document?: DocumentResponse | null; slug: string }) {
  const [active, setActive] = useState<JournalBlock | null>(null);
  const [editingMarkdown, setEditingMarkdown] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [draft, setDraft] = useState("");
  const [conflict, setConflict] = useState<DocumentResponse | null>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [taskAdded, setTaskAdded] = useState(false);
  const replace = useReplaceDocumentBlockMutation();
  const createTask = useCreateTaskMutation();
  const qc = useQueryClient();
  const blocks = parseJournalBlocks(active ? editingMarkdown : document?.markdown ?? "").filter((block) => block.kind !== "preamble");
  const busy = replace.isPending || createTask.isPending;
  const fresh = conflict && active ? parseJournalBlocks(conflict.markdown)[active.index] : undefined;

  function edit(block: JournalBlock) {
    setEditingMarkdown(document?.markdown ?? ""); setDeleting(false);
    setActive(block); setDraft(blockDraft(block)); setConflict(null); setError(""); setStatus(""); setTaskAdded(false);
  }
  function cancel() {
    if (conflict) qc.setQueryData(["document", "daily", slug], conflict);
    setActive(null); setConflict(null); setError("");
  }
  async function save(remove = false, retry = false) {
    if (!active) return;
    const base = retry ? fresh : active;
    if (!base) return;
    setError(""); setDeleting(remove);
    try {
      await replace.mutateAsync({ kind: "daily", slug, data: {
        index: base.index, original: base.source,
        replacement: remove ? "" : rebuildJournalBlock(base, draft),
      } });
      setActive(null); setConflict(null);
    } catch (err) {
      if (err instanceof JournalBlockConflict) setConflict(err.document);
      else setError("Couldn’t save. Your draft is still here. Please try again.");
    }
  }
  async function turnIntoTask() {
    const title = draft.trim().split(/\r?\n/)[0].slice(0, 200);
    if (!title) return;
    setError("");
    try {
      await createTask.mutateAsync({ title });
      setTaskAdded(true); setStatus("Added to your tasks in Horizons.");
    } catch { setError("Couldn’t add the task. Please try again."); }
  }

  return (
    <div className="os-journal-blocks px-1 py-5 sm:px-6 sm:py-7">
      <p className="mb-6 text-xs text-os-faint">Your day, in your words. Select any part to edit.</p>
      {blocks.length === 0 ? <p className="py-10 text-sm text-os-muted">A little space for today. Start with a thought below.</p> : null}
      {blocks.map((block) => active?.index === block.index ? (
        <section key={block.index} className="border-y border-os-accent-line py-5" aria-label="Edit journal block">
          <label htmlFor={`journal-block-${block.index}`} className="os-label mb-3 block text-os-accent">
            {active.kind === "entry" ? `${active.time} · YOUR ENTRY` : active.kind === "heading" ? "EDIT HEADING" : active.title}
          </label>
          <textarea id={`journal-block-${block.index}`} autoFocus value={draft} onChange={(e) => setDraft(e.target.value)}
            rows={active.kind === "heading" ? 2 : 5} disabled={busy}
            className="w-full resize-y rounded-lg border border-os-hairline bg-os-surface px-3 py-3 text-base leading-relaxed text-os-ink focus-visible:outline focus-visible:outline-1 focus-visible:outline-os-accent" />
          {conflict ? <div role="alert" className="mt-4 border-l border-os-accent pl-4 text-sm text-os-muted">
            <p className="text-os-accent">This part changed while you were editing</p>
            <p className="mt-2 text-xs text-os-faint">Fresh text</p>
            {fresh ? <div className="mt-1 whitespace-pre-wrap break-words">{fresh.kind !== "heading" ? <p className="mb-1 text-xs text-os-faint">{fresh.title}</p> : null}{blockDraft(fresh)}</div> : <p>This part was removed. Copy your draft before closing, then add it as a new entry.</p>}
            <p className="mt-2 text-xs text-os-faint">Your draft is kept above. Review the fresh text before trying again.{deleting ? " Trying again will delete this part." : ""}</p>
            {fresh ? <button type="button" className="os-btn mt-3" disabled={busy} onClick={() => void save(deleting, true)}>Try again</button> : null}
          </div> : null}
          {error ? <p role="alert" className="mt-3 text-sm text-os-danger">{error}</p> : null}
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1">
            <button type="button" className="os-btn-text" disabled={busy || !!conflict} onClick={() => void save(true)}>Delete</button>
            {active.kind === "entry" ? <button type="button" className="os-btn-text text-os-accent" disabled={busy || taskAdded || !draft.trim()} onClick={() => void turnIntoTask()}>Turn into a task</button> : null}
            <div className="ml-auto flex gap-3">
              <button type="button" className="os-btn-text" disabled={busy} onClick={cancel}>Cancel</button>
              <button type="button" className="os-btn" disabled={busy || !!conflict} onClick={() => void save()}>{replace.isPending ? "Saving…" : "Done"}</button>
            </div>
          </div>
          {status ? <p role="status" className="mt-3 text-sm text-os-done">{status}</p> : null}
        </section>
      ) : (
        <div key={block.index} data-block-kind={block.kind} role="button" tabIndex={active ? -1 : 0} aria-disabled={!!active}
          aria-label={`Edit ${block.kind === "entry" ? `${block.time} entry` : block.title}`}
          onClick={(event) => { if (!active && !(event.target as Element).closest("a")) edit(block); }}
          onKeyDown={(event) => { if (event.target === event.currentTarget && !active && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); edit(block); } }}
          className={`os-journal-block min-h-[44px] cursor-text py-5 focus-visible:outline focus-visible:outline-1 focus-visible:outline-os-accent ${block.kind === "entry" ? "grid grid-cols-[3rem_minmax(0,1fr)] gap-3 sm:gap-5" : ""}`}>
          {block.kind === "entry" ? <span className="os-num pt-2 text-xs text-os-faint">{block.time}</span> : null}
          <div className="min-w-0">
            {block.kind === "assistant" ? <h2 className="os-label mb-3 text-os-accent">{block.title} · From your assistant</h2> : block.kind !== "entry" ? <h2 className="os-serif mb-3 text-2xl text-os-ink">{block.title}</h2> : null}
            {block.body.trim() ? <MarkdownRenderer content={block.body} plainCheckboxes /> : null}
          </div>
        </div>
      ))}
      {!active && status ? <p role="status" className="mt-3 text-sm text-os-done">{status}</p> : null}
    </div>
  );
}
