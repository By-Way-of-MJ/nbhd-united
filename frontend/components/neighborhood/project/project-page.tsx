"use client";

import clsx from "clsx";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { Diamond, Icon, PillTabs, sectionLabelCls, toneCls, useMediaQuery, useMounted } from "@/components/neighborhood/project/parts";
import { PeopleView } from "@/components/neighborhood/project/people-view";
import { PlanList } from "@/components/neighborhood/project/plan-view";
import { AddPeoplePanel, ConfirmExitPanel, GoalLinkPanel, MilestonePanel, RenamePanel } from "@/components/neighborhood/project/project-forms";
import { ProjectUpdates, StepDetail } from "@/components/neighborhood/project/step-detail";
import { AnswerAskPanel, AskSomeonePanel, StepFormPanel } from "@/components/neighborhood/project/step-forms";
import { TimelineView } from "@/components/neighborhood/project/timeline-view";
import { projectError, useProject } from "@/components/neighborhood/project/use-project";
import { ProjectPanel } from "@/components/neighborhood/projects";
import { ghostBtnCls, SidePanel } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import {
  canEditProject,
  crewLine,
  doneLine,
  findStep,
  healthWords,
  localToday,
  milestoneLine,
  myAssignment,
  myMember,
  orderedSteps,
  type PlanMilestone,
  type PlanStep,
  type ProjectPlan,
  upNext,
} from "@/lib/project-plan";
import { useNeighborhoodHomeQuery, useProjectProposalsQuery, useProjectsV2Enabled, useTenantQuery } from "@/lib/queries";
import type { ProjectProposal } from "@/lib/types";

type Tab = "plan" | "timeline" | "people";
const TABS: { value: Tab; label: string }[] = [
  { value: "plan", label: "Plan" },
  { value: "timeline", label: "Timeline" },
  { value: "people", label: "People" },
];

type Sheet =
  | { kind: "add-step" }
  | { kind: "edit-step"; stepId: string }
  | { kind: "ask"; stepId: string }
  | { kind: "answer"; stepId: string }
  | { kind: "step"; stepId: string }
  | { kind: "add-milestone" }
  | { kind: "edit-milestone"; milestoneId: string }
  | { kind: "rename" }
  | { kind: "people" }
  | { kind: "goal" }
  | { kind: "leave" }
  | { kind: "delete" }
  | null;

export function BackToNeighborhood() {
  return (
    <Link href="/friends" className="os-focus -ml-1 inline-flex min-h-[40px] items-center gap-1.5 rounded px-1 text-[0.875rem] text-os-accent hover:text-white">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
        <path d="M15 6l-6 6 6 6" />
      </svg>
      Neighborhood
    </Link>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="pb-16">
      <BackToNeighborhood />
      {children}
    </div>
  );
}

/**
 * A shared project on its own page: one serif title, then Plan · Timeline ·
 * People switching in place. Open Sky: hairlines and small uppercase labels,
 * teal only for done. Tenants without Projects v2 get the project they've
 * always had (this week, next steps, updates).
 */
export function ProjectPage() {
  const params = useSearchParams();
  const router = useRouter();
  const missionId = params.get("id") ?? "";
  const { data: tenant, isLoading: tenantLoading } = useTenantQuery();
  const v2 = useProjectsV2Enabled();
  const mounted = useMounted();

  if (!mounted || (!tenant && tenantLoading)) {
    return (
      <Frame>
        <p className="mt-6 text-[0.9375rem] text-os-muted">Loading&hellip;</p>
      </Frame>
    );
  }
  if (!missionId) {
    return (
      <Frame>
        <h1 className="os-page-title mt-2">Project</h1>
        <p className="mt-4 text-[0.9375rem] text-os-muted">Choose a project from your Neighborhood to open it.</p>
      </Frame>
    );
  }
  if (!v2) return <LegacyProject missionId={missionId} onClose={() => router.push("/friends")} />;
  const tab = params.get("tab");
  return <ProjectV2 key={missionId} missionId={missionId} initialTab={tab === "timeline" || tab === "people" ? tab : "plan"} initialStepId={params.get("step")} />;
}

