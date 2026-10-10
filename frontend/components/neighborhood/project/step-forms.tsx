"use client";

import clsx from "clsx";
import { type FormEvent, useState } from "react";

import { GhostCircleButton } from "@/components/open-sky/primitives";
import { DateField, FieldLabel, Icon, memberColor, PeoplePicker, rowHoverCls } from "@/components/neighborhood/project/parts";
import type { ProjectActions, StepDraft } from "@/components/neighborhood/project/use-project";
import { ErrorLine, fieldCls, ghostBtnCls, PanelHeader, quietBtnCls, SidePanel } from "@/components/neighborhood/ui";
import {
  askCandidates,
  askNote,
  blockerChoices,
  blockersOf,
  dayIso,
  findMilestone,
  isClosed,
  joinNames,
  orderedSteps,
  ownerLabel,
  parseDay,
  people,
  type PlanStep,
  type ProjectPlan,
  whenLabel,
} from "@/lib/project-plan";

const toggled = (set: Set<string>, id: string) => {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
};

const personOptions = (plan: ProjectPlan, members = people(plan)) =>
  members.map((m) => ({ id: m.id, name: m.id === plan.myMembershipId ? "Me" : m.displayName, initial: m.id === plan.myMembershipId ? "Y" : undefined, color: memberColor(plan, m), sub: m.status === "invited" ? "invited" : undefined }));

/**
 * Add (or edit) a step: what, who, start and finish, milestone, and which
 * steps have to finish first. Asking someone never adds to their journal until
 * they say yes.
 */
