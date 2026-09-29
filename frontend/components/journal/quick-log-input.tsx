"use client";

import { FormEvent, useState } from "react";

interface QuickLogInputProps {
  onSubmit: (content: string) => Promise<void>;
  isPending: boolean;
  writeFirst?: boolean;
}

export function QuickLogInput({ onSubmit, isPending, writeFirst = false }: QuickLogInputProps) {
  const [content, setContent] = useState("");

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!content.trim()) return;
    try {
      await onSubmit(content.trim());
      setContent("");
    } catch { /* The owning document view shows the append error; keep the draft. */ }
  };

  return (
    <>
      {writeFirst ? <p className="mb-2 text-xs leading-relaxed text-os-faint">Write something. It lands in today’s page with the time.</p> : null}
    <form data-os-quick-log onSubmit={handleSubmit} className="flex gap-2">
      <input
        type="text"
        placeholder={writeFirst ? "Write something…" : "Quick log entry..."}
        value={content}
        onChange={(e) => setContent(e.target.value)}
        aria-label={writeFirst ? "Write something" : "Quick log entry"}
        className={`${writeFirst ? "min-w-0 " : ""}min-h-[44px] flex-1 rounded-panel border border-border bg-surface px-3 py-2 text-sm placeholder:text-ink-faint focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent`}
      />
      <button
        type="submit"
        disabled={isPending || !content.trim()}
        className="min-h-[44px] rounded-full bg-accent px-4 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
      >
        {isPending ? "..." : writeFirst ? "Add" : "Log"}
      </button>
    </form>
    </>
  );
}
