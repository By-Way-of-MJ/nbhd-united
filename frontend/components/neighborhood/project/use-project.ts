"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";

import { emitToast } from "@/components/toast";
import {
  addProjectMembers,
  askProjectStep,
  createProjectDependency,
  createProjectMilestone,
  createProjectStep,
  decideProjectProposal,
  deleteProject,
  deleteProjectDependency,
  deleteProjectMilestone,
  deleteProjectStep,
  leaveMission,
  patchProjectMilestone,
  patchProjectStep,
  renameProject,
  respondProjectStep,
  setProjectLinkedGoal,
  setProjectStepDone,
  type StepAnswer,
} from "@/lib/api";
import { getErrorMessage } from "@/lib/errors";
import { dayIso, findMember, joinNames, memberName, orderedSteps, parsePlan, type Day, type PlanMilestone, type PlanStep, type ProjectPlan } from "@/lib/project-plan";
import { useProjectPlanQuery } from "@/lib/queries";
import type { ProjectPlanData, ProjectProposal } from "@/lib/types";

/** What a step form collects (dates as days; null = no date). */
export interface StepDraft {
  title: string;
  description: string;
  start: Day | null;
  due: Day | null;
  milestoneId: string | null;
}

export type Outcome<T = true> = { ok: true; value: T } | { ok: false; error: string };

const CONFLICT = "Someone changed this since you opened it. Here’s the latest — try again.";
const MILESTONE_FORBIDDEN = "Only a project owner or whoever added this milestone can change it.";

function statusOf(err: unknown): number | undefined {
  return err instanceof Error ? (err as Error & { status?: number }).status : undefined;
}

/** The words for a failed project write (the server's own detail when it sent one). */
export function projectError(err: unknown, forbidden?: string): string {
  const status = statusOf(err);
  if (status === 409) return CONFLICT;
  if (status === 403) return forbidden ?? "Only the step’s owner can change that. You can ask them.";
  if (status === 404) return "This project isn’t available anymore.";
  return getErrorMessage(err);
}

function stepBody(draft: StepDraft) {
  return {
    title: draft.title.trim(),
    ...(draft.description.trim() ? { description: draft.description.trim() } : {}),
    start_date: draft.start === null ? null : dayIso(draft.start),
    due_date: draft.due === null ? null : dayIso(draft.due),
    milestone_id: draft.milestoneId,
  };
}

/**
 * One shared project's plan plus every write the page can make to it.
 * Server-authoritative: each write re-reads the plan rather than guessing the
 * result, and a 409 (someone else changed it) reloads with friendly copy
 * instead of retrying blindly.
 */
