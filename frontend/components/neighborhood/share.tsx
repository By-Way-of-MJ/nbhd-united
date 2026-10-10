"use client";

import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ErrorLine, fieldCls, ghostBtnCls, labelCls, PanelHeader, quietBtnCls, SidePanel, dangerBtnCls } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { fetchCircleSharePreview, fetchSharePreview } from "@/lib/api";
import { getErrorMessage } from "@/lib/errors";
import { useApproveShareMutation, useApprovedLessonsQuery, useRejectShareMutation, useShareLessonMutation } from "@/lib/queries";
import type { CircleSummary, HomeNeighbor, PendingShare, SharePreview } from "@/lib/types";

/**
 * The trust surface: exactly what the neighbor will see, already scrubbed,
 * before anything goes out. Approve, edit (re-checked) or reject.
 */
export function ShareReviewPanel({ share, onClose }: { share: PendingShare; onClose: () => void }) {
  const approve = useApproveShareMutation();
  const reject = useRejectShareMutation();
  const [phase, setPhase] = useState<"loading" | "ready" | "failed">("loading");
  const [preview, setPreview] = useState<SharePreview | null>(null);
  const [failure, setFailure] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const mounted = useRef(true);
  const poll = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Lets the 202 branch schedule the next poll without a self-reference.
  const again = useRef<() => void>(() => {});

  const load = useCallback(async () => {
    try {
      let result;
      if (share.circle_id) result = await fetchCircleSharePreview(share.lesson_id, share.circle_id);
      else if (share.friendship_id) result = await fetchSharePreview(share.lesson_id, share.friendship_id);
      else {
        setFailure("This share doesn’t have an audience yet.");
        setPhase("failed");
        return;
      }
      if (!mounted.current) return;
      if (result.status === 200) {
        setPreview(result.data);
        setDraft(result.data.redacted_text);
        setPhase("ready");
      } else if (result.status === 202) {
        poll.current = setTimeout(() => again.current(), 2000);
      } else {
        setFailure(result.detail);
        setPhase("failed");
      }
    } catch (err) {
      if (!mounted.current) return;
      setFailure(getErrorMessage(err));
      setPhase("failed");
    }
  }, [share.lesson_id, share.friendship_id, share.circle_id]);

  useEffect(() => {
    again.current = () => void load();
  }, [load]);
  useEffect(() => {
    mounted.current = true;
    (async () => {
      await load();
    })();
    return () => {
      mounted.current = false;
      if (poll.current) clearTimeout(poll.current);
    };
  }, [load]);

  const busy = approve.isPending || reject.isPending;
  const send = async (finalText: string | undefined) => {
    try {
      const result = await approve.mutateAsync({ id: share.id, finalText });
      if (result.status === 200) {
        emitToast("Shared.", "success");
        onClose();
      } else if (result.status === 202) {
        // An edit triggered a re-scrub — re-preview before it can go out.
        setEditing(false);
        setPhase("loading");
        void load();
      } else {
        setFailure(result.detail);
        setPhase("failed");
      }
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };
  const decline = async () => {
    try {
      await reject.mutateAsync(share.id);
      emitToast("Declined — it won’t be sent.", "success");
      onClose();
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };

  return (
    <SidePanel label="Review before it goes out" onClose={onClose}>
      <div className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow={`Share to ${share.audience ?? "a cluster"}`} title="Review before it goes out" />
        {phase === "loading" ? (
          <p className="text-[0.9375rem] text-os-muted" role="status">
            Preparing your preview safely&hellip;
          </p>
        ) : null}
        {phase === "failed" ? <ErrorLine>{failure}</ErrorLine> : null}
        {phase === "ready" && preview && !editing ? (
          <>
            <blockquote className="os-serif border-l border-os-accent-line pl-5 text-[1.5rem] leading-snug text-white">&ldquo;{preview.redacted_text}&rdquo;</blockquote>
            <p className="text-[0.875rem] text-os-muted">
              This goes to <span className="text-white">{preview.audience}</span>.
            </p>
            <p className="os-hairline-top pt-3 text-[0.8125rem] leading-relaxed text-os-faint">{preview.residuals_banner}</p>
          </>
        ) : null}
        {phase === "ready" && preview && editing ? (
          <label className="flex flex-col gap-2">
            <span className={labelCls}>Edit before sending</span>
            <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={5} className={`${fieldCls} resize-y py-2.5`} data-autofocus />
            <span className="text-[0.75rem] text-os-faint">Editing re-checks your text for anything we hide before it can be approved.</span>
          </label>
        ) : null}
        <div className="mt-auto flex flex-wrap items-center gap-x-5 gap-y-2 pt-4">
          {phase === "ready" && !editing ? (
            <>
              <button type="button" className={ghostBtnCls} disabled={busy} onClick={() => void send(undefined)}>
                {approve.isPending ? "Approving…" : "Approve and send"}
              </button>
              <button type="button" className={quietBtnCls} disabled={busy} onClick={() => setEditing(true)}>
                Edit
              </button>
              <button type="button" className={dangerBtnCls} disabled={busy} onClick={() => void decline()}>
                {reject.isPending ? "Declining…" : "Don’t send"}
              </button>
            </>
          ) : null}
          {phase === "ready" && editing ? (
            <>
              <button type="button" className={ghostBtnCls} disabled={approve.isPending || !draft.trim()} onClick={() => void send(draft)}>
                {approve.isPending ? "Checking…" : "Save and re-check"}
              </button>
              <button type="button" className={quietBtnCls} disabled={approve.isPending} onClick={() => { setEditing(false); setDraft(preview?.redacted_text ?? ""); }}>
                Cancel
              </button>
            </>
          ) : null}
          {phase === "failed" ? (
            <button type="button" className={quietBtnCls} onClick={onClose}>
              Close
            </button>
          ) : null}
        </div>
      </div>
    </SidePanel>
  );
}

/** Pick an approved lesson and send it to a neighbor or a cluster — it goes to Needs you for review first. */
export function ShareLessonPanel({
  neighbors,
  circles,
  initial,
  onClose,
}: {
  neighbors: HomeNeighbor[];
  circles: CircleSummary[];
  initial: { friendshipId?: string; circleId?: string };
  onClose: () => void;
}) {
  const { data: lessons = [], isLoading } = useApprovedLessonsQuery();
  const share = useShareLessonMutation();
  const [lessonId, setLessonId] = useState("");
  const [kind, setKind] = useState<"neighbor" | "circle">(initial.circleId ? "circle" : "neighbor");
  const [friendshipId, setFriendshipId] = useState(initial.friendshipId ?? "");
  const [circleId, setCircleId] = useState(initial.circleId ?? "");
  const target = kind === "circle" ? circleId : friendshipId;
  const person = neighbors.find((n) => n.friendship_id === friendshipId);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!lessonId || !target) return;
    try {
      await share.mutateAsync(kind === "circle" ? { lessonId: Number(lessonId), circleId } : { lessonId: Number(lessonId), friendshipId });
      emitToast("Ready for your review in Needs you.", "success");
      onClose();
    } catch {
      // Pillar-blocked / missing-audience failures surface via the global error toast.
    }
  };
  return (
    <SidePanel label="Share a lesson" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-1 flex-col gap-6">
        <PanelHeader onClose={onClose} eyebrow={kind === "neighbor" && person ? `To ${person.display_name}` : "Share"} title="Share a lesson" />
        <p className="text-[0.9375rem] leading-relaxed text-os-muted">Something you&rsquo;ve learned. Nothing goes out until you&rsquo;ve seen exactly what they&rsquo;ll see and approved it.</p>
        {isLoading ? (
          <p className="text-[0.9375rem] text-os-muted">Loading your lessons&hellip;</p>
        ) : lessons.length === 0 ? (
          <p className="text-[0.9375rem] text-os-muted">No approved lessons yet &mdash; keep one in your Constellation first.</p>
        ) : (
          <>
            <label className="flex flex-col gap-1">
              <span className={labelCls}>Lesson</span>
              <select value={lessonId} onChange={(e) => setLessonId(e.target.value)} className={`${fieldCls} bg-os-sky`} data-autofocus>
                <option value="">Choose a lesson&hellip;</option>
                {lessons.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.text.length > 90 ? `${l.text.slice(0, 87)}…` : l.text}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex flex-col gap-1">
              <span className={labelCls}>With</span>
              <div className="flex gap-5" role="radiogroup" aria-label="Share with">
                <button type="button" role="radio" aria-checked={kind === "neighbor"} disabled={!neighbors.length} onClick={() => setKind("neighbor")} className={`os-focus min-h-[40px] rounded text-[0.875rem] disabled:opacity-40 ${kind === "neighbor" ? "text-os-accent" : "text-os-muted hover:text-os-ink"}`}>
                  A neighbor
                </button>
                <button type="button" role="radio" aria-checked={kind === "circle"} disabled={!circles.length} onClick={() => setKind("circle")} className={`os-focus min-h-[40px] rounded text-[0.875rem] disabled:opacity-40 ${kind === "circle" ? "text-os-accent" : "text-os-muted hover:text-os-ink"}`}>
                  A cluster
                </button>
              </div>
            </div>
            {kind === "circle" ? (
              <label className="flex flex-col gap-1">
                <span className={labelCls}>Cluster</span>
                <select value={circleId} onChange={(e) => setCircleId(e.target.value)} className={`${fieldCls} bg-os-sky`}>
                  <option value="">Choose a cluster&hellip;</option>
                  {circles.map((c) => (
                    <option key={c.circle_id} value={c.circle_id}>
                      {c.name} ({c.member_count})
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <label className="flex flex-col gap-1">
                <span className={labelCls}>Neighbor</span>
                <select value={friendshipId} onChange={(e) => setFriendshipId(e.target.value)} className={`${fieldCls} bg-os-sky`}>
                  <option value="">Choose a neighbor&hellip;</option>
                  {neighbors.map((n) => (
                    <option key={n.friendship_id} value={n.friendship_id}>
                      {n.display_name} (@{n.handle})
                    </option>
                  ))}
                </select>
              </label>
            )}
            <div className="mt-auto flex items-center gap-4 pt-4">
              <button type="submit" className={ghostBtnCls} disabled={!lessonId || !target || share.isPending}>
                {share.isPending ? "Sending…" : "Prepare to share"}
              </button>
              <button type="button" className={quietBtnCls} onClick={onClose}>
                Cancel
              </button>
            </div>
          </>
        )}
      </form>
    </SidePanel>
  );
}
