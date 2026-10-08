"use client";

import clsx from "clsx";
import { type ReactNode, useSyncExternalStore } from "react";

import { labelCls } from "@/components/neighborhood/ui";
import { initialOf, isClosed, ownerInitials, ownersOf, type PlanMember, type PlanStep, type ProjectPlan, type Tone } from "@/lib/project-plan";

/*
 * Small pieces shared by the project page: the colours a person and a step
 * are drawn in, the milestone diamond, owner dots, the Plan/Timeline/People
 * pill, and the capsule buttons. Open Sky: teal only ever means done, amber
 * only means attention, and each person keeps one colour (you are the accent).
 */

export const toneCls: Record<Tone, string> = {
  done: "text-os-done",
  attn: "text-os-attn",
  ink: "text-white",
  muted: "text-os-muted",
  faint: "text-os-faint",
};

/** A person's colour: the accent for you, their own profile hue for everyone else. */
export function memberColor(plan: ProjectPlan, member: PlanMember | undefined): string {
  if (!member) return "var(--os-ink)";
  if (member.id === plan.myMembershipId) return "var(--os-accent)";
  return `hsl(${member.hue} 78% 86%)`;
}

/**
 * The bar / dot colour for a step: teal once it's done, its owner's colour,
 * starlight when several people share it, faint when nobody has it yet.
 */
export function stepColor(plan: ProjectPlan, step: PlanStep): string {
  if (step.status === "done") return "var(--os-done)";
  const owners = ownersOf(plan, step);
  if (owners.length === 0) return "var(--os-faint)";
  if (owners.length > 1) return owners.some((m) => m.id === plan.myMembershipId) ? "var(--os-accent)" : "var(--os-ink)";
  return memberColor(plan, owners[0]);
}

export const sectionLabelCls = "text-[0.6875rem] font-semibold uppercase tracking-[0.16em] text-os-label";
export const capsuleCls =
  "os-focus inline-flex min-h-[40px] shrink-0 items-center justify-center gap-1.5 rounded-full border border-os-ring px-4 text-[0.8125rem] text-os-ink transition hover:border-os-accent-line hover:text-os-accent disabled:cursor-not-allowed disabled:opacity-40";
export const doneCapsuleCls =
  "os-focus inline-flex min-h-[40px] shrink-0 items-center justify-center gap-1.5 rounded-full border border-[color-mix(in_srgb,var(--os-done)_60%,transparent)] px-4 text-[0.8125rem] text-os-done transition hover:bg-[color-mix(in_srgb,var(--os-done)_10%,transparent)] disabled:cursor-not-allowed disabled:opacity-40";
export const rowHoverCls = "transition hover:bg-[rgba(226,232,240,0.04)]";

/** The milestone marker: teal and filled once reached, accent outline while it's the one you're working toward. */
export function Diamond({ reached, active = true, size = 14, className }: { reached: boolean; active?: boolean; size?: number; className?: string }) {
  const stroke = reached ? "var(--os-done)" : active ? "var(--os-accent)" : "var(--os-ring)";
  return (
    <svg width={size} height={size} viewBox="0 0 14 14" aria-hidden="true" className={clsx("shrink-0", className)}>
      <path d="M7 1 L13 7 L7 13 L1 7 Z" fill={reached ? "var(--os-done)" : "none"} stroke={stroke} strokeWidth="1.5" />
    </svg>
  );
}

/** Who has a step, as one small filled dot with their initial(s). */
export function OwnerDot({ plan, step, size = 22 }: { plan: ProjectPlan; step: PlanStep; size?: number }) {
  const text = ownerInitials(plan, step);
  const none = ownersOf(plan, step).length === 0 && !isClosed(step);
  return (
    <span
      aria-hidden="true"
      className={clsx("flex shrink-0 items-center justify-center rounded-full font-bold leading-none", none ? "border border-dashed border-os-ring text-os-faint" : "text-os-sky")}
      style={{ width: size, height: size, fontSize: text.length > 2 ? size * 0.33 : size * 0.41, backgroundColor: none ? "transparent" : stepColor(plan, step) }}
    >
      {text}
    </span>
  );
}

/** The initial-in-a-ring used wherever you pick people. */
export function PersonRing({ name, color, selected = false, size = 52, initial }: { name: string; color: string; selected?: boolean; size?: number; initial?: string }) {
  return (
    <span
      aria-hidden="true"
      className={clsx("flex shrink-0 items-center justify-center rounded-full border transition", selected ? "border-white/75 bg-os-accent-soft" : "border-os-ring")}
      style={{ width: size, height: size, color, fontSize: size * 0.34 }}
    >
      {initial ?? initialOf(name)}
    </span>
  );
}

