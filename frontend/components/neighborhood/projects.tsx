"use client";

import { type FormEvent, useState } from "react";

import {
  ErrorLine,
  fieldCls,
  ghostBtnCls,
  labelCls,
  PanelBlock,
  PanelHeader,
  quietBtnCls,
  SectionHead,
  SidePanel,
  textBtnCls,
  dangerBtnCls,
} from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { getErrorMessage } from "@/lib/errors";
import { crewLabel, memberName, messageTime, projectLine, windowLabel } from "@/lib/neighborhood";
import {
  useAddMissionTaskMutation,
  useAddMissionUpdateMutation,
  useCreateMissionMutation,
  useJoinMissionMutation,
  useLeaveMissionMutation,
  useMissionDetailQuery,
  usePatchMissionMutation,
} from "@/lib/queries";
import type { HomeNeighbor, MissionAsk, MissionDetail } from "@/lib/types";

function Bar({ pct }: { pct: number }) {
  const w = Math.min(100, Math.max(0, pct));
  return (
    <span className="block h-[2px] w-full bg-os-hairline" aria-hidden="true">
      <span className="block h-[2px] bg-os-accent" style={{ width: `${w === 0 ? 0 : Math.max(3, w)}%` }} />
    </span>
  );
}

/**
 * Projects (Missions in the API): something you're doing together, with a
 * goal and a weekly rhythm. Each row: crew, this week, the next step.
 */
