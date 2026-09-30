"use client";

import Link from "next/link";
import { GoalCard } from "@/components/goal-card";
import { openTaskCounts } from "@/lib/horizons-tasks";
import { useDocumentsQuery, useJournalStatusQuery, useTasksQuery } from "@/lib/queries";
import type { HorizonsGoal } from "@/lib/types";

function dateLabel(value: string, dateOnly = false) {
  return new Date(dateOnly ? `${value}T12:00:00` : value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function GoalsProjects({ goals }: { goals: HorizonsGoal[] }) {
  const projects = useDocumentsQuery("project");
  const tasks = useTasksQuery({ status: "open" });
  const status = useJournalStatusQuery();
  const counts = openTaskCounts(tasks.data ?? []);
  const targets = new Map(status.data?.active_goals.map((goal) => [goal.id, goal.target_date]));
  const active = goals.filter((goal) => !goal.status || goal.status === "active");
  const recent = [...(projects.data ?? [])].filter((doc) => doc.kind === "project")
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id)).slice(0, 5);
  return <div className="grid gap-8 md:grid-cols-2 md:gap-14">
    <section aria-labelledby="horizons-goals">
      <h2 id="horizons-goals" className="os-label mb-3">Goals</h2>
      {active.map((goal) => {
        const target = targets.get(goal.id);
        const summary = <><span className="os-serif min-w-0 text-[22px] text-os-ink">{goal.title}</span>
          <span className="shrink-0 text-xs text-os-faint">{tasks.data ? `${counts.get(goal.id) ?? 0} open` : tasks.isError ? "Count unavailable" : "Loading…"}{target ? ` · ${dateLabel(target, true)}` : ""}{goal.slug.startsWith("typed:") ? <span aria-hidden="true" className="ml-2">⌄</span> : null}</span></>;
        const row = "os-focus flex min-h-[52px] items-baseline justify-between gap-3 border-t border-os-hairline py-3";
        // Typed goals retain their notes, checklist and achievement controls.
        return goal.slug.startsWith("typed:") ? <details key={goal.id}>
          <summary className={`${row} cursor-pointer`}>{summary}</summary><GoalCard goal={goal} />
        </details> : <Link key={goal.id} href={`/journal#goal/${encodeURIComponent(goal.slug)}`} className={row}>{summary}</Link>;
      })}
      {!active.length ? <p className="text-sm text-os-faint">No goals yet. Write about your goals in your Journal.</p> : null}
    </section>
    <section aria-labelledby="horizons-projects">
      <h2 id="horizons-projects" className="os-label mb-3">Projects</h2>
      {projects.isPending ? <p className="text-sm text-os-faint">Loading projects…</p> : projects.isError ? <p role="alert" className="text-sm text-os-danger">Couldn’t load projects. <button className="os-btn-text" onClick={() => void projects.refetch()}>Try again</button></p> : recent.length ? <>
        {recent.map((project) => <Link key={project.id} href={`/journal#project/${encodeURIComponent(project.slug)}`} className="os-focus flex min-h-[52px] items-baseline justify-between gap-3 border-t border-os-hairline py-3">
          <span className="os-serif min-w-0 text-[22px] text-os-ink">{project.title}</span>
          <span className="shrink-0 text-xs text-os-faint">edited <time dateTime={project.updated_at}>{dateLabel(project.updated_at)}</time></span>
        </Link>)}
        <p className="mt-2 text-xs leading-relaxed text-os-faint">Project pages live in your Journal. Click one to open it.</p>
      </> : <p className="text-sm text-os-faint">No projects yet.</p>}
    </section>
  </div>;
}