/** Projects v2 off for this account: the same project panel the Neighborhood has always opened. */
function LegacyProject({ missionId, onClose }: { missionId: string; onClose: () => void }) {
  const home = useNeighborhoodHomeQuery();
  const neighbors = home.data?.neighbors;
  const names = useMemo(() => new Map((neighbors ?? []).map((n) => [n.handle, n.display_name])), [neighbors]);
  return (
    <Frame>
      <h1 className="os-page-title mt-2">Project</h1>
      <ProjectPanel missionId={missionId} myHandle={home.data?.profile?.handle ?? null} names={names} onClose={onClose} />
    </Frame>
  );
}

function ProjectV2({ missionId, initialTab, initialStepId }: { missionId: string; initialTab: Tab; initialStepId: string | null }) {
  const router = useRouter();
  const qc = useQueryClient();
  const { plan, query, busy, actions } = useProject(missionId);
  const proposalsQ = useProjectProposalsQuery(missionId);
  const [tab, setTab] = useState<Tab>(initialTab);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sheet, setSheet] = useState<Sheet>(null);
  const [routed, setRouted] = useState(false);
  const wide = useMediaQuery("(min-width: 1024px)");
  const today = localToday();

  // A link to one step (a notification, a shared URL) opens that step — as the
  // question, if it's one I've been asked. Decided during render, once.
  if (plan && !routed) {
    setRouted(true);
    const step = findStep(plan, initialStepId);
    if (step) {
      if (myAssignment(plan, step)?.status === "asked") setSheet({ kind: "answer", stepId: step.id });
      else {
        setSelectedId(step.id);
        if (initialTab !== "plan") setSheet({ kind: "step", stepId: step.id });
      }
    }
  }

  if (!plan) {
    return (
      <Frame>
        <h1 className="os-page-title mt-2">Project</h1>
        {query.isLoading ? (
          <p className="mt-4 text-[0.9375rem] text-os-muted">Loading&hellip;</p>
        ) : (
          <div className="mt-4 flex flex-col items-start gap-3">
            <p className="text-[0.9375rem] text-os-muted">{query.error ? projectError(query.error) : "Couldn’t read this project."}</p>
            <button type="button" className={ghostBtnCls} onClick={() => void query.refetch()}>
              Try again
            </button>
          </div>
        )}
      </Frame>
    );
  }

  const steps = orderedSteps(plan);
  const selected = findStep(plan, selectedId) ?? upNext(plan) ?? steps[0];
  const sheetStep = sheet && "stepId" in sheet ? findStep(plan, sheet.stepId) : undefined;
  const health = healthWords(plan.health);
  const linkedGoal = myMember(plan)?.linkedGoalTitle;
  const close = () => setSheet(null);

  const openStep = (step: PlanStep) => {
    setSelectedId(step.id);
    // The wide Plan shows the step beside the list; everywhere else it opens in a panel.
    if (!(wide && tab === "plan")) setSheet({ kind: "step", stepId: step.id });
  };
  const decide = async (proposal: ProjectProposal, approve: boolean) => {
    const result = await actions.decide(proposal, approve);
    if (!result.ok) emitToast(result.error, "error");
    void qc.invalidateQueries({ queryKey: ["project-proposals"] });
  };
  const gone = (message: string) => {
    void qc.invalidateQueries({ queryKey: ["missions"] });
    emitToast(message, "success");
    router.push("/friends");
  };
  const detailProps = {
    plan,
    actions,
    busy,
    onSelect: (id: string) => {
      setSelectedId(id);
      if (sheet?.kind === "step") setSheet({ kind: "step", stepId: id });
    },
    onEdit: (step: PlanStep) => setSheet({ kind: "edit-step", stepId: step.id }),
    onAsk: (step: PlanStep) => setSheet({ kind: "ask", stepId: step.id }),
    onAnswer: (step: PlanStep) => setSheet({ kind: "answer", stepId: step.id }),
  };

  return (
    <Frame>
      <header className="mt-1 flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
        <div className="flex min-w-0 flex-col gap-2">
          <p className={sectionLabelCls}>Project &middot; {crewLine(plan)}</p>
          <h1 className="os-page-title break-words">{plan.title}</h1>
          <p className="text-[0.875rem] leading-relaxed text-os-muted">
            {plan.goal ? <>{plan.goal} &middot; </> : null}
            <span className="text-white">{doneLine(plan)}</span>
            {plan.total > 0 ? (
              <>
                {" "}
                &middot; <span className={toneCls[health.tone]}>{health.short}</span>
              </>
            ) : null}
          </p>
          <button type="button" onClick={() => setSheet({ kind: "goal" })} title="Only you see this link." className="os-focus -my-1 flex min-h-[36px] items-center gap-2 self-start rounded text-[0.8125rem]">
            <span className="text-os-accent">
              <Icon name="target" size={14} />
            </span>
            {linkedGoal ? (
              <span className="text-os-muted">
                Part of my goal: <span className="text-os-ink">{linkedGoal}</span>
              </span>
            ) : (
              <span className="text-os-accent hover:text-white">Link to one of my goals</span>
            )}
          </button>
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          <PillTabs label="View" options={TABS} value={tab} onChange={setTab} />
          <AddMenu plan={plan} onPick={setSheet} />
        </div>
      </header>

      {plan.milestones.length ? <MilestoneStrip plan={plan} onEdit={(m) => setSheet({ kind: "edit-milestone", milestoneId: m.id })} /> : null}

      <div className={clsx(plan.milestones.length ? "mt-5" : "os-hairline-top mt-5 pt-5")}>
        {tab === "plan" ? (
          <div className="flex flex-col gap-8 lg:flex-row lg:gap-0">
            <div className="min-w-0 lg:w-[430px] lg:shrink-0 lg:border-r lg:border-os-hairline lg:pr-6">
              <PlanList
                plan={plan}
                today={today}
                selectedId={wide ? (selected?.id ?? null) : null}
                proposals={proposalsQ.data ?? []}
                busy={busy}
                onSelect={openStep}
                onAnswer={(step) => setSheet({ kind: "answer", stepId: step.id })}
                onDecide={(p, approve) => void decide(p, approve)}
                onAddStep={() => setSheet({ kind: "add-step" })}
                onAddMilestone={() => setSheet({ kind: "add-milestone" })}
                onEditMilestone={(m) => setSheet({ kind: "edit-milestone", milestoneId: m.id })}
              />
            </div>
            <div className="flex min-w-0 flex-1 flex-col gap-7 lg:pl-8">
              {wide && selected ? <StepDetail key={selected.id} step={selected} {...detailProps} /> : null}
              {wide && !selected ? <p className="text-[0.9375rem] text-os-muted">No steps yet.</p> : null}
              <ProjectUpdates missionId={missionId} />
            </div>
          </div>
        ) : tab === "timeline" ? (
          <TimelineView plan={plan} today={today} onOpenStep={openStep} onAddDates={(step) => setSheet({ kind: "edit-step", stepId: step.id })} />
        ) : (
          <PeopleView plan={plan} today={today} onOpenStep={openStep} />
        )}
      </div>

      {sheet?.kind === "step" && sheetStep ? (
        <SidePanel label={sheetStep.title} onClose={close} wide>
          <StepDetail key={sheetStep.id} step={sheetStep} {...detailProps} onClose={close} />
        </SidePanel>
      ) : null}
      {sheet?.kind === "add-step" ? <StepFormPanel plan={plan} actions={actions} onClose={close} onAdded={setSelectedId} /> : null}
      {sheet?.kind === "edit-step" && sheetStep ? <StepFormPanel key={sheetStep.id} plan={plan} editing={sheetStep} actions={actions} onClose={close} /> : null}
      {sheet?.kind === "ask" && sheetStep ? <AskSomeonePanel plan={plan} step={sheetStep} actions={actions} onClose={close} /> : null}
      {sheet?.kind === "answer" && sheetStep ? <AnswerAskPanel plan={plan} step={sheetStep} actions={actions} onClose={close} /> : null}
      {sheet?.kind === "add-milestone" ? <MilestonePanel plan={plan} actions={actions} onClose={close} /> : null}
      {sheet?.kind === "edit-milestone" ? <MilestonePanel key={sheet.milestoneId} plan={plan} editing={plan.milestones.find((m) => m.id === sheet.milestoneId)} actions={actions} onClose={close} /> : null}
      {sheet?.kind === "rename" ? <RenamePanel plan={plan} actions={actions} onClose={close} /> : null}
      {sheet?.kind === "people" ? <AddPeoplePanel plan={plan} actions={actions} onClose={close} /> : null}
      {sheet?.kind === "goal" ? <GoalLinkPanel plan={plan} actions={actions} onClose={close} /> : null}
      {sheet?.kind === "leave" || sheet?.kind === "delete" ? <ConfirmExitPanel plan={plan} kind={sheet.kind} actions={actions} onClose={close} onGone={gone} /> : null}
    </Frame>
  );
}

