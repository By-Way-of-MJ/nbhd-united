"use client";

import Link from "next/link";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { ThreadPanel } from "@/components/neighborhood/messages";
import { ErrorLine, fieldCls, ghostBtnCls, Initial, labelCls, quietBtnCls, dangerBtnCls, textBtnCls } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { getErrorMessage } from "@/lib/errors";
import { groupKeeps, keepMeta, plural } from "@/lib/neighborhood";
import {
  useAbsorbedQuery,
  useBlockWaveMutation,
  useNeighborhoodHomeQuery,
  useNeighborProfileQuery,
  useOpenThreadMutation,
  usePurgeAbsorbedMutation,
  useThreadsQuery,
  useUnfriendMutation,
  useUpdateNeighborProfileMutation,
} from "@/lib/queries";
import type { ChatThread, HomeNeighbor, NeighborProfile } from "@/lib/types";

/** Sub-page frame: back to Neighborhood, one serif title, a line of why. */
export function SubPage({ title, intro, children }: { title: string; intro?: ReactNode; children: ReactNode }) {
  return (
    <div className="max-w-[760px] pb-16">
      <Link href="/friends" className="os-focus -ml-1 inline-flex min-h-[40px] items-center gap-1.5 rounded px-1 text-[0.875rem] text-os-accent hover:text-white">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
          <path d="M15 6l-6 6 6 6" />
        </svg>
        Neighborhood
      </Link>
      <h1 className="os-page-title mt-2">{title}</h1>
      {intro ? <p className="mt-3 max-w-[640px] text-[0.9375rem] leading-relaxed text-os-muted">{intro}</p> : null}
      <div className="mt-7">{children}</div>
    </div>
  );
}

// ── What your assistant keeps ─────────────────────────────────────────────

