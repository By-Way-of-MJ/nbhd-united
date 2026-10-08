"use client";

import clsx from "clsx";
import { type FormEvent, useState } from "react";

import { capsuleCls, DetailRow, doneCapsuleCls, sectionLabelCls, stepColor, toneCls } from "@/components/neighborhood/project/parts";
import type { ProjectActions } from "@/components/neighborhood/project/use-project";
import { CloseButton, dangerBtnCls, ErrorLine, quietBtnCls, textBtnCls } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { messageTime } from "@/lib/neighborhood";
import {
  blockersOf,
  canCompleteStep,
  canEditStep,
  dependentsOf,
  findMilestone,
  myAssignment,
  ownerLabel,
  ownersOf,
  type PlanStep,
  type ProjectPlan,
  slipLine,
  stepFootnote,
  stepStatus,
  whenLong,
} from "@/lib/project-plan";
import { useAddMissionUpdateMutation, useMissionDetailQuery } from "@/lib/queries";

/**
 * One step: who has it, when, what has to finish first and what it unlocks,
 * then the things you can do about it. Only someone who took the step can mark
 * it done.
 */
export function StepDetail({
  plan,
  step,
  actions,
  busy,
  onSelect,
  onEdit,
  onAsk,
  onAnswer,
  onClose,
}: {
  plan: ProjectPlan;
  step: PlanStep;
  actions: ProjectActions;
  busy: boolean;
  onSelect: (stepId: string) => void;
  onEdit: (step: PlanStep) => void;
  onAsk: (step: PlanStep) => void;
  onAnswer: (step: PlanStep) => void;
  /** Set when shown in a panel (phones, and from Timeline / People). */
  onClose?: () => void;
}) {
  const [error, setError] = useState("");
  const [confirmRemove, setConfirmRemove] = useState(false);
  const milestone = findMilestone(plan, step.milestoneId);
  const status = stepStatus(plan, step);
  const when = whenLong(step);
  const blockers = blockersOf(plan, step.id);
  const unlocks = dependentsOf(plan, step.id);
  const canEdit = canEditStep(plan, step);
  const canComplete = canCompleteStep(plan, step);
  const asked = myAssignment(plan, step)?.status === "asked";
  const slip = slipLine(plan, step);

  const toggleDone = async () => {
    setError("");
    const result = await actions.setDone(step, step.status !== "done");
    if (!result.ok) setError(result.error);
  };
  const remove = async () => {
    setError("");
    const result = await actions.deleteStep(step);
    if (!result.ok) {
      setError(result.error);
      setConfirmRemove(false);
    } else onClose?.();
  };
  const link = (other: PlanStep) => (
    <button key={other.id} type="button" onClick={() => onSelect(other.id)} className="os-focus rounded text-left text-os-ink underline decoration-os-ring underline-offset-[3px] transition hover:text-os-accent hover:decoration-os-accent-line">
      {other.title}
      {other.status === "done" ? <span className="text-os-faint no-underline"> (done)</span> : null}
    </button>
  );
  const list = (steps: PlanStep[]) => steps.map((s, i) => [i > 0 ? <span key={`sep-${s.id}`}>, </span> : null, link(s)]);

  return (
    <article aria-label={step.title} className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-2.5">
          <p className={sectionLabelCls}>
            Step{milestone ? ` · ${milestone.title}` : ""} · <span className={toneCls[status.tone]}>{status.word}</span>
          </p>
          <h2 className="os-serif break-words text-[1.875rem] leading-[1.08] text-white sm:text-[2.125rem]">{step.title}</h2>
        </div>
        {onClose ? <CloseButton onClose={onClose} /> : null}
      </div>

      <dl className="grid grid-cols-[104px_1fr] gap-x-3 gap-y-2.5 border-t border-os-hairline pt-3.5 text-[0.875rem] leading-relaxed">
        <DetailRow label="Who">
          <span className="inline-flex items-center gap-2">
            <span className="h-[7px] w-[7px] shrink-0 rounded-full" style={{ backgroundColor: stepColor(plan, step) }} aria-hidden="true" />
            {ownerLabel(plan, step)}
          </span>
        </DetailRow>
        <DetailRow label="When">
          {when ?? (
            canEdit ? (
              <button type="button" onClick={() => onEdit(step)} className="os-focus rounded text-os-accent hover:text-white">
                Add dates
              </button>
            ) : (
              <span className="text-os-muted">No dates yet</span>
            )
          )}
        </DetailRow>
        <DetailRow label="Waiting on">{blockers.length ? list(blockers) : <span className="text-os-muted">Nothing — it can start now</span>}</DetailRow>
        <DetailRow label="Then unlocks">{unlocks.length ? list(unlocks) : <span className="text-os-muted">—</span>}</DetailRow>
        {step.slackDays !== null ? (
          <DetailRow label="Room to slip">
            <span className={step.slackDays <= 0 ? "text-os-attn" : "text-os-muted"}>{step.slackDays <= 0 ? "None" : `${step.slackDays} day${step.slackDays === 1 ? "" : "s"}`}</span>
          </DetailRow>
        ) : null}
      </dl>

      {step.description ? <p className="whitespace-pre-wrap text-[0.9375rem] leading-relaxed text-os-muted">{step.description}</p> : null}
      {slip ? <p className="text-[0.8125rem] text-os-attn">{slip}</p> : null}

      {asked ? (
        <button type="button" onClick={() => onAnswer(step)} className="os-focus flex min-h-[44px] items-center gap-2 self-start rounded text-[0.9375rem] font-semibold text-os-attn hover:text-white">
          You were asked to take this — answer
        </button>
      ) : null}

      {error ? <ErrorLine>{error}</ErrorLine> : null}

      {confirmRemove ? (
        <div className="flex flex-col gap-1.5 border-t border-os-hairline pt-3">
          <p className="text-[0.9375rem] text-os-ink">Remove this step?</p>
          <p className="text-[0.8125rem] text-os-muted">Anyone who took it keeps the task in their own journal.</p>
          <div className="flex gap-5">
            <button type="button" className={dangerBtnCls} disabled={busy} onClick={() => void remove()}>
              {busy ? "Removing…" : "Remove step"}
            </button>
            <button type="button" className={quietBtnCls} disabled={busy} onClick={() => setConfirmRemove(false)}>
              Keep it
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2.5">
          {canComplete ? (
            <button type="button" className={step.status === "done" ? capsuleCls : doneCapsuleCls} disabled={busy} onClick={() => void toggleDone()}>
              {step.status === "done" ? "Reopen" : "Mark done"}
            </button>
          ) : null}
          {canEdit ? (
            <button type="button" className={capsuleCls} onClick={() => onEdit(step)}>
              {when ? "Edit" : "Edit dates"}
            </button>
          ) : null}
          <button type="button" className={capsuleCls} onClick={() => onAsk(step)}>
            {ownersOf(plan, step).length === 0 ? "Ask someone" : "Ask more people"}
          </button>
          {canEdit ? (
            <button type="button" className={clsx(dangerBtnCls, "ml-1")} onClick={() => setConfirmRemove(true)}>
              Remove
            </button>
          ) : null}
        </div>
      )}

      <p className="text-[0.8125rem] text-os-faint">{stepFootnote(plan, step)}</p>
    </article>
  );
}

