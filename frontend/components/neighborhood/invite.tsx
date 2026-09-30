"use client";

import { type FormEvent, useState } from "react";

import { CopyLink, ErrorLine, fieldCls, ghostBtnCls, labelCls, PanelBlock, PanelHeader, QrCode, SidePanel } from "@/components/neighborhood/ui";
import { emitToast } from "@/components/toast";
import { getErrorMessage } from "@/lib/errors";
import { useCreateInviteMutation, useSendWaveMutation } from "@/lib/queries";

/** Wave by @handle, or make a link (and QR) for someone who isn't here yet. */
export function InvitePanel({ onClose }: { onClose: () => void }) {
  const wave = useSendWaveMutation();
  const invite = useCreateInviteMutation();
  const [handle, setHandle] = useState("");
  const [note, setNote] = useState("");
  const [waveError, setWaveError] = useState("");
  const [linkError, setLinkError] = useState("");

  const sendWave = async (e: FormEvent) => {
    e.preventDefault();
    const h = handle.trim().replace(/^@/, "");
    if (!h) return;
    setWaveError("");
    try {
      const r = await wave.mutateAsync({ handle: h, note: note.trim() || undefined });
      setHandle("");
      setNote("");
      emitToast(r.status === "accepted" ? `You and ${r.display_name} are neighbors now.` : `Wave sent to ${r.display_name}.`, "success");
    } catch (err) {
      setWaveError(getErrorMessage(err));
    }
  };
  const makeLink = () => {
    setLinkError("");
    invite.mutate({}, { onError: (err) => setLinkError(getErrorMessage(err)) });
  };
  const link = invite.data;
  const expires = link ? new Date(link.expires_at) : null;
  const limits = link
    ? [
        expires && !Number.isNaN(expires.getTime()) ? `until ${expires.toLocaleDateString("en-US", { month: "short", day: "numeric" })}` : "",
        link.max_uses ? `for up to ${link.max_uses} ${link.max_uses === 1 ? "person" : "people"}` : "",
      ]
        .filter(Boolean)
        .join(", ")
    : "";

  return (
    <SidePanel label="Invite someone" onClose={onClose}>
      <div className="flex flex-1 flex-col gap-7">
        <PanelHeader onClose={onClose} eyebrow="Neighborhood" title="Invite someone" />
        <p className="text-[0.9375rem] leading-relaxed text-os-muted">Only people you both agree to know can see each other. Nothing here is public.</p>

        <PanelBlock label="Wave by handle">
          <form onSubmit={sendWave} className="flex flex-col gap-4">
            <label className="flex flex-col gap-1">
              <span className="sr-only">Handle</span>
              <span className="flex items-center border-b border-os-ring focus-within:border-os-accent">
                <span className="text-os-faint" aria-hidden="true">@</span>
                <input value={handle} onChange={(e) => setHandle(e.target.value)} placeholder="handle" maxLength={30} autoCapitalize="none" autoCorrect="off" className={`${fieldCls} border-0 pl-1 focus:border-0`} data-autofocus />
              </span>
            </label>
            <label className="flex flex-col gap-1">
              <span className={labelCls}>A note (optional)</span>
              <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="We met at the running club" maxLength={280} className={fieldCls} />
            </label>
            {waveError ? <ErrorLine>{waveError}</ErrorLine> : null}
            <button type="submit" className={`${ghostBtnCls} self-start`} disabled={!handle.trim() || wave.isPending}>
              {wave.isPending ? "Waving…" : "Wave"}
            </button>
          </form>
        </PanelBlock>

        <PanelBlock label="Or share a link">
          {link ? (
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
              <QrCode value={link.url} size={148} label="QR code for your invite link" />
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <p className="text-[0.875rem] leading-relaxed text-os-muted">
                  They&rsquo;ll see who invited them, then join NBHD and become your neighbor.
                  {limits ? ` Works ${limits}.` : ""}
                </p>
                <CopyLink url={link.url} label="Your invite link" />
              </div>
            </div>
          ) : (
            <>
              <p className="text-[0.875rem] leading-relaxed text-os-muted">For someone who isn&rsquo;t on NBHD yet &mdash; a link and a QR code they can scan.</p>
              <button type="button" onClick={makeLink} className={`${ghostBtnCls} self-start`} disabled={invite.isPending}>
                {invite.isPending ? "Making a link…" : "Make an invite link"}
              </button>
            </>
          )}
          {linkError ? <ErrorLine>{linkError}</ErrorLine> : null}
        </PanelBlock>
      </div>
    </SidePanel>
  );
}
