"use client";

import clsx from "clsx";
import qrcode from "qrcode-generator";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

/*
 * Small Open Sky pieces shared by the Neighborhood page and its sub-pages:
 * the right-side panel (replaces the old boxed modals), thin-line fields, the
 * initial avatar ring, the QR code and a copy-link row.
 */

export const labelCls = "text-[0.6875rem] font-semibold uppercase tracking-[0.14em] text-os-faint";
export const fieldCls =
  "min-h-[44px] w-full border-0 border-b border-os-ring bg-transparent px-0 text-[0.9375rem] text-os-ink outline-none transition placeholder:text-os-faint focus:border-os-accent focus-visible:shadow-none focus-visible:outline-none";
export const textBtnCls = "os-focus inline-flex min-h-[40px] items-center rounded text-[0.8125rem] text-os-accent transition hover:text-white disabled:opacity-40";
export const quietBtnCls = "os-focus inline-flex min-h-[40px] items-center rounded text-[0.8125rem] text-os-muted transition hover:text-os-ink disabled:opacity-40";
export const dangerBtnCls = "os-focus inline-flex min-h-[40px] items-center rounded text-[0.8125rem] text-os-danger transition hover:text-white disabled:opacity-40";
export const ghostBtnCls =
  "os-focus inline-flex min-h-[40px] shrink-0 items-center justify-center rounded-full border border-os-accent-line px-4 text-[0.8125rem] text-os-accent transition hover:bg-os-accent-soft disabled:cursor-not-allowed disabled:opacity-40";

/** The hue ring with an initial — people are never a filled blob. */
export function Initial({ name, hue, size = 36 }: { name: string; hue: number; size?: number }) {
  return (
    <span
      aria-hidden="true"
      className="flex shrink-0 items-center justify-center rounded-full border text-[0.8125rem]"
      style={{ width: size, height: size, borderColor: `hsl(${hue} 70% 85% / 0.5)`, color: `hsl(${hue} 80% 92%)` }}
    >
      {name.trim().charAt(0).toLocaleUpperCase() || "?"}
    </span>
  );
}

export function SectionHead({ label, trailing, id }: { label: string; trailing?: ReactNode; id?: string }) {
  return (
    <div className="flex min-h-[32px] items-baseline justify-between gap-4">
      <h2 id={id} className="text-[0.75rem] font-semibold uppercase tracking-[0.15em] text-os-label">
        {label}
      </h2>
      {trailing}
    </div>
  );
}

export function ErrorLine({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="text-[0.8125rem] text-os-danger">
      {children}
    </p>
  );
}

/**
 * A panel on the right edge of the screen (a full sheet on phones) — the Open
 * Sky replacement for boxed modals. Esc and the shade close it; focus moves in
 * on open and returns to where it was on close.
 */
export function SidePanel({ label, onClose, children, wide = false, bare = false }: { label: string; onClose: () => void; children: ReactNode; wide?: boolean; bare?: boolean }) {
  const ref = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>("[data-autofocus]") ?? el;
    first?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) {
        e.preventDefault();
        closeRef.current();
      }
      if (e.key === "Tab" && el) {
        // Keep Tab inside the panel while it's open.
        const items = [...el.querySelectorAll<HTMLElement>("a[href],button:not([disabled]),input:not([disabled]),select,textarea,[tabindex]:not([tabindex='-1'])")].filter((n) => n.offsetParent !== null);
        if (!items.length) return;
        const firstItem = items[0], last = items[items.length - 1];
        if (e.shiftKey && (document.activeElement === firstItem || document.activeElement === el)) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          firstItem.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      before?.focus?.({ preventScroll: true });
    };
  }, []);
  // Sized with vw/dvh, never inset-0 on the fixed layer: a transformed
  // ancestor would otherwise become its containing block.
  return (
    <div className="fixed left-0 top-0 z-[80] h-[100dvh] w-[100vw]">
      <div className="os-shade-in absolute left-0 top-0 h-full w-full bg-[rgba(3,4,7,0.62)]" aria-hidden="true" onClick={onClose} />
      <aside
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        className={clsx(
          "os-panel-in os-panel-sky absolute right-0 top-0 flex h-full w-full flex-col border-l border-os-hairline outline-none focus-visible:shadow-none",
          bare ? "overflow-hidden pt-[env(safe-area-inset-top)]" : "overflow-y-auto px-5 pb-[calc(env(safe-area-inset-bottom)+2rem)] pt-[calc(env(safe-area-inset-top)+1.25rem)] sm:px-11 sm:pt-10",
          wide ? "sm:w-[600px]" : "sm:w-[520px]",
        )}
      >
        {children}
      </aside>
    </div>
  );
}

