"use client";

import clsx from "clsx";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { ClusterConversation } from "@/components/neighborhood/cluster/conversation";
import { ThreadPanel } from "@/components/neighborhood/messages";
import { BackToNeighborhood } from "@/components/neighborhood/project/project-page";
import { PillTabs, rowHoverCls, sectionLabelCls, useMediaQuery, useMounted } from "@/components/neighborhood/project/parts";
import { CopyLink, dangerBtnCls, ErrorLine, ghostBtnCls, Initial, PanelHeader, QrCode, quietBtnCls, SidePanel } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { clusterEyebrow, LEAVE_CLUSTER_NOTE, memberRelation, memberRole, ringPoints, sortedMembers } from "@/lib/cluster";
import { getErrorMessage } from "@/lib/errors";
import { clusterInviteUrl } from "@/lib/neighborhood";
import { clusterCss } from "@/lib/people-sky/renderer";
import {
  useAddCircleMemberMutation,
  useCircleDetailQuery,
  useLeaveCircleMutation,
  useNeighborhoodHomeQuery,
  useOpenThreadMutation,
  usePatchMembershipMutation,
  useRegenerateInviteCodeMutation,
  useRemoveCircleMemberMutation,
  useSendWaveMutation,
  useThreadsQuery,
} from "@/lib/queries";
import type { ChatThread, CircleDetail, CircleMember, HomeNeighbor } from "@/lib/types";

type Tab = "talk" | "people";

/**
 * A cluster on its own page (Circles in the API): the conversation where
 * people ask, offer and share what they've learned; who's in it; the
 * invitation; and your own choices about it.
 */
export function ClusterPage() {
  const params = useSearchParams();
  const router = useRouter();
  const circleId = params.get("id") ?? "";
  const { data: circle, isLoading, isError } = useCircleDetailQuery(circleId || null);
  const home = useNeighborhoodHomeQuery();
  const wide = useMediaQuery("(min-width: 1024px)");
  const [tab, setTab] = useState<Tab>(params.get("invite") ? "people" : "talk");
  const [person, setPerson] = useState<CircleMember | null>(null);
  const [thread, setThread] = useState<ChatThread | null>(null);
  const neighbors = useMemo(() => home.data?.neighbors ?? [], [home.data]);
  const mounted = useMounted();

  if (mounted && (!circleId || (!circle && !isLoading))) {
    return (
      <div className="pb-16">
        <BackToNeighborhood />
        <h1 className="os-page-title mt-2">Cluster</h1>
        <p className="mt-4 text-[0.9375rem] text-os-muted">{isError ? "This cluster isn’t available right now." : "Choose a cluster from your Neighborhood to open it."}</p>
      </div>
    );
  }
  if (!mounted || !circle) {
    return (
      <div className="pb-16">
        <BackToNeighborhood />
        <p className="mt-6 text-[0.9375rem] text-os-muted">Loading&hellip;</p>
      </div>
    );
  }

  const isAdmin = circle.my_role === "admin";
  const people = (
    <ClusterPeople
      circle={circle}
      neighbors={neighbors}
      focusInvite={!!params.get("invite")}
      onPerson={setPerson}
      onLeft={(message) => {
        emitToast(message, "success");
        router.push("/friends");
      }}
    />
  );

  return (
    <div className="pb-16">
      <BackToNeighborhood />
      <header className="mt-1 flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
        <div className="flex min-w-0 flex-col gap-2">
          <p className={clsx(sectionLabelCls, "flex items-center gap-2")}>
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: clusterCss(circle.hue) }} aria-hidden="true" />
            {clusterEyebrow(circle.members.length, isAdmin)}
          </p>
          <h1 className="os-page-title break-words">{circle.name}</h1>
          <p className="max-w-[640px] text-[0.9375rem] leading-relaxed text-os-muted">{circle.description || "A place to show up for one another."}</p>
        </div>
        {!wide ? (
          <PillTabs
            label="View"
            value={tab}
            onChange={setTab}
            options={[
              { value: "talk", label: "Conversation" },
              { value: "people", label: "People" },
            ]}
          />
        ) : null}
      </header>

      <div className="os-hairline-top mt-5 pt-5 lg:grid lg:grid-cols-[minmax(0,1fr)_340px] lg:gap-12">
        {wide || tab === "talk" ? (
          <div className="min-w-0">
            {circle.thread_id ? (
              <ClusterConversation key={circle.thread_id} threadId={circle.thread_id} clusterName={circle.name} />
            ) : (
              <p className="text-[0.9375rem] text-os-muted">This cluster&rsquo;s conversation isn&rsquo;t available right now.</p>
            )}
          </div>
        ) : null}
        {wide || tab === "people" ? <div className="min-w-0 lg:border-l lg:border-os-hairline lg:pl-8">{people}</div> : null}
      </div>

      {person ? (
        <PersonPanel
          circle={circle}
          person={person}
          neighbors={neighbors}
          onClose={() => setPerson(null)}
          onOpenThread={(t) => {
            setPerson(null);
            setThread(t);
          }}
        />
      ) : null}
      {thread ? <ThreadPanel key={thread.thread_id} thread={thread} onClose={() => setThread(null)} /> : null}
    </div>
  );
}