/** Every milestone at a glance, always in view under the title. */
function MilestoneStrip({ plan, onEdit }: { plan: ProjectPlan; onEdit: (milestone: PlanMilestone) => void }) {
  const editable = canEditProject(plan);
  const current = plan.milestones.find((m) => !m.reached)?.id;
  return (
    // One line on a phone (it scrolls sideways); a row of columns from tablet up.
    <ul aria-label="Milestones" className="os-hairline-top mt-5 flex gap-x-7 overflow-x-auto pt-3 [scrollbar-width:none] sm:grid sm:gap-x-8 sm:gap-y-2 sm:overflow-visible sm:[grid-template-columns:repeat(auto-fit,minmax(220px,1fr))]">
      {plan.milestones.map((m) => {
        const inner = (
          <>
            <Diamond reached={m.reached} active={m.id === current} />
            <span className="os-serif min-w-0 truncate text-[1.25rem] leading-tight text-white">{m.title}</span>
            <span className="shrink-0 text-[0.75rem] text-os-faint">{milestoneLine(plan, m)}</span>
          </>
        );
        return (
          <li key={m.id} className="shrink-0 sm:min-w-0 sm:shrink">
            {editable ? (
              <button type="button" onClick={() => onEdit(m)} title="Edit this milestone" className="os-focus flex min-h-[40px] w-full min-w-0 items-center gap-2.5 rounded text-left">
                {inner}
              </button>
            ) : (
              <span className="flex min-h-[40px] min-w-0 items-center gap-2.5">{inner}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** The "+" in the header: add a step, a milestone or people; rename, leave, delete. */
function AddMenu({ plan, onPick }: { plan: ProjectPlan; onPick: (sheet: Sheet) => void }) {
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
  const owner = canEditProject(plan);
  const item = (label: React.ReactNode, sheet: Sheet, opts: { tone?: string; disabled?: boolean } = {}) => (
    <button
      type="button"
      role="menuitem"
      disabled={opts.disabled}
      onClick={() => {
        setOpen(false);
        onPick(sheet);
      }}
      className={clsx("os-focus flex min-h-[44px] w-full items-center gap-2 rounded-xl px-3 text-left text-[0.875rem] hover:bg-os-accent-soft disabled:opacity-40", opts.tone ?? "text-os-ink")}
    >
      {label}
    </button>
  );
  const rule = <div className="mx-2 my-1 h-px bg-os-hairline" aria-hidden="true" />;
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label="Add or change"
        aria-haspopup="menu"
        aria-expanded={open}
        className="os-focus flex h-11 w-11 items-center justify-center rounded-full border border-os-accent-line text-os-accent transition hover:bg-os-accent-soft"
      >
        <Icon name="plus" />
      </button>
      {open ? (
        <div role="menu" className="absolute right-0 top-full z-20 mt-1.5 w-56 rounded-2xl border border-os-hairline bg-os-surface-solid p-1.5">
          {item("Add a step", { kind: "add-step" })}
          {item(
            <>
              <Diamond reached={false} size={11} /> Add a milestone
            </>,
            { kind: "add-milestone" },
            { disabled: plan.milestones.length >= 8 },
          )}
          {plan.canInvite ? item("Add people", { kind: "people" }) : null}
          {rule}
          {owner ? item("Rename project", { kind: "rename" }, { tone: "text-os-muted" }) : null}
          {item(myMember(plan)?.linkedGoalId ? "Change my goal link" : "Link to my goal", { kind: "goal" }, { tone: "text-os-muted" })}
          {rule}
          {item("Leave project", { kind: "leave" }, { tone: "text-os-danger" })}
          {owner ? item("Delete project", { kind: "delete" }, { tone: "text-os-danger" }) : null}
        </div>
      ) : null}
    </div>
  );
}