export function KeepsPage() {
  const { data: items = [], isLoading, error } = useAbsorbedQuery();
  const home = useNeighborhoodHomeQuery();
  const purge = usePurgeAbsorbedMutation();
  const [busy, setBusy] = useState<string | null>(null);
  const names = useMemo(() => new Map((home.data?.neighbors ?? []).map((n) => [n.handle, n.display_name])), [home.data]);
  const groups = useMemo(() => groupKeeps(items, names, home.data?.profile?.handle ?? null), [items, names, home.data]);

  const remove = async (key: string, ids: string[]) => {
    setBusy(key);
    try {
      for (const id of ids) await purge.mutateAsync(id);
      emitToast(ids.length > 1 ? "Removed — your assistant will stop using them." : "Removed — your assistant will stop using it.", "success");
    } catch {
      // The failed one is restored by the mutation; the global toast explains.
    } finally {
      setBusy(null);
    }
  };

  return (
    <SubPage
      title="What your assistant keeps"
      intro="Things your neighbors chose to share, that your assistant can use when it helps you. Remove anything you'd rather it forget. They're never shown to anyone else."
    >
      {isLoading ? (
        <p className="text-[0.9375rem] text-os-muted">Loading&hellip;</p>
      ) : error ? (
        <ErrorLine>Couldn&rsquo;t load this. {getErrorMessage(error)}</ErrorLine>
      ) : groups.length === 0 ? (
        <p className="os-hairline-top pt-4 text-[0.9375rem] text-os-muted">Your assistant isn&rsquo;t keeping anything from your neighbors.</p>
      ) : (
        groups.map((g) => {
          const allIds = g.items.flatMap((i) => i.ids);
          return (
            <section key={g.key} aria-label={g.name} className="os-hairline-top py-4">
              <div className="flex items-baseline justify-between gap-4">
                <h2 className="os-serif text-[1.625rem] leading-tight text-white sm:text-[1.75rem]">{g.name}</h2>
                <span className="flex shrink-0 items-baseline gap-5">
                  <span className="text-[0.8125rem] text-os-faint">{plural(g.count, "note")}</span>
                  {g.items.length > 1 || g.count > 1 ? (
                    <button type="button" className={dangerBtnCls} disabled={!!busy} onClick={() => void remove(`g:${g.key}`, allIds)}>
                      {busy === `g:${g.key}` ? "Removing…" : "Remove all"}
                    </button>
                  ) : null}
                </span>
              </div>
              <ul className="mt-1">
                {g.items.map((it) => (
                  <li key={it.key} className="flex items-start gap-4 border-t border-os-hairline py-3">
                    <span className="flex min-w-0 flex-1 flex-col gap-1">
                      <span className="text-[0.9375rem] leading-relaxed text-white">{it.text}</span>
                      <span className="text-[0.75rem] text-os-faint">{keepMeta(it)}</span>
                    </span>
                    <button type="button" className={quietBtnCls} disabled={!!busy} onClick={() => void remove(it.key + g.key, it.ids)}>
                      {busy === it.key + g.key ? "Removing…" : it.ids.length > 1 ? `Remove all ${it.ids.length}` : "Remove"}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          );
        })
      )}
    </SubPage>
  );
}

// ── Your profile ──────────────────────────────────────────────────────────

export function ProfilePage() {
  const { data: profile, isLoading } = useNeighborProfileQuery();
  const save = useUpdateNeighborProfileMutation();
  const [handle, setHandle] = useState("");
  const [bio, setBio] = useState("");
  const [hue, setHue] = useState(210);
  const [saved, setSaved] = useState({ handle: "", bio: "", hue: 210 });
  const [seededFrom, setSeededFrom] = useState<string | null>(null);
  const [handleError, setHandleError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [justSaved, setJustSaved] = useState(false);

  // Seed drafts from the server during render (not an effect) so a background
  // refetch returning the same values never clobbers typing.
  const incoming = profile ? `${profile.handle} ${profile.bio} ${profile.avatar_hue}` : null;
  if (incoming !== null && incoming !== seededFrom && profile) {
    setSeededFrom(incoming);
    setHandle(profile.handle);
    setBio(profile.bio);
    setHue(profile.avatar_hue);
    setSaved({ handle: profile.handle, bio: profile.bio, hue: profile.avatar_hue });
  }
  const dirty = handle.trim() !== saved.handle || bio !== saved.bio || hue !== saved.hue;

  const submit = async () => {
    setHandleError("");
    setSaveError("");
    try {
      const patch: Partial<NeighborProfile> = { bio, avatar_hue: hue };
      if (handle.trim() !== saved.handle) patch.handle = handle.trim().toLowerCase();
      const u = await save.mutateAsync(patch);
      setSeededFrom(`${u.handle} ${u.bio} ${u.avatar_hue}`);
      setHandle(u.handle);
      setBio(u.bio);
      setHue(u.avatar_hue);
      setSaved({ handle: u.handle, bio: u.bio, hue: u.avatar_hue });
      setJustSaved(true);
      window.setTimeout(() => setJustSaved(false), 2600);
    } catch (err) {
      const msg = getErrorMessage(err);
      if (msg.toLowerCase().includes("handle")) setHandleError(msg);
      else setSaveError(msg);
    }
  };

  return (
    <SubPage title="Your profile" intro="How your neighbors see you. Only people you've both agreed to know can see it.">
      {isLoading || !profile ? (
        <p className="text-[0.9375rem] text-os-muted">Loading&hellip;</p>
      ) : (
        <div className="flex max-w-[520px] flex-col gap-7">
          <div className="flex items-center gap-5">
            <Initial name={profile.display_name || handle} hue={hue} size={56} />
            <div className="flex min-w-0 flex-col">
              <span className="os-serif truncate text-[1.75rem] leading-tight text-white">{profile.display_name || `@${handle}`}</span>
              <span className="text-[0.8125rem] text-os-faint">@{handle || "handle"}</span>
            </div>
          </div>
          <label className="flex flex-col gap-1">
            <span className={labelCls}>Handle</span>
            <span className="flex items-center border-b border-os-ring focus-within:border-os-accent">
              <span className="text-os-faint" aria-hidden="true">@</span>
              <input value={handle} onChange={(e) => setHandle(e.target.value.toLowerCase())} maxLength={30} autoCapitalize="none" autoCorrect="off" className="min-h-[44px] w-full bg-transparent pl-1 text-[0.9375rem] text-os-ink outline-none placeholder:text-os-faint focus-visible:outline-none" />
            </span>
            {handleError ? <ErrorLine>{handleError}</ErrorLine> : null}
          </label>
          <label className="flex flex-col gap-1">
            <span className={labelCls}>
              A line about you <span className="normal-case tracking-normal text-os-faint">({bio.length}/280)</span>
            </span>
            <textarea value={bio} onChange={(e) => setBio(e.target.value.slice(0, 280))} rows={3} maxLength={280} placeholder="A line or two about you…" className={`${fieldCls} resize-none py-2.5`} />
          </label>
          <label className="flex flex-col gap-2">
            <span className={labelCls}>Your colour</span>
            <input type="range" min={0} max={359} value={hue} onChange={(e) => setHue(Number(e.target.value))} className="w-full accent-[var(--os-accent)]" aria-label="Your colour" />
          </label>
          {saveError ? <ErrorLine>{saveError}</ErrorLine> : null}
          <div className="flex items-center gap-4">
            <button type="button" className={ghostBtnCls} disabled={!dirty || !handle.trim() || save.isPending} onClick={() => void submit()}>
              {save.isPending ? "Saving…" : "Save"}
            </button>
            <span className="text-[0.8125rem] text-os-faint" role="status" aria-live="polite">
              {justSaved ? "Saved." : ""}
            </span>
          </div>
        </div>
      )}
    </SubPage>
  );
}

// ── Manage neighbors ──────────────────────────────────────────────────────

function RowMenu({ label, onUnfriend, onBlock, blocking }: { label: string; onUnfriend: () => void; onBlock: () => void; blocking: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  return (
    <div className="relative shrink-0" ref={ref}>
      <button type="button" onClick={() => setOpen((o) => !o)} aria-label={label} aria-expanded={open} className="os-focus flex h-11 w-11 items-center justify-center rounded-full text-os-faint hover:text-os-ink">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="5" cy="12" r="1.5" />
          <circle cx="12" cy="12" r="1.5" />
          <circle cx="19" cy="12" r="1.5" />
        </svg>
      </button>
      {open ? (
        <div role="menu" className="absolute right-0 top-full z-20 mt-1 w-44 rounded-2xl border border-os-hairline bg-os-surface-solid p-1.5">
          <button type="button" role="menuitem" onClick={() => { setOpen(false); onUnfriend(); }} className="os-focus flex min-h-[44px] w-full items-center rounded-xl px-3 text-left text-[0.875rem] text-os-ink hover:bg-os-accent-soft">
            Unfriend
          </button>
          <button type="button" role="menuitem" disabled={blocking} onClick={() => { setOpen(false); onBlock(); }} className="os-focus flex min-h-[44px] w-full items-center rounded-xl px-3 text-left text-[0.875rem] text-os-danger hover:bg-os-accent-soft disabled:opacity-40">
            Block
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function ManagePage() {
  const home = useNeighborhoodHomeQuery();
  const { data: threads = [] } = useThreadsQuery();
  const unfriend = useUnfriendMutation();
  const block = useBlockWaveMutation();
  const openThread = useOpenThreadMutation();
  const [confirm, setConfirm] = useState<HomeNeighbor | null>(null);
  const [confirmBlock, setConfirmBlock] = useState<HomeNeighbor | null>(null);
  const [thread, setThread] = useState<ChatThread | null>(null);
  const neighbors = useMemo(() => [...(home.data?.neighbors ?? [])].sort((a, b) => a.display_name.localeCompare(b.display_name)), [home.data]);
  const waiting = home.data?.pending_out ?? [];

  const message = async (n: HomeNeighbor) => {
    const existing = threads.find((t) => t.friendship_id === n.friendship_id);
    if (existing) return setThread(existing);
    try {
      const r = await openThread.mutateAsync({ friendshipId: n.friendship_id });
      setThread({ thread_id: r.thread_id, friendship_id: n.friendship_id, display_name: n.display_name, handle: n.handle, avatar_hue: n.avatar_hue, unread: 0, last_message: "", last_message_at: null, muted: false, agent_absorb_enabled: false });
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };

  return (
    <SubPage title="Manage neighbors" intro="Everyone you've both agreed to know. Unfriending is quiet — they aren't told. Blocking also stops them waving again.">
      {home.isLoading ? (
        <p className="text-[0.9375rem] text-os-muted">Loading&hellip;</p>
      ) : neighbors.length === 0 ? (
        <p className="os-hairline-top pt-4 text-[0.9375rem] text-os-muted">No neighbors yet.</p>
      ) : (
        <>
          <p className="pb-2 text-[0.75rem] font-semibold uppercase tracking-[0.15em] text-os-faint">{plural(neighbors.length, "neighbor")}</p>
          <ul>
            {neighbors.map((n) => (
              <li key={n.friendship_id} className="os-hairline-top flex min-h-[64px] flex-wrap items-center gap-x-3.5 gap-y-2 py-2.5">
                <Initial name={n.display_name} hue={n.avatar_hue} />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-[0.9375rem] text-white">{n.display_name}</span>
                  <span className="truncate text-[0.75rem] text-os-faint">
                    @{n.handle} &middot; {n.in_my_sky ? "in your sky" : `friends since ${n.friends_since.slice(0, 4)}`}
                  </span>
                </span>
                {confirm?.friendship_id === n.friendship_id || confirmBlock?.friendship_id === n.friendship_id ? (
                  <span className="flex w-full items-center justify-end gap-4 sm:w-auto">
                    <span className="text-[0.8125rem] text-os-muted">{confirm ? `Unfriend ${n.display_name.split(/\s+/)[0]}?` : `Block ${n.display_name.split(/\s+/)[0]}?`}</span>
                    <button
                      type="button"
                      className={dangerBtnCls}
                      onClick={() => {
                        if (confirm) unfriend.mutate(n.friendship_id);
                        else block.mutate(n.friendship_id);
                        setConfirm(null);
                        setConfirmBlock(null);
                      }}
                    >
                      {confirm ? "Unfriend" : "Block"}
                    </button>
                    <button type="button" className={quietBtnCls} onClick={() => { setConfirm(null); setConfirmBlock(null); }}>
                      Cancel
                    </button>
                  </span>
                ) : (
                  <>
                    <button type="button" className={textBtnCls} onClick={() => void message(n)}>
                      Message
                    </button>
                    <RowMenu label={`Actions for ${n.display_name}`} onUnfriend={() => setConfirm(n)} onBlock={() => setConfirmBlock(n)} blocking={block.isPending && block.variables === n.friendship_id} />
                  </>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {waiting.length ? (
        <div className="mt-8">
          <p className="pb-2 text-[0.75rem] font-semibold uppercase tracking-[0.15em] text-os-faint">Waiting on them</p>
          <ul>
            {waiting.map((w) => (
              <li key={w.friendship_id} className="os-hairline-top flex min-h-[52px] items-center gap-3.5 py-2">
                <Initial name={w.display_name || w.handle} hue={w.avatar_hue} size={32} />
                <span className="min-w-0 flex-1 truncate text-[0.9375rem] text-os-ink">
                  {w.display_name} <span className="text-[0.75rem] text-os-faint">@{w.handle}</span>
                </span>
                <span className="text-[0.75rem] text-os-faint">You waved</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {thread ? <ThreadPanel key={thread.thread_id} thread={thread} onClose={() => setThread(null)} /> : null}
    </SubPage>
  );
}