/** A switch with its sentence: on is the accent, off is quiet. */
function Choice({ label, detail, checked, disabled, onChange }: { label: string; detail?: string; checked: boolean; disabled?: boolean; onChange: (next: boolean) => void }) {
  return (
    <button type="button" role="switch" aria-checked={checked} disabled={disabled} onClick={() => onChange(!checked)} className="os-focus flex min-h-[48px] w-full items-center gap-4 rounded py-2 text-left disabled:opacity-50">
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-[0.9375rem] text-os-ink">{label}</span>
        {detail ? <span className="text-[0.75rem] leading-relaxed text-os-faint">{detail}</span> : null}
      </span>
      <span aria-hidden="true" className={clsx("relative h-[22px] w-[40px] shrink-0 rounded-full border transition", checked ? "border-os-accent bg-os-accent-soft" : "border-os-ring")}>
        <span className={clsx("absolute top-[3px] h-[14px] w-[14px] rounded-full transition-all", checked ? "left-[21px] bg-os-accent" : "left-[3px] bg-os-faint")} />
      </span>
    </button>
  );
}

/**
 * Who shares this cluster, drawn as it is: each person joined to the shared
 * purpose, never to each other. Up to six; everyone is in the list below.
 */
function SharedMap({ circle, members, onPerson }: { circle: CircleDetail; members: CircleMember[]; onPerson: (m: CircleMember) => void }) {
  const shown = members.slice(0, 6);
  const W = 300, H = 232, cx = 150, cy = 116, r = 86;
  const points = ringPoints(shown.length, cx, cy, r);
  const color = clusterCss(circle.hue);
  return (
    <div className="relative mx-auto" style={{ width: W, height: H }}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} aria-hidden="true" className="absolute left-0 top-0">
        {points.map((p, i) => (
          <line key={i} x1={cx} y1={cy} x2={p.x} y2={p.y} strokeDasharray="3 5" strokeOpacity={0.55} style={{ stroke: color }} />
        ))}
        <circle cx={cx} cy={cy} r={30} strokeOpacity={0.7} style={{ fill: "var(--os-sky)", stroke: color }} />
      </svg>
      <span className="absolute flex h-[60px] w-[60px] -translate-x-1/2 -translate-y-1/2 items-center justify-center text-center text-[0.625rem] font-semibold uppercase leading-tight tracking-[0.08em] text-os-label" style={{ left: cx, top: cy }} aria-hidden="true">
        Shared
        <br />
        purpose
      </span>
      {shown.map((m, i) => (
        <button
          key={m.handle ?? `m-${i}`}
          type="button"
          onClick={() => onPerson(m)}
          aria-label={`${m.is_me ? "You" : m.display_name}, connected through ${circle.name}`}
          className="os-focus absolute flex w-[76px] -translate-x-1/2 -translate-y-1/2 flex-col items-center gap-1 rounded-xl"
          style={{ left: points[i].x, top: points[i].y }}
        >
          <span className="rounded-full bg-os-sky">
            <Initial name={m.is_me ? "You" : m.display_name} hue={m.avatar_hue} size={34} />
          </span>
          <span className="max-w-full truncate rounded-full bg-os-sky px-1.5 text-[0.6875rem] text-os-muted">{m.is_me ? "You" : m.display_name.split(/\s+/)[0]}</span>
        </button>
      ))}
    </div>
  );
}

