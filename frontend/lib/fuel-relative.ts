// Honest "when" wording for Fuel sessions. Pure functions so they can be
// unit-tested with Node's built-in runner (see fuel-relative.test.ts).
//
// A workout with no scheduled time is date-only: it is never given a fake
// clock time (midnight), only "Today", "Tomorrow", a weekday, or "Overdue".

const DAY_MS = 86_400_000;

/** Parse "YYYY-MM-DD" as a local calendar day (midnight local). */
export function parseLocalDate(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** Whole calendar days from `now`'s day to `target`'s day (DST-safe). */
function dayDiff(target: Date, now: Date): number {
  return Math.round((startOfDay(target).getTime() - startOfDay(now).getTime()) / DAY_MS);
}

function weekdayDay(d: Date, locale?: string): string {
  return `${d.toLocaleDateString(locale, { weekday: "short" })} ${d.getDate()}`;
}

function clock(d: Date, locale?: string): string {
  return d.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" });
}

/** Row label for one day of the agenda: "Today", "Tomorrow", "Sat 3". */
export function dayLabel(date: Date, now: Date, locale?: string): string {
  const diff = dayDiff(date, now);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  return weekdayDay(date, locale);
}

/**
 * Label for the "Next up" line.
 * - date-only: Today / Tomorrow / weekday (this week) / "Overdue · Mon 28"
 * - timed: minute-level wording, "Overdue · Wed 9:30 AM" once well past
 */
export function formatNextUpLabel(
  scheduledAt: string | null,
  date: string,
  now: number,
  locale?: string,
): string {
  const nowDate = new Date(now);

  if (!scheduledAt) {
    const day = parseLocalDate(date);
    const diff = dayDiff(day, nowDate);
    if (diff < 0) return `Overdue · ${weekdayDay(day, locale)}`;
    if (diff === 0) return "Today";
    if (diff === 1) return "Tomorrow";
    if (diff < 7) return day.toLocaleDateString(locale, { weekday: "long" });
    return weekdayDay(day, locale);
  }

  const target = new Date(scheduledAt);
  const diffMin = Math.round((target.getTime() - now) / 60_000);
  if (diffMin < -120) {
    const when = target.toLocaleString(locale, { weekday: "short", hour: "numeric", minute: "2-digit" });
    return `Overdue · ${when}`;
  }
  if (diffMin < 0) return "Overdue";
  if (diffMin < 5) return "Starting soon";
  if (diffMin < 60) return `In ${diffMin} min`;
  const diff = dayDiff(target, nowDate);
  if (diff === 0) return `Today at ${clock(target, locale)}`;
  if (diff === 1) return `Tomorrow at ${clock(target, locale)}`;
  return target.toLocaleString(locale, { weekday: "short", hour: "numeric", minute: "2-digit" });
}

export function isOverdueLabel(label: string): boolean {
  return label.startsWith("Overdue");
}