/** A grid of people to tick: ring, name, selected state. */
export function PeoplePicker({
  label,
  options,
  picked,
  onToggle,
}: {
  label: string;
  options: { id: string; name: string; color: string; sub?: string; initial?: string }[];
  picked: Set<string>;
  onToggle: (id: string) => void;
}) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-x-4 gap-y-4">
      {options.map((o) => {
        const on = picked.has(o.id);
        return (
          <button key={o.id} type="button" aria-pressed={on} onClick={() => onToggle(o.id)} className="os-focus flex w-[76px] flex-col items-center gap-2 rounded-xl">
            <PersonRing name={o.name} color={o.color} selected={on} initial={o.initial} />
            <span className={clsx("w-full truncate text-center text-[0.8125rem]", on ? "text-white" : "text-os-muted")}>{o.name}</span>
            {o.sub ? <span className="-mt-1.5 text-[0.6875rem] text-os-faint">{o.sub}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

/** The segmented pill that switches views in place (Plan · Timeline · People, Weeks · Whole project). */
export function PillTabs<T extends string>({
  label,
  options,
  value,
  onChange,
  small = false,
}: {
  label: string;
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  small?: boolean;
}) {
  return (
    <div role="tablist" aria-label={label} className="inline-flex shrink-0 gap-0.5 rounded-full border border-os-hairline p-[3px]">
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="tab"
            aria-selected={on}
            onClick={() => onChange(o.value)}
            className={clsx(
              "os-focus rounded-full text-[0.8125rem] transition",
              small ? "min-h-[34px] px-3.5" : "min-h-[38px] px-4",
              on ? "bg-[rgba(226,232,240,0.12)] font-semibold text-white" : "text-os-faint hover:text-os-ink",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/** A labelled value on the step page: quiet label left, value right of it. */
export function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-os-faint">{label}</dt>
      <dd className="min-w-0 text-os-ink">{children}</dd>
    </>
  );
}

/** A date field you can leave empty: the native picker plus a way to clear it. */
export function DateField({ id, label, value, onChange, min }: { id: string; label: string; value: string; onChange: (value: string) => void; min?: string }) {
  return (
    <div className="flex min-h-[52px] items-center justify-between gap-4 border-t border-os-hairline">
      <label htmlFor={id} className="text-[0.9375rem] text-os-ink">
        {label}
      </label>
      <span className="flex items-center gap-3">
        <input
          id={id}
          type="date"
          value={value}
          min={min}
          onChange={(e) => onChange(e.target.value)}
          className={clsx("os-focus min-h-[40px] rounded border-0 bg-transparent text-right text-[0.875rem] outline-none [color-scheme:dark]", value ? "text-os-ink" : "text-os-faint")}
        />
        {value ? (
          <button type="button" onClick={() => onChange("")} aria-label={`Clear ${label.toLowerCase()} date`} className="os-focus flex h-8 w-8 items-center justify-center rounded-full text-os-faint hover:text-os-ink">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        ) : null}
      </span>
    </div>
  );
}

export function FieldLabel({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) {
  return htmlFor ? (
    <label htmlFor={htmlFor} className={labelCls}>
      {children}
    </label>
  ) : (
    <span className={labelCls}>{children}</span>
  );
}

function subscribeTo(query: string) {
  return (onChange: () => void) => {
    const mq = window.matchMedia(query);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  };
}

/** True when the viewport matches (false on the server and the first paint). */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    subscribeTo(query),
    () => window.matchMedia(query).matches,
    () => false,
  );
}

const noop = () => () => {};

/**
 * False on the server and during hydration, true afterwards. These pages
 * depend on who's signed in, which the prerendered HTML can't know — so the
 * first paint is always the same quiet "Loading…".
 */
export function useMounted(): boolean {
  return useSyncExternalStore(noop, () => true, () => false);
}

/** Small line icons for the ghost-circle answers and the "+" menu. */
export function Icon({ name, size = 18 }: { name: "check" | "calendar" | "half" | "x" | "plus" | "person" | "pencil" | "trash" | "play" | "target"; size?: number }) {
  const paths: Record<string, ReactNode> = {
    check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
    calendar: (
      <>
        <rect x="4" y="5.5" width="16" height="14" rx="2.5" />
        <path d="M4 10h16M8.5 3.5v4M15.5 3.5v4" />
      </>
    ),
    half: (
      <>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 4a8 8 0 0 0 0 16z" fill="currentColor" stroke="none" />
      </>
    ),
    x: <path d="M6 6l12 12M18 6L6 18" />,
    plus: <path d="M12 5v14M5 12h14" />,
    person: (
      <>
        <circle cx="10" cy="8.5" r="3.5" />
        <path d="M3.5 19.5c.8-3.4 3.3-5 6.5-5 1.4 0 2.7.3 3.7.9M18 13v6M15 16h6" />
      </>
    ),
    pencil: <path d="M4 20l1-4.5L16.5 4 20 7.5 8.5 19zM14 6.5l3.5 3.5" />,
    trash: <path d="M5 7h14M9.5 7V4.5h5V7M7 7l.8 12.5h8.4L17 7" />,
    play: <path d="M8 5.5v13L18.5 12z" />,
    target: (
      <>
        <circle cx="12" cy="12" r="7.5" />
        <circle cx="12" cy="12" r="2.5" />
      </>
    ),
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {paths[name]}
    </svg>
  );
}