function ClusterPeople({ circle, neighbors, focusInvite, onPerson, onLeft }: { circle: CircleDetail; neighbors: HomeNeighbor[]; focusInvite: boolean; onPerson: (m: CircleMember) => void; onLeft: (message: string) => void }) {
  const regenerate = useRegenerateInviteCodeMutation();
  const leave = useLeaveCircleMutation();
  const add = useAddCircleMemberMutation();
  const { data: threads = [] } = useThreadsQuery();
  const membership = usePatchMembershipMutation(circle.thread_id ?? "");
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [adding, setAdding] = useState("");
  const [addError, setAddError] = useState("");
  const inviteRef = useRef<HTMLElement>(null);
  const isAdmin = circle.my_role === "admin";
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const members = sortedMembers(circle.members);
  const host = circle.members.find((m) => m.role === "admin" && !m.is_me);
  const mine = threads.find((t) => t.thread_id === circle.thread_id);
  const inCluster = new Set(circle.members.map((m) => m.handle?.toLowerCase()).filter(Boolean));
  const addable = neighbors.filter((n) => !inCluster.has(n.handle.toLowerCase())).sort((a, b) => a.display_name.localeCompare(b.display_name));

  useEffect(() => {
    if (focusInvite) inviteRef.current?.scrollIntoView({ block: "center" });
  }, [focusInvite]);

  const doLeave = (keep: boolean) =>
    leave.mutate(
      { id: circle.circle_id, keep },
      { onSuccess: (result) => onLeft(result.purged ? "Left the cluster — what your assistant learned there was deleted." : "Left the cluster."), onError: () => setConfirmLeave(false) },
    );
  const submitAdd = (e: FormEvent) => {
    e.preventDefault();
    const n = addable.find((x) => x.handle === adding);
    if (!n || add.isPending) return;
    setAddError("");
    add.mutate(
      { id: circle.circle_id, handle: n.handle },
      {
        onSuccess: () => {
          emitToast(`${n.display_name} is in the cluster.`, "success");
          setAdding("");
        },
        onError: (err) => setAddError(getErrorMessage(err)),
      },
    );
  };

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="cluster-people-heading" className="flex flex-col gap-2">
        <h2 id="cluster-people-heading" className={sectionLabelCls}>
          Connected through this cluster
        </h2>
        <SharedMap circle={circle} members={members} onPerson={onPerson} />
        <p className="text-[0.8125rem] leading-relaxed text-os-faint">These lines show shared membership. A direct connection begins when two people choose it.</p>
        <ul className="mt-1">
          {members.map((m, i) => (
            <li key={m.handle ?? `m-${i}`}>
              <button type="button" onClick={() => onPerson(m)} className={clsx("os-focus flex min-h-[52px] w-full items-center gap-3 border-t border-os-hairline px-1 text-left", rowHoverCls)}>
                <Initial name={m.is_me ? "You" : m.display_name} hue={m.avatar_hue} size={32} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[0.9375rem] text-os-ink">{m.is_me ? "You" : m.display_name}</span>
                  <span className="block truncate text-[0.75rem] text-os-faint">{memberRole(m)}</span>
                </span>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="shrink-0 text-os-faint" aria-hidden="true">
                  <path d="M9 6l6 6-6 6" />
                </svg>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section ref={inviteRef} aria-labelledby="cluster-invite-heading" className="flex flex-col gap-3 border-t border-os-hairline pt-4">
        <div className="flex items-baseline justify-between gap-3">
          <h2 id="cluster-invite-heading" className={sectionLabelCls}>
            Make room for someone
          </h2>
          {isAdmin && circle.invite_code ? (
            <button type="button" className={quietBtnCls} disabled={regenerate.isPending} onClick={() => regenerate.mutate(circle.circle_id)}>
              {regenerate.isPending ? "Making a new code…" : "New code"}
            </button>
          ) : null}
        </div>
        {isAdmin && circle.invite_code ? (
          <>
            <p className="text-[0.875rem] leading-relaxed text-os-muted">Share this with a neighbor you&rsquo;re connected with. They choose whether to join; joining makes them visible to the cluster. A new code stops the old one working.</p>
            <div className="flex items-center gap-4">
              <QrCode value={clusterInviteUrl(origin, circle.invite_code)} size={112} label={`QR code to join ${circle.name}`} />
              <p className="text-[0.8125rem] text-os-faint">
                Code
                <br />
                <span className="select-all font-mono text-[0.9375rem] text-os-ink">{circle.invite_code}</span>
              </p>
            </div>
            <CopyLink url={clusterInviteUrl(origin, circle.invite_code)} label="Cluster invite link" />
          </>
        ) : (
          <p className="text-[0.875rem] leading-relaxed text-os-muted">
            {host ? `${host.display_name.split(/\s+/)[0]} hosts this cluster — ask them for the invite link.` : "Only the host can share this cluster’s invite link."}
          </p>
        )}
        {addable.length ? (
          <form onSubmit={submitAdd} className="flex flex-col gap-1.5">
            <div className="flex items-end gap-3">
              <label className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="text-[0.75rem] text-os-faint">Or bring in one of your neighbors</span>
                <select value={adding} onChange={(e) => setAdding(e.target.value)} className="os-focus min-h-[44px] w-full border-0 border-b border-os-ring bg-os-sky text-[0.9375rem] text-os-ink outline-none">
                  <option value="">Choose a neighbor&hellip;</option>
                  {addable.map((n) => (
                    <option key={n.friendship_id} value={n.handle}>
                      {n.display_name} (@{n.handle})
                    </option>
                  ))}
                </select>
              </label>
              <button type="submit" className={ghostBtnCls} disabled={!adding || add.isPending}>
                {add.isPending ? "Adding…" : "Add"}
              </button>
            </div>
            {addError ? <ErrorLine>{addError}</ErrorLine> : null}
          </form>
        ) : null}
      </section>

      {circle.thread_id ? (
        <section aria-labelledby="cluster-choices-heading" className="flex flex-col border-t border-os-hairline pt-4">
          <h2 id="cluster-choices-heading" className={clsx(sectionLabelCls, "pb-1")}>
            Your choices here
          </h2>
          <Choice
            label="Let my assistant learn from this conversation"
            detail="Turning it off stops your assistant keeping anything new from here. It doesn’t delete the cluster’s messages or change anyone else’s settings."
            checked={!!mine?.agent_absorb_enabled}
            disabled={!mine || membership.isPending}
            onChange={(next) => membership.mutate({ agent_absorb_enabled: next })}
          />
          <Choice label="Mute notifications" checked={!!mine?.muted} disabled={!mine || membership.isPending} onChange={(next) => membership.mutate({ muted: next })} />
        </section>
      ) : null}

      <section aria-label="Leave" className="flex flex-col gap-2 border-t border-os-hairline pt-4">
        {confirmLeave ? (
          <>
            <p className="text-[0.9375rem] text-os-ink">Leave {circle.name}?</p>
            <p className="text-[0.875rem] leading-relaxed text-os-muted">{LEAVE_CLUSTER_NOTE} What should happen to anything your assistant already learned here?</p>
            <div className="flex flex-wrap gap-x-5 gap-y-1 pt-1">
              <button type="button" className={dangerBtnCls} disabled={leave.isPending} onClick={() => doLeave(false)}>
                {leave.isPending ? "Leaving…" : "Leave and delete what it learned"}
              </button>
              <button type="button" className={quietBtnCls} disabled={leave.isPending} onClick={() => doLeave(true)}>
                Leave, but keep it
              </button>
              <button type="button" className={quietBtnCls} disabled={leave.isPending} onClick={() => setConfirmLeave(false)}>
                Cancel
              </button>
            </div>
          </>
        ) : (
          <button type="button" className={`${dangerBtnCls} self-start`} onClick={() => setConfirmLeave(true)}>
            Leave cluster
          </button>
        )}
      </section>
    </div>
  );
}

/**
 * One person in the cluster: say hello if you're already neighbors, send a
 * wave if you aren't (they decide), and — for the host — take them out.
 */
function PersonPanel({ circle, person, neighbors, onClose, onOpenThread }: { circle: CircleDetail; person: CircleMember; neighbors: HomeNeighbor[]; onClose: () => void; onOpenThread: (thread: ChatThread) => void }) {
  const { data: threads = [] } = useThreadsQuery();
  const openThread = useOpenThreadMutation();
  const wave = useSendWaveMutation();
  const remove = useRemoveCircleMemberMutation();
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");
  const [confirmRemove, setConfirmRemove] = useState(false);
  const relation = memberRelation(person, neighbors);
  const neighbor = neighbors.find((n) => n.handle.toLowerCase() === person.handle?.toLowerCase());
  const name = person.is_me ? "You" : person.display_name;
  const first = person.display_name.split(/\s+/)[0];

  const hello = async () => {
    if (!neighbor) return;
    const existing = threads.find((t) => t.friendship_id === neighbor.friendship_id);
    if (existing) return onOpenThread(existing);
    try {
      const r = await openThread.mutateAsync({ friendshipId: neighbor.friendship_id });
      onOpenThread({ thread_id: r.thread_id, friendship_id: neighbor.friendship_id, display_name: neighbor.display_name, handle: neighbor.handle, avatar_hue: neighbor.avatar_hue, unread: 0, last_message: "", last_message_at: null, muted: false, agent_absorb_enabled: false });
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };
  const sendWave = async () => {
    if (!person.handle) return;
    setError("");
    try {
      await wave.mutateAsync({ handle: person.handle });
      setSent(true);
    } catch (err) {
      setError(getErrorMessage(err) || "This person can’t be reached right now.");
    }
  };

  return (
    <SidePanel label={name} onClose={onClose}>
      <div className="flex flex-1 flex-col gap-5">
        <PanelHeader onClose={onClose} eyebrow={`Common ground · ${memberRole(person)}`} title={name} />
        <div className="flex items-center gap-3">
          <Initial name={name} hue={person.avatar_hue} size={48} />
          <span className="text-[0.8125rem] text-os-faint">{person.handle ? `@${person.handle}` : "no handle yet"}</span>
        </div>
        {relation === "me" ? (
          <p className="text-[0.9375rem] leading-relaxed text-os-muted">You belong here, whether you&rsquo;re offering support or asking for it.</p>
        ) : (
          <p className="text-[0.9375rem] leading-relaxed text-os-muted">You share {circle.name}. Start with the purpose that brought you here, and leave room for their story.</p>
        )}
        {relation === "neighbor" ? (
          <button type="button" className={`${ghostBtnCls} self-start`} disabled={openThread.isPending} onClick={() => void hello()} data-autofocus>
            {openThread.isPending ? "Opening…" : "Say hello"}
          </button>
        ) : null}
        {relation === "member" && person.handle ? (
          <>
            <p className="text-[0.8125rem] leading-relaxed text-os-faint">A direct connection is separate from the cluster. Send a wave; they decide whether to accept.</p>
            <button type="button" className={`${ghostBtnCls} self-start`} disabled={wave.isPending || sent} onClick={() => void sendWave()} data-autofocus>
              {sent ? "Wave sent" : wave.isPending ? "Sending…" : "Send a wave"}
            </button>
          </>
        ) : null}
        {error ? <ErrorLine>{error}</ErrorLine> : null}

        {circle.my_role === "admin" && !person.is_me && person.handle ? (
          <div className="mt-auto flex flex-col gap-2 border-t border-os-hairline pt-4">
            {confirmRemove ? (
              <>
                <p className="text-[0.875rem] text-os-muted">Take {first} out of {circle.name}? They lose its conversation and shares.</p>
                <div className="flex gap-5">
                  <button
                    type="button"
                    className={dangerBtnCls}
                    disabled={remove.isPending}
                    onClick={() =>
                      remove.mutate(
                        { id: circle.circle_id, handle: person.handle as string },
                        {
                          onSuccess: () => {
                            emitToast(`Removed ${first}.`, "success");
                            onClose();
                          },
                        },
                      )
                    }
                  >
                    {remove.isPending ? "Removing…" : `Remove ${first}`}
                  </button>
                  <button type="button" className={quietBtnCls} disabled={remove.isPending} onClick={() => setConfirmRemove(false)}>
                    Keep them
                  </button>
                </div>
              </>
            ) : (
              <button type="button" className={`${dangerBtnCls} self-start`} onClick={() => setConfirmRemove(true)}>
                Remove from cluster
              </button>
            )}
          </div>
        ) : null}
      </div>
    </SidePanel>
  );
}
