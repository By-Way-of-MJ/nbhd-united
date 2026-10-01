"use client";

import { type FormEvent, useState } from "react";

import { ErrorLine, fieldCls, ghostBtnCls, labelCls, PanelHeader, quietBtnCls, SectionHead, SidePanel, textBtnCls } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { getErrorMessage } from "@/lib/errors";
import { plural } from "@/lib/neighborhood";
import { clusterCss } from "@/lib/people-sky/renderer";
import { useCreateCircleMutation, useJoinCircleMutation } from "@/lib/queries";
import type { CircleDetail, CircleSummary } from "@/lib/types";

function membersPreview(detail: CircleDetail | undefined): string {
  if (!detail) return "";
  const names = detail.members.map((m) => (m.is_me ? "You" : m.display_name.split(/\s+/)[0]));
  const me = names.indexOf("You");
  if (me > 0) names.unshift(names.splice(me, 1)[0]);
  if (names.length <= 3) return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0] ?? "";
  return `${names.slice(0, 2).join(", ")} and ${names.length - 2} more`;
}

/**
 * Clusters (Circles in the API): groups of neighbors who look out for each
 * other. Name, how many, who; start one, or join with a code.
 */
export function ClustersSection({
  circles,
  details,
  loading,
  joinCode,
  onOpen,
  onCreate,
}: {
  circles: CircleSummary[];
  details: Map<string, CircleDetail>;
  loading: boolean;
  joinCode: string;
  onOpen: (id: string) => void;
  onCreate: () => void;
}) {
  const [joining, setJoining] = useState(!!joinCode);
  return (
    <section aria-labelledby="clusters-heading" className="min-w-0 flex-1">
      <SectionHead
        id="clusters-heading"
        label="Clusters"
        trailing={
          <button type="button" onClick={onCreate} className={textBtnCls}>
            + Start a cluster
          </button>
        }
      />
      <p className="mb-1 mt-1 text-[0.8125rem] leading-relaxed text-os-faint">Groups of neighbors who look out for each other &mdash; mutual aid, the block, the school run.</p>
      {loading ? (
        <p className="os-hairline-top py-4 text-[0.9375rem] text-os-muted">Loading&hellip;</p>
      ) : circles.length === 0 ? (
        <p className="os-hairline-top py-4 text-[0.9375rem] text-os-muted">You&rsquo;re not in a cluster yet. Start one for your block, your building or your school run.</p>
      ) : (
        <ul>
          {circles.map((c) => {
            const d = details.get(c.circle_id);
            const who = membersPreview(d);
            return (
              <li key={c.circle_id}>
                <button type="button" onClick={() => onOpen(c.circle_id)} className="os-focus os-hairline-top group flex w-full flex-col gap-1.5 py-3.5 text-left">
                  <span className="flex w-full items-baseline justify-between gap-4">
                    <span className="flex min-w-0 items-baseline gap-2.5">
                      <span className="h-2 w-2 shrink-0 -translate-y-1 rounded-full" style={{ backgroundColor: clusterCss(c.hue) }} aria-hidden="true" />
                      <span className="os-serif truncate text-[1.5rem] leading-tight text-white transition group-hover:text-os-accent sm:text-[1.625rem]">{c.name}</span>
                    </span>
                    <span className="shrink-0 text-[0.75rem] text-os-faint">{plural(c.member_count, "person", "people")}</span>
                  </span>
                  <span className="text-[0.875rem] text-os-muted">
                    {who || d?.description || " "}
                    {c.my_role === "admin" ? <span className="text-os-faint"> &middot; you host</span> : null}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <div className="os-hairline-top pt-2.5">
        {joining ? <JoinClusterForm initialCode={joinCode} onDone={() => setJoining(false)} /> : (
          <p className="text-[0.8125rem] text-os-faint">
            Have an invite code?{" "}
            <button type="button" onClick={() => setJoining(true)} className={`${textBtnCls} min-h-[36px]`}>
              Join a cluster
            </button>
          </p>
        )}
      </div>
    </section>
  );
}

/** Whether your assistant may learn from the cluster's conversation — off unless you say so. */
function AssistantLearning({ checked, onChange }: { checked: boolean; onChange: (next: boolean) => void }) {
  return (
    <label className="flex cursor-pointer items-start gap-3">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="mt-1 h-4 w-4 shrink-0 accent-[var(--os-accent)]" />
      <span className="flex flex-col gap-0.5">
        <span className="text-[0.875rem] text-os-ink">Let my assistant learn from the cluster conversation</span>
        <span className="text-[0.75rem] leading-relaxed text-os-faint">Optional, and separate from sharing with people. You can change it later on the cluster&rsquo;s page.</span>
      </span>
    </label>
  );
}

function JoinClusterForm({ initialCode, onDone }: { initialCode: string; onDone: () => void }) {
  const join = useJoinCircleMutation();
  const [code, setCode] = useState(initialCode);
  const [learning, setLearning] = useState(false);
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const trimmed = code.trim();
    if (!trimmed) return;
    setError("");
    try {
      await join.mutateAsync({ code: trimmed, assistantLearning: learning });
      emitToast("You’re in.", "success");
      setCode("");
      onDone();
    } catch (err) {
      setError(getErrorMessage(err));
    }
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <div className="flex items-end gap-3">
        <label className="flex min-w-0 flex-1 flex-col gap-1">
          <span className={labelCls}>Invite code</span>
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="Paste a code"
            autoCapitalize="none"
            autoCorrect="off"
            autoFocus
            className={fieldCls}
          />
        </label>
        <button type="submit" className={ghostBtnCls} disabled={!code.trim() || join.isPending}>
          {join.isPending ? "Joining…" : "Join"}
        </button>
        <button type="button" className={quietBtnCls} onClick={onDone}>
          Cancel
        </button>
      </div>
      <p className="text-[0.75rem] leading-relaxed text-os-faint">You can join a cluster started by someone you&rsquo;re already connected with. Its members can see your name and what you share there.</p>
      <AssistantLearning checked={learning} onChange={setLearning} />
      {error ? <ErrorLine>{error}</ErrorLine> : null}
    </form>
  );
}

/** Start a cluster: a name, an optional line, and its colour in the sky. */
export function CreateClusterPanel({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const create = useCreateCircleMutation();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [hue, setHue] = useState(() => Math.floor(Math.random() * 360));
  const [learning, setLearning] = useState(false);
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setError("");
    try {
      const res = await create.mutateAsync({ name: name.trim(), description: description.trim() || undefined, hue, agent_absorb_enabled: learning });
      emitToast("Cluster started.", "success");
      onCreated(res.circle_id);
    } catch (err) {
      setError(getErrorMessage(err));
    }
  };
  return (
    <SidePanel label="Start a cluster" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow="New cluster" title="Start a cluster" />
        <p className="text-[0.9375rem] leading-relaxed text-os-muted">A group of neighbors who look out for each other. You&rsquo;ll get a link and a QR code to share yourself; people choose whether to join, and members can see one another and the conversation.</p>
        <label className="flex flex-col gap-1">
          <span className={labelCls}>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Building 3" maxLength={120} className={fieldCls} data-autofocus />
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelCls}>What brings you together (optional)</span>
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} maxLength={400} placeholder="Saturday market rota, borrowing tools, the school run…" className={`${fieldCls} resize-none py-2.5`} />
        </label>
        <div className="flex items-center gap-4">
          <span className="h-10 w-10 shrink-0 rounded-full" style={{ background: `radial-gradient(circle, ${clusterCss(hue)} 0%, transparent 70%)` }} aria-hidden="true" />
          <label className="flex min-w-0 flex-1 flex-col gap-2">
            <span className={labelCls}>Its colour in your sky</span>
            <input type="range" min={0} max={359} value={hue} onChange={(e) => setHue(Number(e.target.value))} className="w-full accent-[var(--os-accent)]" aria-label="Cluster colour" />
          </label>
        </div>
        <AssistantLearning checked={learning} onChange={setLearning} />
        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <div className="mt-auto flex items-center gap-4 pt-4">
          <button type="submit" className={ghostBtnCls} disabled={!name.trim() || create.isPending}>
            {create.isPending ? "Starting…" : "Start the cluster"}
          </button>
          <button type="button" className={quietBtnCls} onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </SidePanel>
  );
}