export function StepFormPanel({ plan, editing, actions, onClose, onAdded }: { plan: ProjectPlan; editing?: PlanStep; actions: ProjectActions; onClose: () => void; onAdded?: (stepId: string) => void }) {
  const [title, setTitle] = useState(editing?.title ?? "");
  const [description, setDescription] = useState(editing?.description ?? "");
  const [start, setStart] = useState(editing && editing.start !== null ? dayIso(editing.start) : "");
  const [due, setDue] = useState(editing && editing.due !== null ? dayIso(editing.due) : "");
  const [milestoneId, setMilestoneId] = useState(editing?.milestoneId ?? "");
  const [who, setWho] = useState<Set<string>>(new Set());
  const original = editing ? blockersOf(plan, editing.id).map((s) => s.id) : [];
  const [waitsOn, setWaitsOn] = useState<Set<string>>(new Set(original));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const trimmed = title.trim();
  const datesValid = !(start && due) || start <= due;
  const canSave = !saving && !!trimmed && trimmed.length <= 120 && datesValid;
  const choices = blockerChoices(plan, editing?.id ?? null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setError("");
    const draft: StepDraft = { title: trimmed, description, start: parseDay(start), due: parseDay(due), milestoneId: milestoneId || null };
    if (editing) {
      const changed = waitsOn.size !== original.length || original.some((id) => !waitsOn.has(id));
      const result = await actions.updateStep(editing, draft, changed ? waitsOn : null);
      setSaving(false);
      if (result.ok) onClose();
      else setError(result.error);
      return;
    }
    const order = orderedSteps(plan).map((s) => s.id);
    const result = await actions.addStep(draft, order.filter((id) => waitsOn.has(id)), people(plan).map((m) => m.id).filter((id) => who.has(id)));
    setSaving(false);
    if (result.ok) {
      onAdded?.(result.value);
      onClose();
    } else setError(result.error);
  };

  return (
    <SidePanel label={editing ? "Edit step" : "Add a step"} onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow={editing ? "Edit step" : "New step"} title={editing ? "Edit this step" : "Add a step"} />
        <div className="flex flex-col gap-1">
          <FieldLabel htmlFor="step-title">What needs doing</FieldLabel>
          <input id="step-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Buy timber and soil" maxLength={160} className={`${fieldCls} os-serif text-[1.625rem]`} data-autofocus />
          {trimmed.length > 120 ? <ErrorLine>Keep it under 120 characters.</ErrorLine> : null}
        </div>

        {!editing ? (
          <div className="flex flex-col gap-3">
            <FieldLabel>Who&rsquo;s doing it</FieldLabel>
            <PeoplePicker label="Who’s doing it" options={personOptions(plan)} picked={who} onToggle={(id) => setWho((s) => toggled(s, id))} />
            <p className="text-[0.8125rem] leading-relaxed text-os-muted">{askNote(plan, [...who])}</p>
          </div>
        ) : null}

        <div className="flex flex-col border-b border-os-hairline">
          <DateField id="step-start" label="Start" value={start} onChange={setStart} />
          <DateField id="step-due" label="Finish" value={due} onChange={setDue} min={start || undefined} />
          {plan.milestones.length ? (
            <div className="flex min-h-[52px] items-center justify-between gap-4 border-t border-os-hairline">
              <label htmlFor="step-milestone" className="text-[0.9375rem] text-os-ink">
                Milestone
              </label>
              <select id="step-milestone" value={milestoneId} onChange={(e) => setMilestoneId(e.target.value)} className="os-focus min-h-[40px] max-w-[60%] rounded border-0 bg-os-sky text-right text-[0.875rem] text-os-ink outline-none">
                <option value="">None</option>
                {plan.milestones.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.title}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
        </div>
        {!datesValid ? <ErrorLine>Finish has to be on or after start.</ErrorLine> : null}

        {choices.length ? (
          <fieldset className="flex flex-col">
            <legend className="pb-2">
              <FieldLabel>Can&rsquo;t start until these are done</FieldLabel>
            </legend>
            {choices.map((step) => {
              const on = waitsOn.has(step.id);
              return (
                <label key={step.id} className={clsx("flex min-h-[48px] cursor-pointer items-center gap-3 border-t border-os-hairline px-1", rowHoverCls)}>
                  <input type="checkbox" checked={on} onChange={() => setWaitsOn((s) => toggled(s, step.id))} className="peer sr-only" />
                  <span aria-hidden="true" className={clsx("flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full border-[1.5px] peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-os-accent", on ? "border-os-accent text-os-accent" : "border-os-ring text-transparent")}>
                    <Icon name="check" size={13} />
                  </span>
                  <span className={clsx("min-w-0 flex-1 text-[0.9375rem]", isClosed(step) ? "text-os-faint" : "text-os-ink")}>{step.title}</span>
                  <span className="shrink-0 text-[0.75rem] text-os-faint">{ownerLabel(plan, step)}</span>
                </label>
              );
            })}
          </fieldset>
        ) : null}

        <div className="flex flex-col gap-1">
          <FieldLabel htmlFor="step-details">Details (optional)</FieldLabel>
          <textarea id="step-details" value={description} onChange={(e) => setDescription(e.target.value)} rows={3} maxLength={500} className={`${fieldCls} resize-none py-2.5`} />
        </div>

        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <p className="border-t border-os-hairline pt-3.5 text-[0.8125rem] leading-relaxed text-os-faint">Everyone in the project sees this step. It goes into someone&rsquo;s own journal only after they say yes.</p>
        <div className="mt-auto flex items-center gap-4 pt-2">
          <button type="submit" className={ghostBtnCls} disabled={!canSave}>
            {saving ? "Saving…" : editing ? "Save" : "Add step"}
          </button>
          <button type="button" className={quietBtnCls} disabled={saving} onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </SidePanel>
  );
}

/** Ask one or more members to take a step (they answer in their own app). */
export function AskSomeonePanel({ plan, step, actions, onClose }: { plan: ProjectPlan; step: PlanStep; actions: ProjectActions; onClose: () => void }) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const candidates = askCandidates(plan, step);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!picked.size || saving) return;
    setSaving(true);
    setError("");
    const result = await actions.ask(step.id, candidates.map((m) => m.id).filter((id) => picked.has(id)));
    setSaving(false);
    if (result.ok) onClose();
    else setError(result.error);
  };

  return (
    <SidePanel label="Ask someone" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow="Ask someone to take" title={step.title} />
        {candidates.length === 0 ? (
          <p className="text-[0.9375rem] leading-relaxed text-os-muted">Everyone in the project already has it or was asked.</p>
        ) : (
          <PeoplePicker label="Who to ask" options={personOptions(plan, candidates)} picked={picked} onToggle={(id) => setPicked((s) => toggled(s, id))} />
        )}
        <p className="text-[0.8125rem] leading-relaxed text-os-faint">They&rsquo;ll get a notification and decide. Nothing goes into their journal unless they say yes.</p>
        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <div className="mt-auto flex items-center gap-4 pt-2">
          <button type="submit" className={ghostBtnCls} disabled={!picked.size || saving}>
            {saving ? "Asking…" : "Ask"}
          </button>
          <button type="button" className={quietBtnCls} disabled={saving} onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </SidePanel>
  );
}