const UPDATE_KINDS = ["note", "progress", "milestone"] as const;

/**
 * The project's shared updates (note / progress / milestone) — the feed the
 * project has always had, kept under the plan.
 */
export function ProjectUpdates({ missionId }: { missionId: string }) {
  const { data: mission } = useMissionDetailQuery(missionId);
  const addUpdate = useAddMissionUpdateMutation();
  const [text, setText] = useState("");
  const [kind, setKind] = useState<(typeof UPDATE_KINDS)[number]>("note");
  const [all, setAll] = useState(false);
  const updates = mission?.updates ?? [];
  const shown = all ? updates : updates.slice(0, 5);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t || addUpdate.isPending) return;
    try {
      await addUpdate.mutateAsync({ id: missionId, data: { kind, text: t } });
      setText("");
      emitToast("Shared with the project.", "success");
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };

  return (
    <section aria-label="Updates" className="flex flex-col gap-2 border-t border-os-hairline pt-3.5">
      <h3 className={sectionLabelCls}>Updates</h3>
      {updates.length === 0 ? <p className="text-[0.875rem] text-os-faint">No updates yet. Share how it&rsquo;s going.</p> : null}
      <ul className="flex flex-col">
        {shown.map((u) => (
          <li key={u.id} className="flex flex-col gap-0.5 py-1.5">
            <span className="text-[0.9375rem] leading-relaxed text-os-ink">{u.text}</span>
            <span className="text-[0.75rem] text-os-faint">
              {u.author_name}
              {u.kind && u.kind !== "note" && u.kind !== "task_added" && u.kind !== "task_completed" ? ` · ${u.kind}` : ""} &middot; {messageTime(u.created_at)}
            </span>
          </li>
        ))}
      </ul>
      {!all && updates.length > shown.length ? (
        <button type="button" className={`${quietBtnCls} self-start`} onClick={() => setAll(true)}>
          Earlier updates ({updates.length - shown.length})
        </button>
      ) : null}
      <form onSubmit={submit} className="flex flex-col gap-1">
        <div className="flex items-center gap-3 border-b border-os-ring focus-within:border-os-accent">
          <label htmlFor="project-update" className="sr-only">
            Share an update
          </label>
          <input
            id="project-update"
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={500}
            placeholder="Share an update…"
            className="min-h-[44px] min-w-0 flex-1 bg-transparent text-[0.9375rem] text-os-ink outline-none placeholder:text-os-faint focus-visible:shadow-none focus-visible:outline-none"
          />
          <button type="submit" className={textBtnCls} disabled={!text.trim() || addUpdate.isPending}>
            {addUpdate.isPending ? "Posting…" : "Post"}
          </button>
        </div>
        <div className="flex gap-4 text-[0.75rem]" role="radiogroup" aria-label="Kind of update">
          {UPDATE_KINDS.map((k) => (
            <button key={k} type="button" role="radio" aria-checked={kind === k} onClick={() => setKind(k)} className={clsx("os-focus min-h-[32px] rounded capitalize", kind === k ? "text-os-accent" : "text-os-faint hover:text-os-ink")}>
              {k}
            </button>
          ))}
        </div>
      </form>
    </section>
  );
}