export function ProjectsSection({
  missions,
  details,
  loading,
  myHandle,
  names,
  onOpen,
  onCreate,
}: {
  missions: MissionAsk[];
  details: Map<string, MissionDetail>;
  loading: boolean;
  myHandle: string | null;
  names: Map<string, string>;
  onOpen: (id: string) => void;
  onCreate: () => void;
}) {
  return (
    <section aria-labelledby="projects-heading" className="min-w-0 flex-1">
      <SectionHead
        id="projects-heading"
        label="Projects"
        trailing={
          <button type="button" onClick={onCreate} className={textBtnCls}>
            + Start a project
          </button>
        }
      />
      <p className="mb-1 mt-1 text-[0.8125rem] leading-relaxed text-os-faint">Something you&rsquo;re doing together, with a goal and a weekly rhythm.</p>
      {loading ? (
        <p className="os-hairline-top py-4 text-[0.9375rem] text-os-muted">Loading&hellip;</p>
      ) : missions.length === 0 ? (
        <p className="os-hairline-top py-4 text-[0.9375rem] text-os-muted">No projects yet. Start one with a neighbor &mdash; a walk every morning, a shared garden, a move.</p>
      ) : (
        <ul>
          {missions.map((m) => {
            const d = details.get(m.mission_id);
            const me = d?.members.find((x) => x.handle && x.handle === myHandle);
            const pct = me && me.window_days ? (100 * me.showed_up) / me.window_days : d?.overall_pct ?? 0;
            return (
              <li key={m.mission_id}>
                <button type="button" onClick={() => onOpen(m.mission_id)} className="os-focus os-hairline-top group flex w-full flex-col gap-2 py-3.5 text-left">
                  <span className="flex w-full items-baseline justify-between gap-4">
                    <span className="os-serif min-w-0 truncate text-[1.5rem] leading-tight text-white transition group-hover:text-os-accent sm:text-[1.625rem]">{m.title}</span>
                    <span className="shrink-0 text-[0.75rem] text-os-faint">{crewLabel(d, myHandle, names)}</span>
                  </span>
                  {d ? <Bar pct={pct} /> : <span className="block h-[2px] w-full bg-os-hairline" aria-hidden="true" />}
                  <span className="text-[0.875rem] text-os-muted">{d ? projectLine(d, myHandle) || m.my_commitment || " " : m.my_commitment || " "}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** A project, opened: this week per person, next steps, updates, edit / leave. */
export function ProjectPanel({
  missionId,
  myHandle,
  names,
  onClose,
}: {
  missionId: string;
  myHandle: string | null;
  names: Map<string, string>;
  onClose: () => void;
}) {
  const { data: mission, isLoading, isError } = useMissionDetailQuery(missionId);
  const join = useJoinMissionMutation();
  const leave = useLeaveMissionMutation();
  const addUpdate = useAddMissionUpdateMutation();
  const addTask = useAddMissionTaskMutation();
  const patch = usePatchMissionMutation();

  const [justLeft, setJustLeft] = useState(false);
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [step, setStep] = useState("");
  const [update, setUpdate] = useState("");
  const [kind, setKind] = useState<"note" | "progress" | "milestone">("note");
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [editError, setEditError] = useState("");

  const submitStep = async (e: FormEvent) => {
    e.preventDefault();
    const t = step.trim();
    if (!t || addTask.isPending) return;
    try {
      await addTask.mutateAsync({ id: missionId, data: { title: t } });
      setStep("");
      emitToast("Added to your tasks.", "success");
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };
  const submitUpdate = async (e: FormEvent) => {
    e.preventDefault();
    const t = update.trim();
    if (!t || addUpdate.isPending) return;
    try {
      await addUpdate.mutateAsync({ id: missionId, data: { kind, text: t } });
      setUpdate("");
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };
  const saveTitle = async () => {
    if (!mission) return;
    setEditError("");
    try {
      const res = await patch.mutateAsync({ id: missionId, data: { version: mission.version, title: title.trim() } });
      if (res.status === 200) {
        setEditing(false);
        emitToast("Project updated.", "success");
      } else setEditError(res.detail);
    } catch (err) {
      setEditError(getErrorMessage(err));
    }
  };

  const eyebrow = mission
    ? `Project · ${mission.my_role === "owner" ? "started by you" : "shared with you"}${mission.status !== "active" ? ` · ${mission.status}` : ""}`
    : "Project";
  const invited = mission?.my_status === "invited";
  const steps = (mission?.members ?? []).filter((m) => m.next_step);
  const updates = mission?.updates ?? [];

  return (
    <SidePanel label={mission?.title ?? "Project"} onClose={onClose}>
      <div className="flex flex-1 flex-col gap-6">
        {editing && mission ? (
          <div className="flex flex-col gap-3">
            <span className="text-[0.6875rem] font-semibold uppercase tracking-[0.16em] text-os-faint">{eyebrow}</span>
            <label className="sr-only" htmlFor="project-title">Project name</label>
            <input id="project-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} className={`${fieldCls} os-serif text-[1.75rem]`} data-autofocus />
            <div className="flex gap-4">
              <button type="button" className={ghostBtnCls} disabled={!title.trim() || patch.isPending} onClick={() => void saveTitle()}>
                {patch.isPending ? "Saving…" : "Save"}
              </button>
              <button type="button" className={quietBtnCls} onClick={() => { setEditing(false); setEditError(""); }}>
                Cancel
              </button>
            </div>
            {editError ? <ErrorLine>{editError}</ErrorLine> : null}
          </div>
        ) : (
          <PanelHeader onClose={onClose} eyebrow={eyebrow} title={mission?.title ?? (isLoading ? "…" : "Project")} />
        )}

        {isLoading && !mission ? (
          <p className="text-[0.9375rem] text-os-muted">Loading&hellip;</p>
        ) : isError || !mission ? (
          <p className="text-[0.9375rem] text-os-muted">This project isn&rsquo;t available right now.</p>
        ) : (
          <>
            {mission.description ? <p className="text-[1rem] leading-relaxed text-os-muted">{mission.description}</p> : null}
            {mission.my_commitment ? <p className="text-[0.875rem] text-os-faint">Your part: {mission.my_commitment}</p> : null}

            {invited ? (
              <PanelBlock label="You're asked to help">
                <p className="text-[0.9375rem] text-os-muted">Join to see how everyone&rsquo;s doing and share updates.</p>
                <button type="button" className={`${ghostBtnCls} self-start`} disabled={join.isPending} onClick={() => join.mutate({ id: missionId })}>
                  {join.isPending ? "Joining…" : "I can help"}
                </button>
              </PanelBlock>
            ) : (
              <PanelBlock label={windowLabel(mission.window_days)}>
                {mission.members.length === 0 ? <p className="text-[0.875rem] text-os-muted">No one&rsquo;s active in this project right now.</p> : null}
                {mission.members.map((m, i) => (
                  <div key={m.handle ?? `m-${i}`} className="flex flex-col gap-1.5 py-1">
                    <span className="flex items-baseline justify-between gap-3 text-[0.875rem]">
                      <span className="text-white">
                        {memberName(m.handle, myHandle, names)}
                        {m.is_creator ? <span className="text-os-faint"> &middot; started it</span> : null}
                      </span>
                      <span className="os-num text-os-faint">
                        {m.showed_up} of {m.window_days} days{m.streak > 1 ? ` · ${m.streak}-day streak` : ""}
                      </span>
                    </span>
                    <Bar pct={m.window_days ? (100 * m.showed_up) / m.window_days : 0} />
                  </div>
                ))}
              </PanelBlock>
            )}

            {!invited ? (
              <PanelBlock label="Next steps">
                {steps.length === 0 ? <p className="text-[0.875rem] text-os-faint">No next steps yet.</p> : null}
                {steps.map((m, i) => (
                  <p key={`${m.handle}-${i}`} className="flex items-baseline justify-between gap-3 py-1 text-[0.9375rem] text-white">
                    <span>{m.next_step}</span>
                    <span className="shrink-0 text-[0.75rem] text-os-faint">{memberName(m.handle, myHandle, names)}</span>
                  </p>
                ))}
                <form onSubmit={submitStep} className="flex items-center gap-3 border-b border-os-ring focus-within:border-os-accent">
                  <label htmlFor="project-step" className="sr-only">Add a next step</label>
                  <input id="project-step" value={step} onChange={(e) => setStep(e.target.value)} maxLength={256} placeholder="Add a next step…" className="min-h-[44px] min-w-0 flex-1 bg-transparent text-[0.9375rem] text-os-ink outline-none placeholder:text-os-faint focus-visible:shadow-none focus-visible:outline-none" />
                  <button type="submit" className={textBtnCls} disabled={!step.trim() || addTask.isPending}>
                    Add
                  </button>
                </form>
                <p className="text-[0.75rem] text-os-faint">Your next step goes on your own task list.</p>
              </PanelBlock>
            ) : null}

            {!invited ? (
              <PanelBlock label="Updates">
                {updates.length === 0 ? <p className="text-[0.875rem] text-os-faint">No updates yet. Share how it&rsquo;s going.</p> : null}
                <ul className="flex flex-col">
                  {updates.slice(0, 20).map((u) => (
                    <li key={u.id} className="flex flex-col gap-0.5 py-2">
                      <span className="text-[0.9375rem] leading-relaxed text-os-ink">{u.text}</span>
                      <span className="text-[0.75rem] text-os-faint">
                        {u.author_name}
                        {u.kind && u.kind !== "note" && u.kind !== "task_added" && u.kind !== "task_completed" ? ` · ${u.kind}` : ""} &middot; {messageTime(u.created_at)}
                      </span>
                    </li>
                  ))}
                </ul>
                <form onSubmit={submitUpdate} className="flex flex-col gap-2">
                  <div className="flex items-center gap-3 border-b border-os-ring focus-within:border-os-accent">
                    <label htmlFor="project-update" className="sr-only">Share an update</label>
                    <input id="project-update" value={update} onChange={(e) => setUpdate(e.target.value)} placeholder="Share an update…" className="min-h-[44px] min-w-0 flex-1 bg-transparent text-[0.9375rem] text-os-ink outline-none placeholder:text-os-faint focus-visible:shadow-none focus-visible:outline-none" />
                    <button type="submit" className={textBtnCls} disabled={!update.trim() || addUpdate.isPending}>
                      Post
                    </button>
                  </div>
                  <div className="flex gap-4 text-[0.75rem]" role="radiogroup" aria-label="Kind of update">
                    {(["note", "progress", "milestone"] as const).map((k) => (
                      <button key={k} type="button" role="radio" aria-checked={kind === k} onClick={() => setKind(k)} className={`os-focus min-h-[32px] rounded capitalize ${kind === k ? "text-os-accent" : "text-os-faint hover:text-os-ink"}`}>
                        {k}
                      </button>
                    ))}
                  </div>
                </form>
              </PanelBlock>
            ) : null}

            <div className="mt-auto flex flex-wrap items-center gap-x-6 gap-y-2 pt-4">
              {!invited && !editing ? (
                <button type="button" className={quietBtnCls} onClick={() => { setTitle(mission.title); setEditing(true); }}>
                  Edit project
                </button>
              ) : null}
              {justLeft ? (
                <>
                  <span className="text-[0.8125rem] text-os-muted">You left this project.</span>
                  <button type="button" className={textBtnCls} disabled={join.isPending} onClick={() => join.mutate({ id: missionId }, { onSuccess: () => { setJustLeft(false); emitToast("Back in.", "success"); } })}>
                    {join.isPending ? "Rejoining…" : "Rejoin"}
                  </button>
                </>
              ) : !invited && confirmLeave ? (
                <>
                  <span className="text-[0.8125rem] text-os-muted">Leave this project?</span>
                  <button type="button" className={dangerBtnCls} disabled={leave.isPending} onClick={() => leave.mutate(missionId, { onSuccess: () => { emitToast("Left the project.", "success"); setJustLeft(true); setConfirmLeave(false); } })}>
                    {leave.isPending ? "Leaving…" : "Leave"}
                  </button>
                  <button type="button" className={quietBtnCls} onClick={() => setConfirmLeave(false)}>
                    Stay
                  </button>
                </>
              ) : !invited ? (
                <button type="button" className={dangerBtnCls} onClick={() => setConfirmLeave(true)}>
                  Leave project
                </button>
              ) : null}
            </div>
          </>
        )}
      </div>
    </SidePanel>
  );
}

/** Start a project with a neighbor. */
export function CreateProjectPanel({ neighbors, onClose, onCreated }: { neighbors: HomeNeighbor[]; onClose: () => void; onCreated: (id: string) => void }) {
  const create = useCreateMissionMutation();
  const [friendshipId, setFriendshipId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [metric, setMetric] = useState("");
  const [value, setValue] = useState("");
  const [cadence, setCadence] = useState<"daily" | "weekly">("daily");
  const [targetDate, setTargetDate] = useState("");
  const [error, setError] = useState("");
  const sorted = [...neighbors].sort((a, b) => Number(b.in_my_sky) - Number(a.in_my_sky) || a.display_name.localeCompare(b.display_name));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!friendshipId || !title.trim()) return;
    setError("");
    const target: { metric?: string; cadence?: "daily" | "weekly"; value?: number } = { cadence };
    if (metric.trim()) target.metric = metric.trim();
    if (value.trim() && !Number.isNaN(Number(value))) target.value = Number(value);
    try {
      const res = await create.mutateAsync({ friendship_id: friendshipId, title: title.trim(), description: description.trim() || undefined, target, target_date: targetDate || undefined });
      emitToast("Project started.", "success");
      onCreated(res.mission_id);
    } catch (err) {
      setError(getErrorMessage(err));
    }
  };
  return (
    <SidePanel label="Start a project" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow="New project" title="Start a project" />
        {neighbors.length === 0 ? (
          <p className="text-[0.9375rem] text-os-muted">Invite someone first &mdash; you can start a project together once they&rsquo;re your neighbor.</p>
        ) : (
          <>
            <label className="flex flex-col gap-1">
              <span className={labelCls}>With</span>
              <select value={friendshipId} onChange={(e) => setFriendshipId(e.target.value)} className={`${fieldCls} bg-os-sky`} data-autofocus>
                <option value="">Choose a neighbor&hellip;</option>
                {sorted.map((n) => (
                  <option key={n.friendship_id} value={n.friendship_id}>
                    {n.display_name} (@{n.handle})
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className={labelCls}>What you&rsquo;re doing together</span>
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Walk every morning this month" maxLength={200} className={fieldCls} />
            </label>
            <label className="flex flex-col gap-1">
              <span className={labelCls}>Why (optional)</span>
              <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} maxLength={2000} className={`${fieldCls} resize-none py-2.5`} />
            </label>
            <div className="grid grid-cols-2 gap-5">
              <label className="flex flex-col gap-1">
                <span className={labelCls}>Rhythm</span>
                <select value={cadence} onChange={(e) => setCadence(e.target.value as "daily" | "weekly")} className={`${fieldCls} bg-os-sky`}>
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className={labelCls}>By (optional)</span>
                <input type="date" value={targetDate} onChange={(e) => setTargetDate(e.target.value)} className={`${fieldCls} [color-scheme:dark]`} />
              </label>
              <label className="flex flex-col gap-1">
                <span className={labelCls}>Measure (optional)</span>
                <input value={metric} onChange={(e) => setMetric(e.target.value)} placeholder="walks" maxLength={60} className={fieldCls} />
              </label>
              <label className="flex flex-col gap-1">
                <span className={labelCls}>Goal</span>
                <input type="number" inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} placeholder="5" className={fieldCls} />
              </label>
            </div>
            {error ? <ErrorLine>{error}</ErrorLine> : null}
            <div className="mt-auto flex items-center gap-4 pt-4">
              <button type="submit" className={ghostBtnCls} disabled={!friendshipId || !title.trim() || create.isPending}>
                {create.isPending ? "Starting…" : "Start the project"}
              </button>
              <button type="button" className={quietBtnCls} onClick={onClose}>
                Cancel
              </button>
            </div>
          </>
        )}
      </form>
    </SidePanel>
  );
}
