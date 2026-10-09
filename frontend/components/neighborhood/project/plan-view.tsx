"use client";

import clsx from "clsx";

import { Diamond, OwnerDot, rowHoverCls, sectionLabelCls, toneCls } from "@/components/neighborhood/project/parts";
import { ghostBtnCls, quietBtnCls, textBtnCls } from "@/components/neighborhood/ui";
import {
  asksForMe,
  canEditProject,
  type Day,
  groupLabel,
  groupStatus,
  ownerLabel,
  planGroups,
  type PlanMilestone,
  type PlanStep,
  type ProjectPlan,
  stepState,
  undatedPlanHint,
  whenLabel,
} from "@/lib/project-plan";
import type { ProjectProposal } from "@/lib/types";

/** A step on one line: who (dot), what, "who · when", and the word for where it stands. */
export function StepRow({ plan, step, today, selected = false, onClick }: { plan: ProjectPlan; step: PlanStep; today: Day; selected?: boolean; onClick: () => void }) {
  const state = stepState(plan, step, today);
  const done = step.status === "done" || step.status === "skipped";
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={clsx(
        "os-focus flex min-h-[56px] w-full items-center gap-3 border-t px-2.5 py-2 text-left",
        selected ? "rounded-[10px] border-transparent bg-[rgba(226,232,240,0.08)]" : clsx("border-os-hairline", rowHoverCls),
      )}
    >
      <OwnerDot plan={plan} step={step} />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className={clsx("text-[0.9375rem] leading-snug", done ? "text-os-faint" : "text-white")}>{step.title}</span>
        <span className="truncate text-[0.75rem] text-os-faint">{[ownerLabel(plan, step), whenLabel(step) ?? "no dates yet"].join(" · ")}</span>
      </span>
      <span className={clsx("shrink-0 text-[0.75rem]", toneCls[state.tone])}>{state.word}</span>
    </button>
  );
}

/**
 * One suggestion from your assistant. Approve applies it (within the normal
 * rules); nothing happened before that. A suggestion made right after the
 * assistant read other people's project text says so.
 */
function SuggestionCard({ proposal, busy, onDecide }: { proposal: ProjectProposal; busy: boolean; onDecide: (approve: boolean) => void }) {
  return (
    <div className="flex flex-col gap-2 border-t border-os-hairline py-3">
      <p className="text-[0.9375rem] font-semibold text-os-ink">{proposal.summary || "A suggestion"}</p>
      <ul className="flex flex-col gap-1">
        {proposal.changes.map((line, i) => (
          <li key={i} className="flex gap-2 text-[0.875rem] leading-relaxed text-os-muted">
            <span className="text-os-faint" aria-hidden="true">
              &bull;
            </span>
            <span>{line}</span>
          </li>
        ))}
      </ul>
      {proposal.from_project_text ? <p className="text-[0.75rem] text-os-attn">Based on text other people wrote in this project — check it&rsquo;s what you want.</p> : null}
      {proposal.touches_others ? <p className="text-[0.75rem] text-os-faint">Anything that involves someone else is sent as your request — they still decide.</p> : null}
      <div className="flex items-center gap-4">
        <button type="button" className={ghostBtnCls} disabled={busy} onClick={() => onDecide(true)}>
          Approve
        </button>
        <button type="button" className={quietBtnCls} disabled={busy} onClick={() => onDecide(false)}>
          No thanks
        </button>
      </div>
    </div>
  );
}

/**
 * The plan as a list: what's asked of you, what your assistant suggests, then
 * every step under its milestone (diamond, date, how many to go).
 */
export function PlanList({
  plan,
  today,
  selectedId,
  proposals,
  busy,
  onSelect,
  onAnswer,
  onDecide,
  onAddStep,
  onAddMilestone,
  onEditMilestone,
}: {
  plan: ProjectPlan;
  today: Day;
  selectedId: string | null;
  proposals: ProjectProposal[];
  busy: boolean;
  onSelect: (step: PlanStep) => void;
  onAnswer: (step: PlanStep) => void;
  onDecide: (proposal: ProjectProposal, approve: boolean) => void;
  onAddStep: () => void;
  onAddMilestone: () => void;
  onEditMilestone: (milestone: PlanMilestone) => void;
}) {
  const asks = asksForMe(plan);
  const groups = planGroups(plan);
  const editable = canEditProject(plan);
  const hint = undatedPlanHint(plan);
  const current = plan.milestones.find((m) => !m.reached)?.id;

  return (
    <div className="flex flex-col gap-6">
      {asks.length ? (
        <section aria-labelledby="asked-heading">
          <h2 id="asked-heading" className={clsx(sectionLabelCls, "pb-1.5")}>
            Asked of you
          </h2>
          {asks.map((step) => (
            <StepRow key={step.id} plan={plan} step={step} today={today} onClick={() => onAnswer(step)} />
          ))}
        </section>
      ) : null}

      {proposals.length ? (
        <section aria-labelledby="assistant-heading">
          <h2 id="assistant-heading" className={clsx(sectionLabelCls, "pb-1.5")}>
            From your assistant
          </h2>
          {proposals.map((p) => (
            <SuggestionCard key={p.proposal_id} proposal={p} busy={busy} onDecide={(approve) => onDecide(p, approve)} />
          ))}
        </section>
      ) : null}

      {groups.map((group) => {
        const status = groupStatus(group);
        const heading = (
          <span className="flex min-w-0 items-center gap-2">
            {group.milestone ? <Diamond reached={group.milestone.reached} active={group.milestone.id === current} size={11} /> : null}
            <span className={clsx(sectionLabelCls, "truncate")}>{groupLabel(group)}</span>
          </span>
        );
        return (
          <section key={group.id} aria-label={groupLabel(group)}>
            <div className="flex min-h-[28px] items-center justify-between gap-3 pb-1">
              {group.milestone && editable ? (
                <button type="button" onClick={() => onEditMilestone(group.milestone as PlanMilestone)} title="Edit this milestone" className="os-focus min-w-0 rounded text-left hover:[&_span]:text-white">
                  {heading}
                </button>
              ) : (
                heading
              )}
              {group.milestone ? <span className={clsx("shrink-0 text-[0.75rem]", toneCls[status.tone])}>{status.word}</span> : null}
            </div>
            {group.steps.length === 0 ? <p className="flex min-h-[44px] items-center border-t border-os-hairline px-2.5 text-[0.875rem] text-os-faint">No steps yet.</p> : null}
            {group.steps.map((step) => (
              <StepRow key={step.id} plan={plan} step={step} today={today} selected={step.id === selectedId} onClick={() => onSelect(step)} />
            ))}
          </section>
        );
      })}

      {plan.steps.length === 0 ? <p className="text-[0.9375rem] leading-relaxed text-os-muted">Add the first step — something small someone can start this week.</p> : null}
      {hint ? <p className="text-[0.8125rem] leading-relaxed text-os-faint">{hint} Open a step and choose Edit to give it dates.</p> : null}

      <div className="flex flex-wrap gap-x-5 gap-y-1">
        <button type="button" className={textBtnCls} onClick={onAddStep}>
          + Add a step
        </button>
        {plan.milestones.length < 8 ? (
          <button type="button" className={clsx(textBtnCls, "gap-1.5")} onClick={onAddMilestone}>
            <Diamond reached={false} size={10} /> Add a milestone
          </button>
        ) : null}
      </div>
    </div>
  );
}
