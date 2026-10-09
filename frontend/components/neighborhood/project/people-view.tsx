"use client";

import clsx from "clsx";

import { memberColor, rowHoverCls, toneCls } from "@/components/neighborhood/project/parts";
import {
  blockersOf,
  type Day,
  dependentsOf,
  describeStep,
  findMilestone,
  isInvitedMember,
  memberName,
  people,
  personLine,
  personStepState,
  personSteps,
  type PlanStep,
  type ProjectPlan,
  whenLabel,
} from "@/lib/project-plan";

/**
 * Who's doing what: each person's part of the plan side by side — their steps,
 * when, what each needs first and what it unlocks.
 */
export function PeopleView({ plan, today, onOpenStep }: { plan: ProjectPlan; today: Day; onOpenStep: (step: PlanStep) => void }) {
  const everyone = people(plan);
  return (
    <div className="grid gap-x-12 gap-y-10" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 300px), 1fr))" }}>
      {everyone.map((person) => {
        const steps = personSteps(plan, person.id);
        const mine = person.id === plan.myMembershipId;
        const role = isInvitedMember(person) ? "invited" : person.role === "owner" ? "owner" : "member";
        return (
          <section key={person.id} aria-label={mine ? "Your part" : `${person.displayName}’s part`} className="flex min-w-0 flex-col">
            <div className="flex items-baseline gap-2.5">
              <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: memberColor(plan, person) }} aria-hidden="true" />
              <h2 className="os-serif min-w-0 truncate text-[1.75rem] leading-tight text-white sm:text-[1.875rem]">{memberName(plan, person)}</h2>
              <span className="shrink-0 text-[0.75rem] text-os-faint">{role}</span>
            </div>
            <p className="pb-2.5 pt-1 text-[0.875rem] leading-relaxed text-os-muted">{personLine(plan, person)}</p>
            {steps.map((step) => {
              const state = personStepState(plan, step, person.id, today);
              const needs = blockersOf(plan, step.id);
              const unlocks = dependentsOf(plan, step.id);
              return (
                <button key={step.id} type="button" onClick={() => onOpenStep(step)} className={clsx("os-focus flex w-full flex-col gap-1 border-t border-os-hairline px-1 py-3 text-left", rowHoverCls)}>
                  <span className="flex items-baseline justify-between gap-3">
                    <span className={clsx("min-w-0 text-[0.9375rem] leading-snug", step.status === "done" ? "text-os-faint" : "text-white")}>{step.title}</span>
                    <span className={clsx("shrink-0 text-[0.75rem]", toneCls[state.tone])}>{state.word}</span>
                  </span>
                  <span className="text-[0.75rem] text-os-faint">{[whenLabel(step) ?? "no dates yet", findMilestone(plan, step.milestoneId)?.title].filter(Boolean).join(" · ")}</span>
                  {needs.length ? (
                    <span className="text-[0.75rem] leading-relaxed text-os-faint">
                      Needs first: <span className="text-os-muted">{needs.map((s) => describeStep(plan, s)).join(", ")}</span>
                    </span>
                  ) : null}
                  {unlocks.length ? (
                    <span className="text-[0.75rem] leading-relaxed text-os-faint">
                      Then unlocks: <span className="text-os-muted">{unlocks.map((s) => describeStep(plan, s)).join(", ")}</span>
                    </span>
                  ) : null}
                </button>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}
