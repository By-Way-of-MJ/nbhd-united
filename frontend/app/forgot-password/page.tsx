"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";

import { AuthError, AuthFrame, authFieldCls, authLabelCls, authLinkCls, authPrimaryCls } from "@/components/auth/auth-frame";
import { ApiNetworkError, requestPasswordReset } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [rateLimited, setRateLimited] = useState(false);
  const [networkFailed, setNetworkFailed] = useState(false);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setRateLimited(false);
    setNetworkFailed(false);
    setLoading(true);
    try {
      await requestPasswordReset(email);
      setSubmitted(true);
    } catch (err) {
      // A 429 means the rate limiter tripped (per-email 3/hr, per-IP 5/hr) and
      // no email was sent — showing the "check your inbox" confirmation would
      // leave the user waiting for mail that never arrives. 429 is
      // existence-independent, so surfacing it doesn't reveal whether the
      // account exists. Network failures are also existence-independent and
      // stay on the form; any other failure falls through to the generic
      // confirmation, preserving the no-enumeration guarantee.
      if (err instanceof ApiNetworkError) {
        setNetworkFailed(true);
      } else if ((err as { status?: number } | null)?.status === 429) {
        setRateLimited(true);
      } else {
        setSubmitted(true);
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthFrame title="Reset your password" subtitle="Enter your email and we'll send you a link to set a new one.">
      {submitted ? (
        <p className="border-l-2 border-os-accent pl-4 text-[0.9375rem] leading-relaxed text-os-muted" role="status">
          Check your inbox. If an account exists for <span className="text-white">{email}</span>, you&apos;ll get a reset link within a minute or two. The link expires in 3 days.
        </p>
      ) : (
        <form onSubmit={handleSubmit} className="flex flex-col gap-7">
          <label className="flex flex-col gap-1.5">
            <span className={authLabelCls}>Email</span>
            <input id="email" type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} className={authFieldCls} placeholder="you@example.com" />
          </label>
          {networkFailed && <AuthError>Couldn&apos;t reach the server. Check your connection and try again.</AuthError>}
          {rateLimited && <AuthError>Too many reset requests. Please wait an hour before trying again.</AuthError>}
          <button type="submit" disabled={loading || !email} className={`${authPrimaryCls} self-start`}>
            {loading ? "Sending…" : "Send reset link"}
          </button>
        </form>
      )}
      <p className="text-[0.9375rem] text-os-muted">
        Remembered it?{" "}
        <Link href="/login" className={authLinkCls}>
          Back to sign in
        </Link>
      </p>
    </AuthFrame>
  );
}
