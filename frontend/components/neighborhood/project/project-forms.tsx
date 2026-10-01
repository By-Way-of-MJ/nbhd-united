"use client";

import clsx from "clsx";
import { type FormEvent, useState } from "react";

import { DateField, FieldLabel, Icon, PersonRing, rowHoverCls } from "@/components/neighborhood/project/parts";
import type { ProjectActions } from "@/components/neighborhood/project/use-project";
import { dangerBtnCls, ErrorLine, fieldCls, ghostBtnCls, PanelHeader, quietBtnCls, SidePanel } from "@/components/neighborhood/ui";
import { dayIso, isActiveMember, isInvitedMember, leaveMessage, myMember, parseDay, type PlanMilestone, type ProjectPlan } from "@/lib/project-plan";
import { useActiveGoalsQuery, useNeighborhoodHomeQuery } from "@/lib/queries";

/**
 * Add a milestone, or rename / re-date / remove one. Removing a milestone keeps
 * its steps — they move to "Other steps".
 */
export function MilestonePanel({ plan, editing, actions, onClose }: { plan: ProjectPlan; editing?: PlanMilestone; actions: ProjectActions; onClose: () => void }) {
  const [title, setTitle] = useState(editing?.title ?? "");
  const [target, setTarget] = useState(editing && editing.target !== null ? dayIso(editing.target) : "");
  const [saving, setSaving] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [error, setError] = useState("");
  const trimmed = title.trim();
  const full = !editing && plan.milestones.length >= 8;
  const canSave = !saving && !!trimmed && trimmed.length <= 120 && !full;

  const finish = (result: { ok: boolean; error?: string }) => {
    setSaving(false);
    if (result.ok) onClose();
    else setError(result.error ?? "");
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setError("");
    finish(editing ? await actions.updateMilestone(editing, trimmed, parseDay(target)) : await actions.addMilestone(trimmed, parseDay(target)));
  };
  const remove = async () => {
    if (!editing) return;
    setSaving(true);
    setError("");
    finish(await actions.deleteMilestone(editing));
  };

  return (
    <SidePanel label={editing ? "Milestone" : "Add a milestone"} onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow={editing ? "Milestone" : "New milestone"} title={editing ? "Edit this milestone" : "Add a milestone"} />
        <div className="flex flex-col gap-1">
          <FieldLabel htmlFor="milestone-title">A point you&rsquo;ll reach</FieldLabel>
          <input id="milestone-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Beds built" maxLength={160} className={`${fieldCls} os-serif text-[1.625rem]`} data-autofocus />
          {trimmed.length > 120 ? <ErrorLine>Keep it under 120 characters.</ErrorLine> : null}
        </div>
        <div className="border-b border-os-hairline">
          <DateField id="milestone-target" label="Target date" value={target} onChange={setTarget} />
        </div>
        {full ? <p className="text-[0.8125rem] text-os-attn">A project can have up to 8 milestones.</p> : null}
        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <p className="text-[0.8125rem] leading-relaxed text-os-faint">To put a step under this milestone, open the step and choose Edit.</p>

        <div className="mt-auto flex flex-col gap-3 pt-2">
          <div className="flex items-center gap-4">
            <button type="submit" className={ghostBtnCls} disabled={!canSave}>
              {saving ? "Saving…" : editing ? "Save" : "Add milestone"}
            </button>
            <button type="button" className={quietBtnCls} disabled={saving} onClick={onClose}>
              Cancel
            </button>
          </div>
          {editing ? (
            confirmRemove ? (
              <div className="flex flex-col gap-1.5 border-t border-os-hairline pt-3">
                <p className="text-[0.9375rem] text-os-ink">Remove this milestone?</p>
                <p className="text-[0.8125rem] text-os-muted">Its steps stay in the project, under &ldquo;Other steps&rdquo;.</p>
                <div className="flex gap-5">
                  <button type="button" className={dangerBtnCls} disabled={saving} onClick={() => void remove()}>
                    Remove milestone
                  </button>
                  <button type="button" className={quietBtnCls} disabled={saving} onClick={() => setConfirmRemove(false)}>
                    Keep it
                  </button>
                </div>
              </div>
            ) : (
              <button type="button" className={`${dangerBtnCls} self-start`} disabled={saving} onClick={() => setConfirmRemove(true)}>
                Remove milestone
              </button>
            )
          ) : null}
        </div>
      </form>
    </SidePanel>
  );
}

