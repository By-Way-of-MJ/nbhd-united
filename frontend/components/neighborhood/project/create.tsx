"use client";

import clsx from "clsx";
import { type FormEvent, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { FieldLabel, Icon, PeoplePicker, rowHoverCls, sectionLabelCls } from "@/components/neighborhood/project/parts";
import { projectError } from "@/components/neighborhood/project/use-project";
import { dangerBtnCls, ErrorLine, fieldCls, ghostBtnCls, PanelHeader, quietBtnCls, SidePanel, textBtnCls } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { createMission, createProjectDependency, createProjectMilestone, createProjectStep, discardProjectDraft, publishProjectDraft } from "@/lib/api";
import { assistantSeed, dayShort, joinNames, localToday, parseDay, PROJECT_TEMPLATES, type ProjectTemplate, templateWrites } from "@/lib/project-plan";
import { useProjectDraftQuery } from "@/lib/queries";
import type { HomeNeighbor, ProjectDraftDetail } from "@/lib/types";

type Start = "blank" | "template" | "assistant";

const STARTS: { value: Start; title: string; detail: string }[] = [
  { value: "blank", title: "A blank plan", detail: "Add steps as you go." },
  { value: "template", title: "A starter template", detail: "Milestones and steps you can edit." },
  { value: "assistant", title: "Ask my assistant", detail: "It drafts a plan for you to review here." },
];

/** Lays a template into a fresh project: milestones, steps, and the links between them. */
async function applyTemplate(missionId: string, template: ProjectTemplate) {
  const ids = new Map<string, string>();
  for (const write of templateWrites(template, localToday())) {
    if (write.kind === "milestone") ids.set(write.key, String((await createProjectMilestone(missionId, write.body)).milestone_id));
    else if (write.kind === "step") ids.set(write.key, String((await createProjectStep(missionId, { ...write.body, milestone_id: ids.get(write.milestoneKey) ?? null })).step_id));
    else {
      const blocker = ids.get(write.blockerKey), blocked = ids.get(write.blockedKey);
      // A missing link isn't worth failing the whole project over.
      if (blocker && blocked) await createProjectDependency(missionId, blocker, blocked).catch(() => undefined);
    }
  }
}

/**
 * Start a project with any number of neighbors (or on your own): blank, from a
 * starter template, or by asking your assistant to draft the plan.
 */
export function CreateProjectV2Panel({ neighbors, onClose, onCreated }: { neighbors: HomeNeighbor[]; onClose: () => void; onCreated: (id: string) => void }) {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [start, setStart] = useState<Start>("blank");
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const sorted = [...neighbors].sort((a, b) => Number(b.in_my_sky) - Number(a.in_my_sky) || a.display_name.localeCompare(b.display_name));
  const template = PROJECT_TEMPLATES.find((t) => t.id === templateId) ?? null;
  const trimmed = title.trim();
  const canSave = !saving && !!trimmed && trimmed.length <= 120 && (start !== "template" || !!template);
  const seed = assistantSeed(trimmed || "a project", goal, sorted.filter((n) => picked.has(n.friendship_id)).map((n) => n.display_name));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSave || start === "assistant") return;
    setSaving(true);
    setError("");
    let missionId = "";
    try {
      const created = await createMission({ title: trimmed, description: goal.trim(), member_friendship_ids: sorted.map((n) => n.friendship_id).filter((id) => picked.has(id)) });
      missionId = String(created.mission_id ?? "");
      if (!missionId) {
        setError("The project may have been created. Refresh before trying again.");
        return;
      }
      if (start === "template" && template) await applyTemplate(missionId, template);
      void qc.invalidateQueries({ queryKey: ["missions"] });
      emitToast("Project started.", "success");
      onCreated(missionId);
    } catch (err) {
      void qc.invalidateQueries({ queryKey: ["missions"] });
      // The project exists but its starter plan stopped part-way: open it rather than lose it.
      if (missionId) {
        emitToast("Project started, but some of the starter plan didn’t save. You can add the rest.", "error");
        onCreated(missionId);
      } else setError(projectError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <SidePanel label="Start a project" onClose={onClose} wide>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-7">
        <PanelHeader onClose={onClose} eyebrow="New project" title="Start a project" />
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <FieldLabel htmlFor="new-project-title">What are we doing?</FieldLabel>
            <input id="new-project-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Fix up the shared garden" maxLength={160} className={`${fieldCls} os-serif text-[1.625rem]`} data-autofocus />
            {trimmed.length > 120 ? <ErrorLine>Keep it under 120 characters.</ErrorLine> : null}
          </div>
          <div className="flex flex-col gap-1">
            <FieldLabel htmlFor="new-project-goal">The goal, in a sentence (optional)</FieldLabel>
            <input id="new-project-goal" value={goal} onChange={(e) => setGoal(e.target.value)} maxLength={500} className={fieldCls} />
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <FieldLabel>Who&rsquo;s in</FieldLabel>
          {sorted.length === 0 ? (
            <p className="text-[0.875rem] leading-relaxed text-os-muted">Connect with a neighbor first — or start solo and invite people later.</p>
          ) : (
            <PeoplePicker
              label="Who’s in"
              options={sorted.map((n) => ({ id: n.friendship_id, name: n.display_name, color: `hsl(${n.avatar_hue} 78% 86%)` }))}
              picked={picked}
              onToggle={(id) =>
                setPicked((s) => {
                  const next = new Set(s);
                  if (next.has(id)) next.delete(id);
                  else next.add(id);
                  return next;
                })
              }
            />
          )}
          <p className="text-[0.8125rem] leading-relaxed text-os-faint">
            {picked.size ? "They get an invitation and decide. Creating a project doesn’t commit their time." : "Nobody picked: it starts as your own project, and you can add people later."}
          </p>
        </div>

        <div role="radiogroup" aria-label="Start from" className="flex flex-col border-b border-os-hairline">
          <span className={clsx(sectionLabelCls, "pb-2")}>Start from</span>
          {STARTS.map((s) => (
            <button key={s.value} type="button" role="radio" aria-checked={start === s.value} onClick={() => setStart(s.value)} className={clsx("os-focus flex min-h-[60px] w-full items-center gap-3 border-t border-os-hairline px-1 text-left", rowHoverCls)}>
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="text-[0.9375rem] text-os-ink">{s.title}</span>
                <span className="text-[0.8125rem] text-os-muted">{s.detail}</span>
              </span>
              {start === s.value ? (
                <span className="text-os-accent">
                  <Icon name="check" size={16} />
                </span>
              ) : null}
            </button>
          ))}
        </div>

        {start === "template" ? (
          <div role="radiogroup" aria-label="Starter template" className="grid grid-cols-2 gap-x-6 gap-y-1">
            {PROJECT_TEMPLATES.map((t) => {
              const on = t.id === templateId;
              const steps = t.milestones.reduce((n, m) => n + m.steps.length, 0);
              return (
                <button key={t.id} type="button" role="radio" aria-checked={on} onClick={() => setTemplateId(t.id)} className={clsx("os-focus flex min-h-[64px] flex-col justify-center gap-0.5 rounded-xl border px-3.5 py-2.5 text-left transition", on ? "border-white/60 bg-os-accent-soft" : "border-os-hairline hover:border-os-ring")}>
                  <span className={clsx("text-[0.9375rem]", on ? "text-white" : "text-os-ink")}>{t.title}</span>
                  <span className="text-[0.75rem] text-os-faint">
                    {t.milestones.length} milestones · {steps} steps
                  </span>
                </button>
              );
            })}
          </div>
        ) : null}

        {start === "assistant" ? (
          <div className="flex flex-col gap-2">
            <p className="text-[0.875rem] leading-relaxed text-os-muted">Send this to your assistant on iPhone, Telegram or LINE. Its draft shows up under Projects for you to review — nothing is shared until you start it.</p>
            <CopyRequest text={seed} />
          </div>
        ) : null}

        {error ? <ErrorLine>{error}</ErrorLine> : null}
        <div className="mt-auto flex items-center gap-4 pt-2">
          {start === "assistant" ? (
            <button type="button" className={ghostBtnCls} onClick={onClose}>
              Done
            </button>
          ) : (
            <button type="submit" className={ghostBtnCls} disabled={!canSave}>
              {saving ? "Starting…" : "Start the project"}
            </button>
          )}
          <button type="button" className={quietBtnCls} disabled={saving} onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </SidePanel>
  );
}

/** The request, ready to copy into a chat with your assistant. */
function CopyRequest({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const copy = async () => {
    setFailed(false);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2200);
    } catch {
      setFailed(true);
    }
  };
  return (
    <div className="flex flex-col items-start gap-1">
      <p className="w-full select-all rounded-xl border border-os-hairline px-3.5 py-3 text-[0.875rem] leading-relaxed text-os-ink">{text}</p>
      <button type="button" onClick={() => void copy()} className={textBtnCls} aria-live="polite">
        {copied ? "Copied" : "Copy the request"}
      </button>
      {failed ? <p className="text-[0.8125rem] text-os-muted">Couldn&rsquo;t copy automatically &mdash; select the text and copy it.</p> : null}
    </div>
  );
}

