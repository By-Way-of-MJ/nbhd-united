"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { ClusterPanel, ClustersSection, CreateClusterPanel } from "@/components/neighborhood/clusters";
import { InvitePanel } from "@/components/neighborhood/invite";
import { MessagesSection, ThreadPanel } from "@/components/neighborhood/messages";
import { NeedsYou } from "@/components/neighborhood/needs-you";
import { PeopleSky } from "@/components/neighborhood/people-sky";
import { CreateProjectPanel, ProjectPanel, ProjectsSection } from "@/components/neighborhood/projects";
import { ShareLessonPanel, ShareReviewPanel } from "@/components/neighborhood/share";
import { ghostBtnCls } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { getErrorMessage } from "@/lib/errors";
import { buildNeeds, countsLine, groupKeeps, keepsSummary, messageRows, type MessageRow, type Need } from "@/lib/neighborhood";
import {
  useAbsorbedQuery,
  useAcceptWaveMutation,
  useApproveGoalActionMutation,
  useCircleDetailsQueries,
  useCirclesQuery,
  useDeclineWaveMutation,
  useGoalActionsQuery,
  useJoinMissionMutation,
  useMissionAsksQuery,
  useMissionDetailsQueries,
  useNeighborhoodHomeQuery,
  useOpenThreadMutation,
  usePendingSharesQuery,
  useRejectGoalActionMutation,
  useSkyMembershipMutation,
  useThreadsQuery,
} from "@/lib/queries";
import type { ChatThread, CircleDetail, HomeNeighbor, MissionDetail, PendingShare } from "@/lib/types";

type Panel =
  | { kind: "project"; id: string }
  | { kind: "new-project" }
  | { kind: "cluster"; id: string; invite: boolean }
  | { kind: "new-cluster" }
  | { kind: "thread"; thread: ChatThread }
  | { kind: "review"; share: PendingShare }
  | { kind: "share"; friendshipId?: string; circleId?: string }
  | { kind: "invite" }
  | null;

/** Projects rows each read their crew projection; past this many, the rest load on open. */
const DETAIL_CAP = 12;

function readJoinCode(): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get("join")?.trim() ?? "";
}

/** The server owns the sky cap; its 409 body carries the number. */
function skyErrorMessage(err: unknown): string {
  try {
    const body = JSON.parse(err instanceof Error ? err.message : "") as { error?: string; cap?: number };
    if (body.error === "sky_full") {
      return typeof body.cap === "number" ? `Your sky holds ${body.cap} people. Take someone out first.` : "Your sky is full. Take someone out first.";
    }
  } catch {
    // Not a JSON body — fall through to the shared copy.
  }
  return getErrorMessage(err);
}

/**
 * Neighborhood (Open Sky): your people as a star cluster, one list of what's
 * waiting on you, Clusters and Projects side by side, Messages, and a few quiet
 * links. Everything else lives one step away, in its own panel or page.
 */
