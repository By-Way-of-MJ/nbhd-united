"use client";

import { ghostBtnCls, quietBtnCls, SectionHead } from "@/components/neighborhood/ui";
import type { Need } from "@/lib/neighborhood";
import type { HomeWave } from "@/lib/types";

/**
 * Everything waiting on you, in one list: a full sentence and one action each
 * (plus a quiet way out where the flow has one). "All clear" when empty.
 */
export function NeedsYou({
  needs,
  waitingOn,
  busyKey,
  onAct,
  onDismiss,
}: {
  needs: Need[];
  waitingOn: HomeWave[];
  busyKey: string | null;
  onAct: (need: Need) => void;
  onDismiss: (need: Need) => void;
}) {
  return (
    <section aria-labelledby="needs-heading" className="flex flex-col">
      <SectionHead id="needs-heading" label="Needs you" trailing={needs.length ? <span className="text-[0.8125rem] text-os-faint">{needs.length} waiting</span> : null} />
      {needs.length === 0 ? (
        <p className="os-hairline-top py-3.5 text-[0.9375rem] text-os-muted">All clear. Nothing waiting on you.</p>
      ) : (
        <ul>
          {needs.map((n) => {
            const busy = busyKey === n.key;
            return (
              <li key={n.key} className="os-hairline-top flex flex-col gap-2 py-3.5 sm:flex-row sm:items-center sm:gap-5">
                <span className="shrink-0 text-[0.75rem] text-os-faint sm:w-[124px] sm:truncate" title={n.tag}>
                  {n.tag}
                </span>
                <p className="min-w-0 flex-1 text-[0.9375rem] leading-relaxed text-os-ink">{n.text}</p>
                <div className="flex shrink-0 items-center gap-3">
                  {n.dismiss ? (
                    <button type="button" className={quietBtnCls} disabled={busy} onClick={() => onDismiss(n)}>
                      {n.dismiss}
                    </button>
                  ) : null}
                  <button type="button" className={ghostBtnCls} disabled={busy} onClick={() => onAct(n)}>
                    {busy ? "Working…" : n.action}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {waitingOn.length ? (
        <p className="os-hairline-top pt-3 text-[0.8125rem] text-os-faint">
          You waved at {waitingOn.map((w) => w.display_name || `@${w.handle}`).join(", ")} &mdash; waiting on them.
        </p>
      ) : null}
    </section>
  );
}
