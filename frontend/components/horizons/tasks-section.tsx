"use client";

import { useState } from "react";
import { groupOpenTasks, localMonday, taskPage, topThreeTasks } from "@/lib/horizons-tasks";
import { useCompleteTaskMutation, useCreateTaskMutation, useReopenTaskMutation, useTasksQuery, useTaskGoalsQuery } from "@/lib/queries";
import type { HorizonsGoal, JournalTask } from "@/lib/types";

function TaskRow({ task, goal, dueSoon = false, featured = false }: { task: JournalTask; goal?: string; dueSoon?: boolean; featured?: boolean }) {
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
    } catch { setError(true); }
    finally { setOptimistic(null); }
  }
  return (
    <li className={featured ? "border-t border-os-hairline py-2" : "border-b border-os-hairline py-1"}>
      <div className={`flex min-h-[44px] items-start gap-2 sm:gap-3 ${featured ? "flex-wrap" : ""}`}>
        <label className="relative flex h-11 w-11 shrink-0 cursor-pointer items-center justify-center">
          <input type="checkbox" aria-label={`${done ? "Reopen" : "Complete"} ${task.title}`} checked={done} disabled={busy} onChange={() => void toggle()}
            className="os-task-checkbox" />
          <svg aria-hidden="true" className="os-task-check pointer-events-none absolute h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 6l2.5 2.5L10 3" />
          </svg>
        </label>
        <div className="min-w-0 flex-1 py-2.5">
          <p className={`break-words ${featured ? "os-serif text-[23px] leading-tight" : "text-[15px] leading-relaxed"} ${done ? "text-os-done line-through" : "text-os-ink"}`}>{task.title}</p>
          {goal && !featured ? <p className="mt-1 text-xs text-os-faint sm:hidden">{goal}</p> : null}
          {featured && task.due_date ? <TaskDueDate task={task} highlight={!done} /> : null}
          {error ? <p role="alert" className="mt-1 text-xs text-os-danger">Couldn’t save. Try the checkbox again.</p> : null}
        </div>
        {goal && !featured ? <span className="hidden max-w-[25%] py-3 text-xs text-os-faint sm:block">{goal}</span> : null}
        {!featured && task.due_date ? <TaskDueDate task={task} highlight={dueSoon && !done} /> : null}
      </div>
    </li>
  );
}

function TaskDueDate({ task, highlight }: { task: JournalTask; highlight: boolean }) {
  const date = new Date(`${task.due_date}T00:00:00`);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);
  const label = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return <time dateTime={task.due_date!} className={`block shrink-0 py-3 text-xs ${highlight ? "text-os-accent" : "text-os-faint"}`}>
    {date < today ? `Overdue · ${label}` : +date === +today ? "Today" : +date === +tomorrow ? "Tomorrow" : label}
  </time>;
}

export function TopTasksSection() {
  const open = useTasksQuery({ status: "open" });
  const top = topThreeTasks(open.data ?? []);
  if (!top.length) return null;
  return <section aria-labelledby="top-three">
    <header className="flex flex-wrap items-baseline justify-between gap-2">
      <h2 id="top-three" className="os-label">Top 3 right now</h2>
      <p className="text-xs text-os-faint">Overdue first, then soonest due</p>
    </header>
    <ul className="mt-3 grid gap-4 md:grid-cols-3 md:gap-7">
      {top.map((task) => <TaskRow key={task.id} task={task} featured />)}
    </ul>
  </section>;
}

export function TasksSection({ goals }: { goals: HorizonsGoal[] }) {
  const goalLabels = useTaskGoalsQuery();
  const open = useTasksQuery({ status: "open" });
  const done = useTasksQuery({ status: "done", completed_after: localMonday() });
  const create = useCreateTaskMutation();
  const [title, setTitle] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [shown, setShown] = useState<Record<string, number>>({ "Due soon": 10, Anytime: 10, Done: 10 });
  const donePage = taskPage(done.data ?? [], shown.Done);
  const { dueSoon, anytime } = groupOpenTasks(open.data ?? []);
  const names = new Map([...goals, ...(goalLabels.data ?? [])].map((goal) => [goal.id, goal.title]));
  return (
    <section aria-labelledby="horizons-to-do" className="space-y-6">
      <header className="flex items-baseline justify-between gap-4">
        <h2 id="horizons-to-do" className="os-serif text-3xl text-os-ink">To do</h2>
        <span className="text-xs text-os-faint">{open.data ? `${open.data.length} open` : "One thing at a time."}</span>
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
        {[{ label: "Due soon", tasks: dueSoon, soon: true }, { label: "Anytime", tasks: anytime, soon: false }].map(({ label, tasks, soon }) => {
          const page = taskPage(tasks, shown[label]);
          return (
          <div key={label}>
            <h3 className="os-label mb-2">{label} · {page.total}</h3>
            {tasks.length ? <ul>{page.items.map((task) => <TaskRow key={task.id} task={task} goal={names.get(task.parent_goal_id ?? "")} dueSoon={soon} />)}</ul> : <p className="py-3 text-sm text-os-faint">{soon ? "Nothing due in the next seven days." : "Room for whatever comes next."}</p>}
            {page.more ? <button type="button" className="os-btn-text !text-os-accent" onClick={() => setShown((prev) => ({ ...prev, [label]: prev[label] + 10 }))}>Show {page.more} more</button> : null}
          </div>
        ); })}
      </>}
      <div>
        <button type="button" className="os-btn-text flex w-full items-center gap-3 text-left" aria-expanded={expanded} aria-controls="tasks-done-this-week" onClick={() => setExpanded(!expanded)}>
          <span aria-hidden="true">{expanded ? "−" : "+"}</span> Done this week · {done.data?.length ?? (done.isPending ? "…" : "—")}
        </button>
        {done.isError ? <p role="alert" className="text-sm text-os-danger">Couldn’t load completed tasks. <button className="os-btn-text" onClick={() => void done.refetch()}>Try again</button></p> : null}
        {expanded ? <div id="tasks-done-this-week">
          {done.isPending ? <p className="py-3 text-sm text-os-faint">Loading…</p> : done.data?.length ? <ul>{donePage.items.map((task) => <TaskRow key={task.id} task={task} goal={names.get(task.parent_goal_id ?? "")} />)}</ul> : !done.isError ? <p className="py-3 text-sm text-os-faint">Your finished tasks will collect here.</p> : null}
          {donePage.more ? <button type="button" className="os-btn-text !text-os-accent" onClick={() => setShown((prev) => ({ ...prev, Done: prev.Done + 10 }))}>Show {donePage.more} more</button> : null}
        </div> : null}
      </div>
    </section>
  );
}
