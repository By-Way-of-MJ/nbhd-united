"use client";

import { useEffect, useMemo, useState } from "react";

import {
  useCompleteWorkoutMutation,
  useDeleteWorkoutMutation,
  useScheduleWindowQuery,
  useSkipWorkoutMutation,
} from "@/lib/queries";
import { dayLabel, formatNextUpLabel, isOverdueLabel } from "@/lib/fuel-relative";
import type { FuelWorkout, WorkoutCategory } from "@/lib/types";
import { SkelBar } from "@/components/ui/skeleton";
import { CATEGORIES } from "./category-meta";

interface ScheduleWeekProps {
  onAddSession: (date: string) => void;
  onOpenWorkout: (id: string) => void;
}

function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function nextSevenDays(): { iso: string; date: Date }[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const out: { iso: string; date: Date }[] = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(today);
    d.setDate(d.getDate() + i);
    out.push({ iso: isoDate(d), date: d });
  }
  return out;
}

function formatTime(scheduledAt: string | null): string | null {
  if (!scheduledAt) return null;
  const d = new Date(scheduledAt);
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function ScheduleWeek({ onAddSession, onOpenWorkout }: ScheduleWeekProps) {
  const { data, isLoading, isPending } = useScheduleWindowQuery("7d");

  const days = useMemo(() => nextSevenDays(), []);

  const byDate = useMemo(() => {
    const m: Record<string, FuelWorkout[]> = {};
    for (const w of data || []) {
      (m[w.date] ||= []).push(w);
    }
    // Sort each day's sessions by scheduled_at (nulls last) then created_at
    for (const iso in m) {
      m[iso].sort((a, b) => {
        const aTime = a.scheduled_at ? new Date(a.scheduled_at).getTime() : Number.POSITIVE_INFINITY;
        const bTime = b.scheduled_at ? new Date(b.scheduled_at).getTime() : Number.POSITIVE_INFINITY;
        if (aTime !== bTime) return aTime - bTime;
        return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
      });
    }
    return m;
  }, [data]);

  // First "planned" session in chronological order — what's coming next
  // (or what's overdue, when scheduled_at is in the past).
  const nextUp = useMemo(() => {
    const planned = (data || []).filter((w) => w.status === "planned");
    planned.sort((a, b) => {
      const aTime = a.scheduled_at ? new Date(a.scheduled_at).getTime() : Date.parse(a.date);
      const bTime = b.scheduled_at ? new Date(b.scheduled_at).getTime() : Date.parse(b.date);
      return aTime - bTime;
    });
    return planned[0];
  }, [data]);

  return (
    <div className="space-y-3">
      {nextUp ? (
        <NextUpBanner workout={nextUp} onOpen={() => onOpenWorkout(nextUp.id)} />
      ) : isPending ? (
        <NextUpSkeleton />
      ) : null}

      <div className="flex items-center justify-between gap-3">
        <h2 data-os-label className="font-headline text-base sm:text-lg font-semibold text-ink">Next 7 days</h2>
        {isLoading && data && (
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-faint">syncing…</span>
        )}
      </div>

      {/* One agenda list, top to bottom in date order, one row per day. */}
      <ol className="border-t border-border">
        {days.map(({ iso, date }, i) => (
          <DayRow
            key={iso}
            iso={iso}
            date={date}
            isToday={i === 0}
            sessions={byDate[iso] || []}
            onAddSession={onAddSession}
            onOpenWorkout={onOpenWorkout}
          />
        ))}
      </ol>
    </div>
  );
}

interface DayRowProps {
  iso: string;
  date: Date;
  isToday: boolean;
  sessions: FuelWorkout[];
  onAddSession: (iso: string) => void;
  onOpenWorkout: (id: string) => void;
}

function DayRow({ iso, date, isToday, sessions, onAddSession, onOpenWorkout }: DayRowProps) {
  const label = dayLabel(date, new Date());
  const showDate = label === "Today" || label === "Tomorrow";
  const addButton = (
    <button
      type="button"
      aria-label={`Add a session on ${date.toDateString()}`}
      onClick={() => onAddSession(iso)}
      className="relative inline-flex items-center gap-1 rounded-md px-1 py-0.5 text-xs text-ink-faint transition hover:text-ink before:absolute before:-inset-x-2 before:-inset-y-3 before:content-[''] [@media(hover:hover)]:opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
    >
      <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M12 5v14M5 12h14" strokeLinecap="round" />
      </svg>
      Add
    </button>
  );

  return (
    <li className="group grid grid-cols-[84px_minmax(0,1fr)] sm:grid-cols-[120px_minmax(0,1fr)] gap-x-4 border-b border-border py-4">
      <div className="min-w-0 pt-0.5">
        <div className={`text-sm font-semibold ${isToday ? "text-accent" : "text-ink"}`}>{label}</div>
        {showDate && (
          <div className="mt-0.5 text-xs text-ink-faint">
            {date.toLocaleDateString(undefined, { weekday: "short" })} {date.getDate()}
          </div>
        )}
      </div>
      <div className="min-w-0">
        {sessions.length === 0 ? (
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm text-ink-faint">Rest day</span>
            {addButton}
          </div>
        ) : (
          <>
            <div className="space-y-1">
              {sessions.map((s) => (
                <SessionItem key={s.id} workout={s} onOpen={() => onOpenWorkout(s.id)} />
              ))}
            </div>
            <div className="mt-1">{addButton}</div>
          </>
        )}
      </div>
    </li>
  );
}

function NextUpSkeleton() {
  return (
    <section data-os-surface
      aria-busy="true"
      role="status"
      aria-label="Loading next workout"
      className="rounded-panel border border-border bg-card/95 p-4 sm:p-5 shadow-panel backdrop-blur-md relative overflow-hidden"
      style={{ borderLeftWidth: "3px", borderLeftColor: "rgba(226,232,240,0.15)" }}
    >
      <div className="flex items-start gap-4 sm:items-center sm:justify-between flex-col sm:flex-row">
        <div className="min-w-0 w-full sm:w-auto">
          <div className="flex items-center gap-2 mb-2">
            <SkelBar className="h-3 w-14" />
            <SkelBar className="h-3 w-24" />
          </div>
          <SkelBar className="h-6 w-2/3 sm:w-72" />
          <div className="mt-2 flex items-center gap-3">
            <SkelBar className="h-3 w-16" />
            <SkelBar className="h-3 w-12" />
          </div>
        </div>
        <div className="flex w-full sm:w-auto items-center gap-2 shrink-0">
          <SkelBar className="h-11 flex-1 sm:w-28" />
          <SkelBar className="h-11 w-20" />
          <SkelBar className="h-11 w-20 hidden sm:block" />
        </div>
      </div>
    </section>
  );
}

interface NextUpBannerProps {
  workout: FuelWorkout;
  onOpen: () => void;
}

function NextUpBanner({ workout, onOpen }: NextUpBannerProps) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    // Re-render every 30s so the relative time stays accurate without
    // refetching the whole window.
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const skip = useSkipWorkoutMutation();
  const complete = useCompleteWorkoutMutation();

  // Brief "✓ Done" confirmation that survives the cache invalidation +
  // workout-prop swap, so the user gets explicit feedback that the click took.
  const [justCompleted, setJustCompleted] = useState<FuelWorkout | null>(null);
  useEffect(() => {
    if (!justCompleted) return;
    const id = window.setTimeout(() => setJustCompleted(null), 1400);
    return () => window.clearTimeout(id);
  }, [justCompleted]);

  const display = justCompleted ?? workout;
  const cat = CATEGORIES[display.category as WorkoutCategory] ?? CATEGORIES.other;
  const relative = formatNextUpLabel(display.scheduled_at, display.date, now);
  const isOverdue = isOverdueLabel(relative);

  const onComplete = () => {
    const target = workout;
    complete.mutate(
      { id: target.id },
      {
        onSuccess: () => setJustCompleted({ ...target, status: "done" }),
      },
    );
  };

  return (
    <section data-os-surface
      aria-label="Next workout"
      className="
        rounded-panel border border-border bg-card/95 p-4 sm:p-5 shadow-panel backdrop-blur-md
        relative overflow-hidden hover:border-border-strong transition-colors
      "
      style={{
        // accent stripe along the left edge in the category color
        borderLeftWidth: "3px",
        borderLeftColor: cat.accent,
      }}
    >
      {/* Full-card tap target — sits beneath the action buttons so they keep working.
          Disabled during the post-complete flash so the user can't accidentally
          re-open the completed workout while it's animating out. */}
      {!justCompleted && (
        <button
          type="button"
          onClick={onOpen}
          aria-label={`View ${workout.activity}`}
          className="absolute inset-0 z-0 rounded-panel focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent"
        />
      )}
      <div className="relative z-10 flex items-start gap-4 sm:items-center sm:justify-between flex-col sm:flex-row pointer-events-none">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-accent">
              Next up
            </span>
            <span
              className={`font-mono text-[10px] uppercase tracking-[0.18em] ${
                isOverdue ? "text-status-amber-text" : "text-ink-faint"
              }`}
            >
              · {relative}
            </span>
          </div>
          <h3 className="font-headline text-xl font-semibold text-ink truncate">
            {display.activity}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
            <span className="capitalize">{cat.label}</span>
            {display.duration_minutes && <span>· {display.duration_minutes} min</span>}
          </div>
        </div>

        <div className="flex w-full sm:w-auto items-center gap-2 shrink-0 pointer-events-auto">
          {justCompleted ? (
            <div
              role="status"
              aria-live="polite"
              className="flex-1 sm:flex-none rounded-full bg-status-emerald text-status-emerald-text px-4 py-2.5 text-sm font-semibold min-h-[44px] flex items-center justify-center gap-2 animate-reveal"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="3" aria-hidden="true">
                <path d="M5 12l5 5L20 7" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Done
            </div>
          ) : (
            <>
              <button
                type="button"
                onClick={onComplete}
                disabled={complete.isPending}
                className="glow-purple flex-1 sm:flex-none rounded-full bg-accent px-4 py-2.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:opacity-50 min-h-[44px] flex items-center justify-center"
              >
                {complete.isPending ? "Completing…" : "Complete"}
              </button>
              <button
                type="button"
                onClick={() => {
                  const reason = window.prompt("Skip reason (optional):") || "";
                  skip.mutate({ id: workout.id, reason });
                }}
                disabled={skip.isPending}
                className="rounded-full border border-border bg-transparent px-4 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink active:scale-95 disabled:opacity-50 min-h-[44px]"
              >
                Skip
              </button>
              <button
                type="button"
                onClick={onOpen}
                className="rounded-full border border-border bg-transparent px-4 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink active:scale-95 min-h-[44px] hidden sm:inline-flex"
              >
                View
              </button>
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function SessionItem({ workout, onOpen }: { workout: FuelWorkout; onOpen: () => void }) {
  const time = formatTime(workout.scheduled_at);
  const cat = CATEGORIES[workout.category as WorkoutCategory] ?? CATEGORIES.other;
  const isDone = workout.status === "done";
  const meta = [
    time,
    cat.label,
    workout.duration_minutes ? `${workout.duration_minutes}m` : null,
  ].filter(Boolean);

  return (
    <div className="flex items-start gap-2">
      <button
        type="button"
        onClick={onOpen}
        className="min-w-0 flex-1 py-1 text-left"
      >
        <span className={`block text-sm font-medium ${isDone ? "text-ink-muted" : "text-ink"}`}>
          {workout.activity}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-xs text-ink-muted">
          <span>{meta.join(" · ")}</span>
          {isDone && <span className="font-medium text-emerald-text">Done</span>}
          {workout.status === "skipped" && <span className="text-ink-faint">Skipped</span>}
        </span>
      </button>
      <SessionMenu workout={workout} />
    </div>
  );
}

function SessionMenu({ workout }: { workout: FuelWorkout }) {
  const [open, setOpen] = useState(false);
  const skip = useSkipWorkoutMutation();
  const complete = useCompleteWorkoutMutation();
  const del = useDeleteWorkoutMutation();

  const close = () => setOpen(false);
  const action = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
    } finally {
      close();
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        aria-label="Session actions"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="relative inline-flex h-8 w-8 items-center justify-center rounded-md text-ink-faint transition before:absolute before:-inset-1.5 before:content-[''] hover:bg-surface-hover hover:text-ink"
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
          <circle cx="5" cy="12" r="1.5" />
          <circle cx="12" cy="12" r="1.5" />
          <circle cx="19" cy="12" r="1.5" />
        </svg>
      </button>
      {open && (
        <>
          {/* Click-away */}
          <button
            type="button"
            aria-label="Dismiss menu"
            className="fixed inset-0 z-10 cursor-default"
            onClick={close}
          />
          <div
            role="menu"
            className="absolute right-0 top-full z-20 mt-1 min-w-[180px] rounded-xl border border-border bg-surface-elevated shadow-panel backdrop-blur-md"
          >
            {workout.status === "planned" && (
              <>
                <MenuItem
                  onSelect={() =>
                    action(() => complete.mutateAsync({ id: workout.id }))
                  }
                  disabled={complete.isPending}
                  label="Mark complete"
                />
                <MenuItem
                  onSelect={() => {
                    const reason = window.prompt("Skip reason (optional):") || "";
                    void action(() => skip.mutateAsync({ id: workout.id, reason }));
                  }}
                  disabled={skip.isPending}
                  label="Skip…"
                />
              </>
            )}
            <MenuItem
              onSelect={() => {
                if (window.confirm(`Delete "${workout.activity}"?`)) {
                  void action(() => del.mutateAsync(workout.id));
                }
              }}
              disabled={del.isPending}
              label="Delete"
              tone="rose"
            />
          </div>
        </>
      )}
    </div>
  );
}

function MenuItem({
  onSelect,
  label,
  disabled,
  tone,
}: {
  onSelect: () => void;
  label: string;
  disabled?: boolean;
  tone?: "rose";
}) {
  const toneCls = tone === "rose" ? "text-rose-text hover:bg-rose-bg" : "text-ink hover:bg-surface-hover";
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onSelect}
      disabled={disabled}
      className={`block w-full px-3 py-2.5 text-left text-sm transition disabled:opacity-50 ${toneCls}`}
    >
      {label}
    </button>
  );
}