/** The asked member's answer: Yes · Other dates · Smaller part · Not this time. */
export function AnswerAskPanel({ plan, step, actions, onClose }: { plan: ProjectPlan; step: PlanStep; actions: ProjectActions; onClose: () => void }) {
  const [mode, setMode] = useState<"choose" | "dates" | "smaller">("choose");
  const [start, setStart] = useState(step.start !== null ? dayIso(step.start) : "");
  const [due, setDue] = useState(step.due !== null ? dayIso(step.due) : "");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const milestone = findMilestone(plan, step.milestoneId);
  const waits = blockersOf(plan, step.id).filter((s) => !isClosed(s)).map((s) => s.title);
  const datesValid = (!!start || !!due) && !(start && due && start > due);

  const answer = async (body: Parameters<ProjectActions["respond"]>[1]) => {
    if (saving) return;
    setSaving(true);
    setError("");
    const result = await actions.respond(step.id, body);
    setSaving(false);
    if (result.ok) onClose();
    else setError(result.error);
  };
  const rows: [string, string][] = [["Project", plan.title], ["When", whenLabel(step) ?? "No dates yet"]];
  if (milestone) rows.push(["Part of", milestone.title]);
  if (waits.length) rows.push(["Starts after", joinNames(waits)]);

  return (
    <SidePanel label="Can you take this?" onClose={onClose}>
      <div className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow="Can you take this?" title={step.title} />
        <dl className="flex flex-col border-b border-os-hairline">
          {rows.map(([label, value]) => (
            <div key={label} className="flex min-h-[48px] items-baseline justify-between gap-4 border-t border-os-hairline py-3">
              <dt className="text-[0.9375rem] text-os-ink">{label}</dt>
              <dd className="text-right text-[0.875rem] text-os-muted">{value}</dd>
            </div>
          ))}
        </dl>
        {step.description ? <p className="whitespace-pre-wrap text-[0.9375rem] leading-relaxed text-os-muted">{step.description}</p> : null}

        {mode === "choose" ? (
          <div className="grid grid-cols-4 gap-2">
            <GhostCircleButton label="Yes" icon={<Icon name="check" />} onClick={() => void answer({ answer: "yes" })} disabled={saving} />
            <GhostCircleButton label="Other dates" icon={<Icon name="calendar" />} onClick={() => setMode("dates")} disabled={saving} />
            <GhostCircleButton label="Smaller part" icon={<Icon name="half" />} onClick={() => setMode("smaller")} disabled={saving} />
            <GhostCircleButton label="Not this time" icon={<Icon name="x" />} onClick={() => void answer({ answer: "no" })} disabled={saving} />
          </div>
        ) : mode === "dates" ? (
          <div className="flex flex-col">
            <FieldLabel>Dates that work for you</FieldLabel>
            <div className="mt-2 flex flex-col border-b border-os-hairline">
              <DateField id="ask-start" label="Start" value={start} onChange={setStart} />
              <DateField id="ask-due" label="Finish" value={due} onChange={setDue} min={start || undefined} />
            </div>
            {start && due && start > due ? <ErrorLine>Finish has to be on or after start.</ErrorLine> : null}
            <div className="flex items-center gap-4 pt-3">
              <button type="button" className={ghostBtnCls} disabled={!datesValid || saving} onClick={() => void answer({ answer: "dates", ...(start ? { start } : {}), ...(due ? { due } : {}) })}>
                {saving ? "Sending…" : "Send dates"}
              </button>
              <button type="button" className={quietBtnCls} disabled={saving} onClick={() => setMode("choose")}>
                Back
              </button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <FieldLabel htmlFor="ask-note">What part could you take?</FieldLabel>
            <textarea id="ask-note" value={note} onChange={(e) => setNote(e.target.value)} rows={3} placeholder="e.g. I can pick up the soil, not the timber" className={`${fieldCls} resize-none py-2.5`} data-autofocus />
            <span className={clsx("os-num text-[0.6875rem]", note.length > 200 ? "text-os-danger" : "text-os-faint")}>{note.length}/200</span>
            <div className="flex items-center gap-4 pt-1">
              <button type="button" className={ghostBtnCls} disabled={!note.trim() || note.length > 200 || saving} onClick={() => void answer({ answer: "smaller", note: note.trim() })}>
                {saving ? "Sending…" : "Send"}
              </button>
              <button type="button" className={quietBtnCls} disabled={saving} onClick={() => setMode("choose")}>
                Back
              </button>
            </div>
          </div>
        )}

        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <p className="text-[0.8125rem] leading-relaxed text-os-faint">An invitation, not an obligation. Only your answer is shared — never your reasons.</p>
        <div className="mt-auto pt-2">
          <button type="button" className={quietBtnCls} disabled={saving} onClick={onClose}>
            Decide later
          </button>
        </div>
      </div>
    </SidePanel>
  );
}