/** Rename the project (owners). */
export function RenamePanel({ plan, actions, onClose }: { plan: ProjectPlan; actions: ProjectActions; onClose: () => void }) {
  const [title, setTitle] = useState(plan.title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const trimmed = title.trim();
  const canSave = !saving && !!trimmed && trimmed.length <= 120 && trimmed !== plan.title;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setError("");
    const result = await actions.rename(trimmed);
    setSaving(false);
    if (result.ok) onClose();
    else setError(result.error);
  };
  return (
    <SidePanel label="Rename project" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow="Project name" title="Rename the project" />
        <div className="flex flex-col gap-1">
          <FieldLabel htmlFor="project-name">Name</FieldLabel>
          <input id="project-name" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={160} className={`${fieldCls} os-serif text-[1.625rem]`} data-autofocus />
          {trimmed.length > 120 ? <ErrorLine>Keep it under 120 characters.</ErrorLine> : null}
        </div>
        <p className="text-[0.8125rem] text-os-faint">Everyone in the project sees the new name.</p>
        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <div className="mt-auto flex items-center gap-4 pt-2">
          <button type="submit" className={ghostBtnCls} disabled={!canSave}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button type="button" className={quietBtnCls} disabled={saving} onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </SidePanel>
  );
}

/**
 * Invite more neighbors into a project I started. Each gets an invitation and
 * still decides whether to join.
 */