export function NeighborhoodPage() {
  const home = useNeighborhoodHomeQuery();
  const circlesQ = useCirclesQuery();
  const asksQ = useMissionAsksQuery();
  const sharesQ = usePendingSharesQuery();
  const actionsQ = useGoalActionsQuery();
  const threadsQ = useThreadsQuery();
  const absorbedQ = useAbsorbedQuery();

  const accept = useAcceptWaveMutation();
  const decline = useDeclineWaveMutation();
  const join = useJoinMissionMutation();
  const approveAction = useApproveGoalActionMutation();
  const rejectAction = useRejectGoalActionMutation();
  const sky = useSkyMembershipMutation();
  const openThread = useOpenThreadMutation();

  const [panel, setPanel] = useState<Panel>(null);
  const [skyError, setSkyError] = useState("");
  const [joinCode] = useState(readJoinCode);

  const neighbors = useMemo(() => home.data?.neighbors ?? [], [home.data]);
  const myHandle = home.data?.profile?.handle ?? null;
  const names = useMemo(() => new Map(neighbors.map((n) => [n.handle, n.display_name])), [neighbors]);
  const circles = useMemo(() => circlesQ.data ?? [], [circlesQ.data]);
  const circleIds = useMemo(() => circles.map((c) => c.circle_id), [circles]);
  const circleData = useCircleDetailsQueries(circleIds);
  const circleDetails = useMemo(() => {
    const m = new Map<string, CircleDetail>();
    circleData.forEach((d, i) => d && m.set(circleIds[i], d));
    return m;
  }, [circleIds, circleData]);

  const asks = useMemo(() => asksQ.data ?? [], [asksQ.data]);
  const projects = useMemo(() => asks.filter((m) => m.my_status === "active"), [asks]);
  const projectIds = useMemo(() => projects.slice(0, DETAIL_CAP).map((m) => m.mission_id), [projects]);
  const missionData = useMissionDetailsQueries(projectIds);
  const missionDetails = useMemo(() => {
    const m = new Map<string, MissionDetail>();
    missionData.forEach((d, i) => d && m.set(projectIds[i], d));
    return m;
  }, [projectIds, missionData]);

  const needs = useMemo(
    () => buildNeeds({ shares: sharesQ.data, waves: home.data?.pending_in, asks, actions: actionsQ.data }),
    [sharesQ.data, home.data?.pending_in, asks, actionsQ.data],
  );
  const rows = useMemo(() => messageRows(threadsQ.data ?? [], neighbors), [threadsQ.data, neighbors]);
  const keeps = useMemo(() => keepsSummary(groupKeeps(absorbedQ.data ?? [], names, myHandle)), [absorbedQ.data, names, myHandle]);

  const busyKey =
    accept.isPending ? `wave:${accept.variables}` :
    decline.isPending ? `wave:${decline.variables}` :
    join.isPending ? `ask:${join.variables?.id}` :
    approveAction.isPending ? `action:${approveAction.variables}` :
    rejectAction.isPending ? `action:${rejectAction.variables}` : null;

  const act = (n: Need) => {
    if (n.kind === "share") {
      const share = sharesQ.data?.find((s) => s.id === n.id);
      if (share) setPanel({ kind: "review", share });
    } else if (n.kind === "wave") accept.mutate(n.id, { onSuccess: () => emitToast("You’re neighbors now.", "success") });
    else if (n.kind === "ask") join.mutate({ id: n.id }, { onSuccess: () => emitToast("You’re in. It’s under Projects.", "success") });
    else if (n.kind === "action") approveAction.mutate(n.id, { onSuccess: () => emitToast("Added to your tasks.", "success") });
  };
  const dismiss = (n: Need) => {
    if (n.kind === "wave") decline.mutate(n.id);
    else if (n.kind === "action") rejectAction.mutate(n.id);
  };

  const messageNeighbor = async (n: HomeNeighbor) => {
    const existing = (threadsQ.data ?? []).find((t) => t.friendship_id === n.friendship_id);
    if (existing) {
      setPanel({ kind: "thread", thread: existing });
      return;
    }
    try {
      const r = await openThread.mutateAsync({ friendshipId: n.friendship_id });
      setPanel({
        kind: "thread",
        thread: { thread_id: r.thread_id, friendship_id: n.friendship_id, display_name: n.display_name, handle: n.handle, avatar_hue: n.avatar_hue, unread: 0, last_message: "", last_message_at: null, muted: false, agent_absorb_enabled: false },
      });
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };
  const openRow = (r: MessageRow) => {
    if (r.thread) setPanel({ kind: "thread", thread: r.thread });
    else if (r.neighbor) void messageNeighbor(r.neighbor);
  };
  const toggleSky = (n: HomeNeighbor) => {
    setSkyError("");
    sky.mutate({ friendshipId: n.friendship_id, inSky: !n.in_my_sky }, { onError: (err) => setSkyError(skyErrorMessage(err)) });
  };

  const close = () => setPanel(null);

  return (
    <div className="pb-16">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
        <div className="flex min-w-0 flex-col gap-2">
          <h1 className="os-page-title">Neighborhood</h1>
          <p className="text-[0.75rem] font-semibold uppercase tracking-[0.15em] text-os-faint">{countsLine(neighbors.length, circles.length, projects.length)}</p>
        </div>
        <button type="button" onClick={() => setPanel({ kind: "invite" })} className={`${ghostBtnCls} min-h-[44px] px-5 text-[0.875rem]`}>
          Invite someone
        </button>
      </header>

      <div className="mt-6">
        <PeopleSky
          neighbors={neighbors}
          circles={circles}
          circleDetails={circleDetails}
          reachTotal={home.data?.reach_total}
          loading={home.isLoading}
          error={home.error ? getErrorMessage(home.error) : ""}
          skyPending={sky.isPending}
          skyError={skyError}
          onMessage={(n) => void messageNeighbor(n)}
          onToggleSky={toggleSky}
          onShareLesson={(n) => setPanel({ kind: "share", friendshipId: n.friendship_id })}
          onOpenCluster={(id) => setPanel({ kind: "cluster", id, invite: false })}
          onInviteCluster={(id) => setPanel({ kind: "cluster", id, invite: true })}
          onInvite={() => setPanel({ kind: "invite" })}
        />
      </div>

      <div className="mt-10">
        <NeedsYou needs={needs} waitingOn={home.data?.pending_out ?? []} busyKey={busyKey} onAct={act} onDismiss={dismiss} />
      </div>

      <div className="mt-10 flex flex-col gap-10 lg:flex-row lg:gap-12">
        <ClustersSection
          circles={circles}
          details={circleDetails}
          loading={circlesQ.isLoading}
          joinCode={joinCode}
          onOpen={(id) => setPanel({ kind: "cluster", id, invite: false })}
          onCreate={() => setPanel({ kind: "new-cluster" })}
        />
        <ProjectsSection
          missions={projects}
          details={missionDetails}
          loading={asksQ.isLoading}
          myHandle={myHandle}
          names={names}
          onOpen={(id) => setPanel({ kind: "project", id })}
          onCreate={() => setPanel({ kind: "new-project" })}
        />
      </div>

      <div className="mt-10">
        <MessagesSection rows={rows} loading={threadsQ.isLoading} onOpen={openRow} />
      </div>

      <nav aria-label="More neighborhood settings" className="os-hairline-top mt-10 flex flex-wrap gap-x-7 gap-y-1 pt-3 text-[0.875rem]">
        <Link href="/friends/profile" className="os-focus flex min-h-[44px] items-center rounded text-os-muted transition hover:text-os-ink">
          Your profile
        </Link>
        <Link href="/friends/keeps" className="os-focus flex min-h-[44px] items-center rounded text-os-muted transition hover:text-os-ink">
          What your assistant keeps{keeps ? ` · ${keeps}` : ""}
        </Link>
        <Link href="/friends/manage" className="os-focus flex min-h-[44px] items-center rounded text-os-muted transition hover:text-os-ink">
          Manage neighbors
        </Link>
        <button type="button" onClick={() => setPanel({ kind: "share" })} className="os-focus flex min-h-[44px] items-center rounded text-os-muted transition hover:text-os-ink">
          Share a lesson
        </button>
      </nav>

      {panel?.kind === "project" ? <ProjectPanel missionId={panel.id} myHandle={myHandle} names={names} onClose={close} /> : null}
      {panel?.kind === "new-project" ? <CreateProjectPanel neighbors={neighbors} onClose={close} onCreated={(id) => setPanel({ kind: "project", id })} /> : null}
      {panel?.kind === "cluster" ? <ClusterPanel circleId={panel.id} focusInvite={panel.invite} onClose={close} onOpenChat={(thread) => setPanel({ kind: "thread", thread })} /> : null}
      {panel?.kind === "new-cluster" ? <CreateClusterPanel onClose={close} onCreated={(id) => setPanel({ kind: "cluster", id, invite: true })} /> : null}
      {panel?.kind === "thread" ? <ThreadPanel key={panel.thread.thread_id} thread={panel.thread} onClose={close} /> : null}
      {panel?.kind === "review" ? <ShareReviewPanel key={panel.share.id} share={panel.share} onClose={close} /> : null}
      {panel?.kind === "share" ? <ShareLessonPanel neighbors={neighbors} circles={circles} initial={panel} onClose={close} /> : null}
      {panel?.kind === "invite" ? <InvitePanel onClose={close} /> : null}
    </div>
  );
}
