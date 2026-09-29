"use client";

import { useState } from "react";
import { groupOpenTasks, localMonday } from "@/lib/horizons-tasks";
import { useCompleteTaskMutation, useCreateTaskMutation, useReopenTaskMutation, useTasksQuery, useTaskGoalsQuery } from "@/lib/queries";
import type { HorizonsGoal, JournalTask } from "@/lib/types";

function TaskRow({ task, goal, dueSoon = false }: { task: JournalTask; goal?: string; dueSoon?: boolean }) {
  const complete = useCompleteTaskMutation();
  const reopen = useReopenTaskMutation();
  const [optimistic, setOptimistic] = useState<boolean | null>(null);
  const [error, setError] = useState(false);
  const done = optimistic ?? task.status === "done";
  const busy = complete.isPending || reopen.isPending;
  async function toggle() {
    const next = !done;
    setOptimistic(next); setError(false);
    try {
      await (next ? complete : reopen).mutateAsync(task.id);
    } catch { setOptimistic(null); setError(true); }
  }
  return (
    <li className="border-b border-os-hairline py-2">
      <div className="flex min-h-[44px] items-start gap-2 sm:gap-3">
        <label className="flex h-11 w-11 shrink-0 cursor-pointer items-center justify-center">
          <input type="checkbox" aria-label={`${done ? "Reopen" : "Complete"} ${task.title}`} checked={done} disabled={busy} onChange={() => void toggle()}
            className="h-[18px] w-[18px] cursor-pointer accent-os-done" />
        </label>
        <div className="min-w-0 flex-1 py-2.5">
          <p className={`break-words text-sm leading-relaxed ${done ? "text-os-done line-through" : "text-os-ink"}`}>{task.title}</p>
          {goal ? <p className="mt-1 text-xs text-os-faint">{goal}</p> : null}
          {error ? <p role="alert" className="mt-1 text-xs text-os-danger">Couldn’t save. Try the checkbox again.</p> : null}
        </div>
        {task.due_date ? <time dateTime={task.due_date} className={`shrink-0 pt-3 text-xs ${dueSoon && !done ? "text-os-accent" : "text-os-faint"}`}>
          {new Date(`${task.due_date}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
        </time> : null}
      </div>
    </li>
  );
}

export function TasksSection({ goals }: { goals: HorizonsGoal[] }) {
  const goalLabels = useTaskGoalsQuery();
  const open = useTasksQuery({ status: "open" });
  const done = useTasksQuery({ status: "done", completed_after: localMonday() });
  const create = useCreateTaskMutation();
  const [title, setTitle] = useState("");
  const [expanded, setExpanded] = useState(false);
  const { dueSoon, anytime } = groupOpenTasks(open.data ?? []);
  const names = new Map([...goals, ...(goalLabels.data ?? [])].map((goal) => [goal.id, goal.title]));
  return (
    <section aria-labelledby="horizons-to-do" className="space-y-6">
      <header className="flex items-baseline justify-between gap-4">
        <h2 id="horizons-to-do" className="os-serif text-3xl text-os-ink">To do</h2>
        <span className="text-xs text-os-faint">One thing at a time.</span>
      </header>
      <form className="flex gap-3 border-b border-os-hairline pb-4" onSubmit={async (event) => {
        event.preventDefault();
        if (!title.trim() || create.isPending) return;
        try { await create.mutateAsync({ title: title.trim() }); setTitle(""); } catch { /* Inline error below. */ }
      }}>
        <input aria-label="Add a task" placeholder="Add a task…" maxLength={200} value={title} disabled={create.isPending} onChange={(event) => setTitle(event.target.value)}
          className="min-h-[44px] min-w-0 flex-1 border-0 bg-transparent px-1 text-base text-os-ink placeholder:text-os-faint focus-visible:outline focus-visible:outline-1 focus-visible:outline-os-accent" />
        <button type="submit" className="os-btn" disabled={!title.trim() || create.isPending}>{create.isPending ? "Adding…" : "Add task"}</button>
      </form>
      {create.isError ? <p role="alert" className="text-sm text-os-danger">Couldn’t add your task. Your text is kept above; try again.</p> : null}
      {open.isPending ? <p className="text-sm text-os-muted">Loading your tasks…</p> : open.isError ? <p role="alert" className="text-sm text-os-danger">Couldn’t load tasks. <button className="os-btn-text" onClick={() => void open.refetch()}>Try again</button></p> : <>
        {[{ label: "Due soon", tasks: dueSoon, soon: true }, { label: "Anytime", tasks: anytime, soon: false }].map(({ label, tasks, soon }) => (
          <div key={label}>
            <h3 className="os-label mb-2">{label}</h3>
            {tasks.length ? <ul>{tasks.map((task) => <TaskRow key={task.id} task={task} goal={names.get(task.parent_goal_id ?? "")} dueSoon={soon} />)}</ul> : <p className="py-3 text-sm text-os-faint">{soon ? "Nothing due in the next seven days." : "Room for whatever comes next."}</p>}
          </div>
        ))}
      </>}
      <div>
        <button type="button" className="os-btn-text flex w-full items-center gap-3 text-left" aria-expanded={expanded} aria-controls="tasks-done-this-week" onClick={() => setExpanded(!expanded)}>
          <span aria-hidden="true">{expanded ? "−" : "+"}</span> Done this week · {done.data?.length ?? (done.isPending ? "…" : "—")}
        </button>
        {done.isError ? <p role="alert" className="text-sm text-os-danger">Couldn’t load completed tasks. <button className="os-btn-text" onClick={() => void done.refetch()}>Try again</button></p> : null}
        {expanded ? <div id="tasks-done-this-week">
          {done.isPending ? <p className="py-3 text-sm text-os-faint">Loading…</p> : done.data?.length ? <ul>{done.data.map((task) => <TaskRow key={task.id} task={task} goal={names.get(task.parent_goal_id ?? "")} />)}</ul> : !done.isError ? <p className="py-3 text-sm text-os-faint">Your finished tasks will collect here.</p> : null}
        </div> : null}
      </div>
    </section>
  );
}