/** "You", "@sam", or "Anyone". */
function draftOwner(owner: string | null | undefined): string {
  if (!owner) return "Anyone";
  return owner.toLowerCase() === "me" ? "You" : owner.startsWith("@") ? owner : `@${owner}`;
}

function draftWhen(step: NonNullable<ProjectDraftDetail["payload"]["steps"]>[number]): string | null {
  const s = parseDay(step.start_date), e = parseDay(step.due_date);
  if (s !== null && e !== null) return s === e ? dayShort(s) : `${dayShort(s)} – ${dayShort(e)}`;
  if (s !== null) return dayShort(s);
  return e !== null ? `by ${dayShort(e)}` : null;
}

/**
 * Review a private starter plan your assistant drafted, then start it (or not).
 * Nothing is shared until "Start project"; the people it suggests are then asked.
 */
export function DraftReviewPanel({ draftId, onClose, onStarted }: { draftId: string; onClose: () => void; onStarted: (missionId: string) => void }) {
  const qc = useQueryClient();
  const { data: draft, isLoading, error: loadError } = useProjectDraftQuery(draftId);
  const [working, setWorking] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [error, setError] = useState("");
  const payload = draft?.payload;
  const steps = payload?.steps ?? [];
  const milestones = payload?.milestones ?? [];
  const groups = [
    ...milestones.map((m) => ({ title: [m.title ?? "Milestone", parseDay(m.target_date) !== null ? dayShort(parseDay(m.target_date) as number) : null].filter(Boolean).join(" · "), steps: steps.filter((s) => s.milestone_key === m.key) })),
    { title: "Other steps", steps: steps.filter((s) => !milestones.some((m) => m.key === s.milestone_key)) },
  ].filter((g) => g.steps.length);

  const start = async () => {
    setWorking(true);
    setError("");
    try {
      const result = await publishProjectDraft(draftId);
      void qc.invalidateQueries({ queryKey: ["project-drafts"] });
      void qc.invalidateQueries({ queryKey: ["missions"] });
      if (!result.mission_id) return setError("The project may have started. Refresh before trying again.");
      emitToast("Project started.", "success");
      onStarted(String(result.mission_id));
    } catch (err) {
      setError(projectError(err));
    } finally {
      setWorking(false);
    }
  };
  const discard = async () => {
    setWorking(true);
    setError("");
    try {
      await discardProjectDraft(draftId);
      void qc.invalidateQueries({ queryKey: ["project-drafts"] });
      emitToast("Draft discarded.", "success");
      onClose();
    } catch (err) {
      setError(projectError(err));
      setWorking(false);
    }
  };

  return (
    <SidePanel label="Draft from your assistant" onClose={onClose} wide>
      <div className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow="Draft from your assistant" title={payload?.title ?? (isLoading ? "…" : "Draft")} />
        {isLoading ? (
          <p className="text-[0.9375rem] text-os-muted">Loading&hellip;</p>
        ) : !payload ? (
          <p className="text-[0.9375rem] text-os-muted">{loadError ? "This draft isn’t available anymore." : "Couldn’t read this draft."}</p>
        ) : (
          <>
            {payload.goal ? <p className="os-serif text-[1.375rem] leading-snug text-os-ink">{payload.goal}</p> : null}
            <p className="text-[0.8125rem] leading-relaxed text-os-muted">Only you can see this. Nothing is shared until you start it — then the people it suggests are asked, and they decide.</p>
            {groups.map((g) => (
              <section key={g.title} aria-label={g.title}>
                <h3 className={clsx(sectionLabelCls, "pb-2")}>{g.title}</h3>
                {g.steps.map((s) => {
                  const waits = (s.depends_on ?? []).map((key) => steps.find((x) => x.key === key)?.title).filter((t): t is string => !!t);
                  return (
                    <div key={s.key} className="flex flex-col gap-0.5 border-t border-os-hairline py-3">
                      <span className="text-[0.9375rem] text-os-ink">{s.title ?? "Step"}</span>
                      <span className="text-[0.8125rem] text-os-muted">{[draftOwner(s.owner), draftWhen(s)].filter(Boolean).join(" · ")}</span>
                      {waits.length ? <span className="text-[0.75rem] text-os-faint">Waits for: {joinNames(waits)}</span> : null}
                    </div>
                  );
                })}
              </section>
            ))}
            {error ? <ErrorLine>{error}</ErrorLine> : null}
            <div className="mt-auto flex flex-wrap items-center gap-x-5 gap-y-2 pt-2">
              {confirmDiscard ? (
                <>
                  <span className="text-[0.875rem] text-os-muted">Discard this draft?</span>
                  <button type="button" className={dangerBtnCls} disabled={working} onClick={() => void discard()}>
                    Discard draft
                  </button>
                  <button type="button" className={quietBtnCls} disabled={working} onClick={() => setConfirmDiscard(false)}>
                    Keep it
                  </button>
                </>
              ) : (
                <>
                  <button type="button" className={ghostBtnCls} disabled={working} onClick={() => void start()}>
                    {working ? "Starting…" : "Start project"}
                  </button>
                  <button type="button" className={dangerBtnCls} disabled={working} onClick={() => setConfirmDiscard(true)}>
                    Discard
                  </button>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </SidePanel>
  );
}