export function PanelHeader({ eyebrow, title, onClose, eyebrowColor }: { eyebrow?: ReactNode; title: ReactNode; onClose: () => void; eyebrowColor?: string }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex min-w-0 flex-col gap-2">
        {eyebrow ? (
          <span className="text-[0.6875rem] font-semibold uppercase tracking-[0.16em] text-os-faint" style={eyebrowColor ? { color: eyebrowColor } : undefined}>
            {eyebrow}
          </span>
        ) : null}
        <h2 className="os-serif break-words text-[2rem] leading-[1.05] text-white sm:text-[2.375rem]">{title}</h2>
      </div>
      <CloseButton onClose={onClose} />
    </div>
  );
}

export function CloseButton({ onClose, label = "Close" }: { onClose: () => void; label?: string }) {
  return (
    <button
      type="button"
      onClick={onClose}
      aria-label={label}
      className="os-focus flex h-11 w-11 shrink-0 items-center justify-center rounded-full border border-os-ring text-os-ink transition hover:border-os-accent-line hover:text-os-accent"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
        <path d="M6 6l12 12M18 6L6 18" />
      </svg>
    </button>
  );
}

/** A labelled group inside a panel: hairline, small caps label, content. */
export function PanelBlock({ label, children, trailing }: { label: string; children: ReactNode; trailing?: ReactNode }) {
  return (
    <section className="os-hairline-top flex flex-col gap-2 pt-4">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className={labelCls}>{label}</h3>
        {trailing}
      </div>
      {children}
    </section>
  );
}

/** Scannable QR (dark on a light tile — cameras need the contrast). */
export function QrCode({ value, size = 168, label }: { value: string; size?: number; label: string }) {
  const { d, n } = useMemo(() => {
    const q = qrcode(0, "M");
    q.addData(value);
    q.make();
    const count = q.getModuleCount();
    let path = "";
    for (let r = 0; r < count; r++) {
      for (let c = 0; c < count; c++) if (q.isDark(r, c)) path += `M${c} ${r}h1v1h-1z`;
    }
    return { d: path, n: count };
  }, [value]);
  const pad = 3;
  return (
    <svg viewBox={`${-pad} ${-pad} ${n + pad * 2} ${n + pad * 2}`} width={size} height={size} role="img" aria-label={label} className="shrink-0 rounded-lg" shapeRendering="crispEdges">
      <rect x={-pad} y={-pad} width={n + pad * 2} height={n + pad * 2} fill="#F4F3FF" />
      <path d={d} fill="#07090C" />
    </svg>
  );
}

/** A link on one quiet line with Copy (and Share, where the device has it). */
export function CopyLink({ url, label = "Invite link" }: { url: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const canShare = typeof navigator !== "undefined" && typeof navigator.share === "function";
  const copy = async () => {
    setFailed(false);
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2200);
    } catch {
      setFailed(true);
    }
  };
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-3 border-b border-os-ring">
        <label className="sr-only" htmlFor={`copy-${label}`}>
          {label}
        </label>
        <input id={`copy-${label}`} readOnly value={url} onFocus={(e) => e.currentTarget.select()} className="min-h-[44px] min-w-0 flex-1 truncate bg-transparent text-[0.875rem] text-os-muted outline-none" />
        <button type="button" onClick={() => void copy()} className={textBtnCls} aria-live="polite">
          {copied ? "Copied" : "Copy"}
        </button>
        {canShare ? (
          <button type="button" onClick={() => void navigator.share({ url }).catch(() => {})} className={textBtnCls}>
            Share
          </button>
        ) : null}
      </div>
      {failed ? <p className="text-[0.8125rem] text-os-muted">Couldn&rsquo;t copy automatically &mdash; select the link and copy it.</p> : null}
    </div>
  );
}