export function AddPeoplePanel({ plan, actions, onClose }: { plan: ProjectPlan; actions: ProjectActions; onClose: () => void }) {
  const home = useNeighborhoodHomeQuery();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const neighbors = home.data?.neighbors ?? [];
  // Neighbors not already in (or invited to) the project.
  const taken = new Set(plan.members.filter((m) => isActiveMember(m) || isInvitedMember(m)).map((m) => m.handle?.toLowerCase()).filter(Boolean));
  const choices = neighbors.filter((n) => !taken.has(n.handle.toLowerCase())).sort((a, b) => a.display_name.localeCompare(b.display_name));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!picked.size || saving) return;
    setSaving(true);
    setError("");
    const result = await actions.addPeople(choices.filter((n) => picked.has(n.friendship_id)).map((n) => ({ friendshipId: n.friendship_id, name: n.display_name })));
    setSaving(false);
    if (result.ok) onClose();
    else setError(result.error);
  };

  return (
    <SidePanel label="Add people" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-5">
        <PanelHeader onClose={onClose} eyebrow="Add people" title="Who else is in?" />
        <p className="text-[0.9375rem] leading-relaxed text-os-muted">They get an invitation to &ldquo;{plan.title}&rdquo; and decide whether to join.</p>
        {home.isLoading ? (
          <p className="text-[0.9375rem] text-os-muted">Loading&hellip;</p>
        ) : choices.length === 0 ? (
          <p className="text-[0.875rem] leading-relaxed text-os-faint">
            {neighbors.length === 0 ? "Connect with a neighbor first, then you can invite them here." : "Everyone you’re connected with is already in this project."}
          </p>
        ) : (
          <ul className="border-b border-os-hairline">
            {choices.map((n) => {
              const on = picked.has(n.friendship_id);
              return (
                <li key={n.friendship_id}>
                  <button
                    type="button"
                    aria-pressed={on}
                    onClick={() =>
                      setPicked((s) => {
                        const next = new Set(s);
                        if (on) next.delete(n.friendship_id);
                        else next.add(n.friendship_id);
                        return next;
                      })
                    }
                    className={clsx("os-focus flex min-h-[60px] w-full items-center gap-3.5 border-t border-os-hairline px-1 text-left", rowHoverCls)}
                  >
                    <PersonRing name={n.display_name} color={`hsl(${n.avatar_hue} 78% 86%)`} selected={on} size={40} />
                    <span className="min-w-0 flex-1 truncate text-[0.9375rem] text-os-ink">{n.display_name}</span>
                    {on ? (
                      <span className="text-os-accent">
                        <Icon name="check" size={16} />
                      </span>
                    ) : null}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <div className="mt-auto flex items-center gap-4 pt-2">
          <button type="submit" className={ghostBtnCls} disabled={!picked.size || saving}>
            {saving ? "Inviting…" : "Invite"}
          </button>
          <button type="button" className={quietBtnCls} disabled={saving} onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </SidePanel>
  );
}

/** Pick one of my own Horizons goals for this project (or none). Private to me. */
export function GoalLinkPanel({ plan, actions, onClose }: { plan: ProjectPlan; actions: ProjectActions; onClose: () => void }) {
  const goals = useActiveGoalsQuery(true);
  const current = myMember(plan)?.linkedGoalId ?? null;
  const [saving, setSaving] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState("");
  const choose = async (goalId: string | null) => {
    if (saving !== undefined) return;
    if (goalId === current) return onClose();
    setSaving(goalId);
    setError("");
    const result = await actions.setLinkedGoal(goalId);
    setSaving(undefined);
    if (result.ok) onClose();
    else setError(result.error);
  };
  const row = (id: string | null, title: string) => (
    <li key={id ?? "none"}>
      <button type="button" aria-pressed={current === id} disabled={saving !== undefined} onClick={() => void choose(id)} className={clsx("os-focus flex min-h-[52px] w-full items-center justify-between gap-3 border-t border-os-hairline px-1 text-left disabled:opacity-60", rowHoverCls)}>
        <span className={clsx("min-w-0 truncate text-[0.9375rem]", id === null ? "text-os-muted" : "text-os-ink")}>{title}</span>
        {current === id ? (
          <span className="text-os-accent">
            <Icon name="check" size={16} />
          </span>
        ) : saving === id ? (
          <span className="text-[0.75rem] text-os-faint">Saving&hellip;</span>
        ) : null}
      </button>
    </li>
  );
  return (
    <SidePanel label="Part of my goal" onClose={onClose}>
      <div className="flex flex-1 flex-col gap-5">
        <PanelHeader onClose={onClose} eyebrow="Part of my goal" title="Link to one of my goals" />
        <p className="text-[0.9375rem] leading-relaxed text-os-muted">Steps you take in this project show up under the goal in Horizons. Only you see this.</p>
        {goals.isLoading ? (
          <p className="text-[0.9375rem] text-os-muted">Loading&hellip;</p>
        ) : (
          <ul className="border-b border-os-hairline">
            {(goals.data ?? []).map((g) => row(g.id, g.title))}
            {row(null, "None")}
          </ul>
        )}
        {!goals.isLoading && (goals.data ?? []).length === 0 ? <p className="text-[0.8125rem] text-os-faint">You don&rsquo;t have any active goals in Horizons yet.</p> : null}
        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <div className="mt-auto pt-2">
          <button type="button" className={quietBtnCls} onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </SidePanel>
  );
}

/** Leave the project, or (owners) delete it for everyone — said plainly first. */
export function ConfirmExitPanel({ plan, kind, actions, onClose, onGone }: { plan: ProjectPlan; kind: "leave" | "delete"; actions: ProjectActions; onClose: () => void; onGone: (message: string) => void }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const leaving = kind === "leave";
  const confirm = async () => {
    setSaving(true);
    setError("");
    const result = leaving ? await actions.leave() : await actions.deleteProject();
    setSaving(false);
    if (result.ok) onGone(leaving ? "You left the project." : "Project deleted.");
    else setError(result.error);
  };
  return (
    <SidePanel label={leaving ? "Leave project" : "Delete project"} onClose={onClose}>
      <div className="flex flex-1 flex-col gap-5">
        <PanelHeader onClose={onClose} eyebrow={plan.title} title={leaving ? "Leave this project?" : "Delete this project for everyone?"} />
        <p className="text-[0.9375rem] leading-relaxed text-os-muted">{leaving ? leaveMessage(plan) : "It disappears for everyone in it. Steps people already took stay in their own journals."}</p>
        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <div className="flex items-center gap-5">
          <button type="button" className={dangerBtnCls} disabled={saving} onClick={() => void confirm()} data-autofocus>
            {saving ? (leaving ? "Leaving…" : "Deleting…") : leaving ? "Leave project" : "Delete project"}
          </button>
          <button type="button" className={quietBtnCls} disabled={saving} onClick={onClose}>
            {leaving ? "Stay" : "Keep it"}
          </button>
        </div>
      </div>
    </SidePanel>
  );
}