export function useProject(missionId: string) {
  const qc = useQueryClient();
  const query = useProjectPlanQuery(missionId || null);
  const plan = useMemo(() => parsePlan(query.data), [query.data]);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);

  const reload = useCallback(async (): Promise<ProjectPlan | null> => {
    void qc.invalidateQueries({ queryKey: ["missions"] });
    void qc.invalidateQueries({ queryKey: ["mission", missionId] });
    void qc.invalidateQueries({ queryKey: ["project-proposals", missionId] });
    await qc.invalidateQueries({ queryKey: ["project-plan", missionId] });
    return parsePlan(qc.getQueryData<ProjectPlanData>(["project-plan", missionId]));
  }, [qc, missionId]);

  const write = useCallback(
    async <T>(fn: () => Promise<T>, opts: { receipt?: string | ((value: T) => string); forbidden?: string; reload?: boolean } = {}): Promise<Outcome<T>> => {
      if (busyRef.current) return { ok: false, error: "One moment — still saving the last change." };
      busyRef.current = true;
      setBusy(true);
      try {
        const value = await fn();
        if (opts.reload !== false) await reload();
        const receipt = typeof opts.receipt === "function" ? opts.receipt(value) : opts.receipt;
        if (receipt) emitToast(receipt, "success");
        return { ok: true, value };
      } catch (err) {
        if (statusOf(err) === 409) await reload();
        return { ok: false, error: projectError(err, opts.forbidden) };
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [reload],
  );

  const actions = useMemo(() => {
    const names = (p: ProjectPlan | null, ids: string[]) =>
      ids.map((id) => (p ? findMember(p, id) : undefined)).filter((m): m is NonNullable<typeof m> => !!m).map((m) => (p ? memberName(p, m).replace(/^You$/, "you") : m.displayName));

    return {
      /**
       * Creates the step, links what it waits on, then asks the chosen people.
       * Choosing yourself is your own yes — it's taken straight away.
       */
      addStep: (draft: StepDraft, waitsOn: string[], askIds: string[]) =>
        write(
          async () => {
            const created = await createProjectStep(missionId, stepBody(draft));
            const stepId = String(created.step_id);
            for (const blocker of waitsOn) await createProjectDependency(missionId, blocker, stepId);
            if (askIds.length) {
              await askProjectStep(missionId, stepId, askIds);
              if (plan && askIds.includes(plan.myMembershipId)) await respondProjectStep(missionId, stepId, { answer: "yes" });
            }
            return stepId;
          },
          { receipt: "Step added." },
        ),

      /**
       * Saves the step, then makes it wait on exactly `blockers`: links the new
       * ones, unlinks the dropped ones. The server refuses a loop; the picker
       * already hides those.
       */
      updateStep: (step: PlanStep, draft: StepDraft, blockers: Set<string> | null) =>
        write(
          async () => {
            await patchProjectStep(missionId, step.id, { ...stepBody(draft), description: draft.description.trim(), version: step.version });
            if (!plan || !blockers) return true as const;
            const current = plan.edges.filter((e) => e.blockedId === step.id);
            const existing = new Set(current.map((e) => e.blockerId));
            for (const edge of current) if (!blockers.has(edge.blockerId)) await deleteProjectDependency(missionId, edge.id);
            for (const id of orderedSteps(plan).map((s) => s.id)) if (blockers.has(id) && !existing.has(id)) await createProjectDependency(missionId, id, step.id);
            return true as const;
          },
          { receipt: "Saved." },
        ),

      deleteStep: (step: PlanStep) => write(() => deleteProjectStep(missionId, step.id).then(() => true as const), { receipt: "Step removed." }),

      ask: (stepId: string, memberIds: string[]) =>
        write(() => askProjectStep(missionId, stepId, memberIds).then(() => true as const), { receipt: memberIds.length ? `Asked ${joinNames(names(plan, memberIds))}.` : undefined }),

      respond: (stepId: string, answer: StepAnswer) =>
        write(() => respondProjectStep(missionId, stepId, answer).then(() => true as const), {
          receipt:
            answer.answer === "yes"
              ? "It’s yours — and it’s on your task list."
              : answer.answer === "dates"
                ? "Sent your dates."
                : answer.answer === "smaller"
                  ? "Sent — they’ll see what you can take."
                  : "No problem. Maybe next time.",
        }),

      setDone: (step: PlanStep, done: boolean) =>
        write(() => setProjectStepDone(missionId, step.id, done).then(() => true as const), { receipt: done ? "Done. Nice work." : "Reopened." }),

      addMilestone: (title: string, target: Day | null) =>
        write(() => createProjectMilestone(missionId, { title, target_date: target === null ? null : dayIso(target) }).then((r) => String(r.milestone_id)), { receipt: "Milestone added." }),

      updateMilestone: (milestone: PlanMilestone, title: string, target: Day | null) =>
        write(() => patchProjectMilestone(missionId, milestone.id, { title, target_date: target === null ? null : dayIso(target) }).then(() => true as const), {
          receipt: "Milestone saved.",
          forbidden: MILESTONE_FORBIDDEN,
        }),

      /** Its steps stay in the project, under "Other steps". */
      deleteMilestone: (milestone: PlanMilestone) =>
        write(() => deleteProjectMilestone(missionId, milestone.id).then(() => true as const), { receipt: "Milestone removed.", forbidden: MILESTONE_FORBIDDEN }),

      rename: (title: string) =>
        write(() => renameProject(missionId, title, plan?.version ?? 0).then(() => true as const), { receipt: "Renamed.", forbidden: "Only a project owner can rename it." }),

      /** Invites them; each still decides whether to join. */
      addPeople: (people: { friendshipId: string; name: string }[]) =>
        write(() => addProjectMembers(missionId, people.map((p) => p.friendshipId)).then(() => true as const), {
          receipt: `Invited ${joinNames(people.map((p) => p.name))}. They’ll decide whether to join.`,
          forbidden: "Only the person who started the project can add people.",
        }),

      /** Link this project to one of my own goals (null clears). Only I ever see it. */
      setLinkedGoal: (goalId: string | null) =>
        write(() => setProjectLinkedGoal(missionId, goalId).then(() => true as const), {
          receipt: goalId === null ? "Unlinked from your goal." : "Linked — your steps show under that goal in Horizons.",
        }),

      /** The human decides. Approving applies what the normal rules allow and reports the rest. */
      decide: (proposal: ProjectProposal, approve: boolean) =>
        write(() => decideProjectProposal(proposal.proposal_id, approve), {
          receipt: (result) => {
            if (!approve) return "Dismissed.";
            const skipped = (result.changes ?? []).filter((c) => c.outcome === "skipped").length;
            return skipped === 0 ? "Done — applied." : `Applied what I could; ${skipped} change${skipped === 1 ? "" : "s"} weren’t allowed.`;
          },
        }),

      leave: () => write(() => leaveMission(missionId).then(() => true as const), { reload: false }),

      deleteProject: () => write(() => deleteProject(missionId).then(() => true as const), { forbidden: "Only a project owner can delete it.", reload: false }),
    };
  }, [write, missionId, plan]);

  return { plan, query, busy, actions, reload };
}

export type ProjectActions = ReturnType<typeof useProject>["actions"];
