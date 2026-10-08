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

/** Date-only ISO due dates sort chronologically: overdue, today, then future.
 * Ties use oldest creation instant, then ID so API order cannot reshuffle them.
 * Only undated tasks fill any remaining slots, also oldest first.
 */
export function topThreeTasks<T extends GroupableTask & { id: string; created_at: string }>(tasks: readonly T[]): T[] {
  const oldest = (a: T, b: T) => Date.parse(a.created_at) - Date.parse(b.created_at) || a.id.localeCompare(b.id);
  const open = tasks.filter((task) => task.status === "open");
  const dated = open.filter((task) => task.due_date).sort((a, b) => a.due_date!.localeCompare(b.due_date!) || oldest(a, b));
  const anytime = open.filter((task) => !task.due_date).sort(oldest);
  return [...dated, ...anytime].slice(0, 3);
}

export function taskPage<T>(tasks: readonly T[], shown = 10): { items: T[]; total: number; more: number } {
  const count = Math.max(0, Math.floor(shown));
  return { items: tasks.slice(0, count), total: tasks.length, more: Math.min(10, Math.max(0, tasks.length - count)) };
}
