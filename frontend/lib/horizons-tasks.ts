/** Calendar boundaries use the browser's local timezone, like daily slugs. */
export function localMonday(now = new Date()): string {
  const monday = new Date(now);
  monday.setDate(monday.getDate() - (monday.getDay() + 6) % 7);
  monday.setHours(0, 0, 0, 0);
  // Send an instant, preserving local midnight even across DST / UTC offsets.
  return monday.toISOString();
}

interface GroupableTask { status: string; due_date: string | null; parent_goal_id?: string | null }

export function groupOpenTasks<T extends GroupableTask>(tasks: readonly T[], now = new Date()): { dueSoon: T[]; anytime: T[] } {
  const end = new Date(now);
  end.setDate(end.getDate() + 7);
  const cutoff = `${end.getFullYear()}-${String(end.getMonth() + 1).padStart(2, "0")}-${String(end.getDate()).padStart(2, "0")}`;
  const dueSoon: T[] = [], anytime: T[] = [];
  for (const task of tasks) {
    if (task.status !== "open") continue;
    (task.due_date && task.due_date <= cutoff ? dueSoon : anytime).push(task);
  }
  dueSoon.sort((a, b) => a.due_date!.localeCompare(b.due_date!));
  return { dueSoon, anytime };
}

export function openTaskCounts(tasks: readonly GroupableTask[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const task of tasks) {
    if (task.status === "open" && task.parent_goal_id) counts.set(task.parent_goal_id, (counts.get(task.parent_goal_id) ?? 0) + 1);
  }
  return counts;
}
