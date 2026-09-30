"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useState } from "react";

import { AuthError, AuthFrame, authFieldCls, authLabelCls, authLinkCls, authPrimaryCls } from "@/components/auth/auth-frame";
import { ApiNetworkError, confirmPasswordReset } from "@/lib/api";
import { setTokens } from "@/lib/auth";

function ResetPasswordInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const uid = searchParams.get("uid") ?? "";
  const token = searchParams.get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const linkBroken = !uid || !token;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");

    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }

    setLoading(true);
    try {
      const tokens = await confirmPasswordReset(uid, token, password);
      setTokens(tokens.access, tokens.refresh);
      router.push("/journal");
    } catch (err) {
      setError(
        err instanceof ApiNetworkError
          ? "Couldn't reach the server. Check your connection and try again."
          : err instanceof Error
          ? err.message
          : "Reset link is invalid or has expired.",
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthFrame title="Set a new password" subtitle="Choose something you haven't used elsewhere.">
      {linkBroken ? (
        <AuthError>
          This reset link is missing required information. Request a new one from the{" "}
          <Link href="/forgot-password" className="underline hover:text-white">
            forgot-password page
          </Link>
          .
        </AuthError>
      ) : (
        <form onSubmit={handleSubmit} className="flex flex-col gap-7">
          <div className="flex flex-col gap-5">
            <label className="flex flex-col gap-1.5">
              <span className={authLabelCls}>New password</span>
              <input id="password" type="password" required autoComplete="new-password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} className={authFieldCls} placeholder="At least 8 characters" />
            </label>
            <label className="flex flex-col gap-1.5">
              <span className={authLabelCls}>Confirm password</span>
              <input id="confirm" type="password" required autoComplete="new-password" minLength={8} value={confirm} onChange={(e) => setConfirm(e.target.value)} className={authFieldCls} placeholder="Type it again" />
            </label>
          </div>
          {error && <AuthError>{error}</AuthError>}
          <button type="submit" disabled={loading || !password || !confirm} className={`${authPrimaryCls} self-start`}>
            {loading ? "Setting…" : "Set new password"}
          </button>
        </form>
      )}
      <p className="text-[0.9375rem] text-os-muted">
        Back to{" "}
        <Link href="/login" className={authLinkCls}>
          Sign in
        </Link>
      </p>
    </AuthFrame>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={null}>
      <ResetPasswordInner />
    </Suspense>
  );
}
