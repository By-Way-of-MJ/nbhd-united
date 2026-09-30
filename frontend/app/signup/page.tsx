"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useEffect, useState } from "react";

import {
  AppleSignInButton,
  type AppleAuthenticationResult,
  useAppleSignInEligible,
} from "@/components/apple-sign-in-button";
import {
  AuthDivider,
  AuthError,
  AuthFrame,
  AuthLegal,
  authAppleCls,
  authFieldCls,
  authLabelCls,
  authLinkCls,
  authPrimaryCls,
} from "@/components/auth/auth-frame";
import { fetchMe, signup } from "@/lib/api";
import {
  completeAuthentication,
  getAccessToken,
  getAuthenticationEpoch,
} from "@/lib/auth";
import { hasPendingAppAuthorize } from "@/lib/app-authorize";
import { authPathForIntent } from "@/lib/authorize-decision";
import { stashInviteToken } from "@/lib/invite-token";
import { decidePostAuthRoute } from "@/lib/post-auth-route";
import { PasswordStrengthMeter } from "@/components/onboarding/password-strength-meter";

function SignupPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const fromApp = searchParams.get("from") === "app";
  const loginParams = new URLSearchParams();
  for (const key of ["from", "invite"]) {
    const value = searchParams.get(key);
    if (value !== null) loginParams.set(key, value);
  }
  const loginQuery = loginParams.toString();
  const loginHref = `${authPathForIntent("signin")}${loginQuery ? `?${loginQuery}` : ""}`;

  // Neighborhood invite handoff: stash `?invite=<token>` now — tenant
  // provisioning (and the invite claim) happens later, in PersonaScene
  // during /onboarding, by which point the query param is gone.
  useEffect(() => {
    const invite = searchParams.get("invite");
    if (invite) stashInviteToken(invite);
  }, [searchParams]);

  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [emailExists, setEmailExists] = useState(false);
  const [loading, setLoading] = useState(false);
  const [appleBusy, setAppleBusy] = useState(false);
  const appleEligible = useAppleSignInEligible();
  const [returnToApp, setReturnToApp] = useState<{
    created: boolean;
    email: string;
  } | null>(null);

  const finishWebRouting = async (created: boolean) => {
    if (created) {
      const destination = decidePostAuthRoute({
        hasPendingHandoff: false,
        fromApp: false,
        created,
        needsOnboarding: false,
      });
      router.push(destination === "journal" ? "/journal" : "/onboarding");
      return;
    }

    let needsOnboarding = true;
    try {
      const me = await fetchMe();
      needsOnboarding = !me.tenant || me.tenant.status !== "active";
    } catch {
      // Preserve the existing safe fallback to onboarding.
    }
    const destination = decidePostAuthRoute({
      hasPendingHandoff: false,
      fromApp: false,
      created,
      needsOnboarding,
    });
    router.push(destination === "journal" ? "/journal" : "/onboarding");
  };

  const finishAuthentication = async (
    result: AppleAuthenticationResult,
    attemptEpoch: number,
    attemptAccessToken: string | null,
  ) => {
    if (
      getAccessToken() !== attemptAccessToken ||
      getAuthenticationEpoch() !== attemptEpoch
    ) {
      return;
    }
    completeAuthentication(result);
    const created = Boolean(result.created);
    const destination = decidePostAuthRoute({
      hasPendingHandoff: hasPendingAppAuthorize(),
      fromApp,
      created,
      needsOnboarding: false,
    });
    if (destination === "app-authorize") {
      router.replace("/app/authorize");
      return;
    }
    if (destination === "return-to-app") {
      setReturnToApp({ created, email });
      return;
    }
    await finishWebRouting(created);
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (appleBusy) return;
    setError("");
    setEmailExists(false);

    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    setLoading(true);
    const attemptEpoch = getAuthenticationEpoch();
    const attemptAccessToken = getAccessToken();
    try {
      const tokens = await signup(
        email,
        password,
        displayName || undefined,
        fromApp ? "ios_handoff" : undefined,
      );
      await finishAuthentication(
        { ...tokens, created: true },
        attemptEpoch,
        attemptAccessToken,
      );
    } catch (err) {
      setEmailExists(err instanceof Error && "status" in err && err.status === 409);
      setError(err instanceof Error ? err.message : "Signup failed.");
    } finally {
      setLoading(false);
    }
  };

  if (returnToApp) {
    return (
      <AuthFrame
        title="Your account is ready"
        subtitle={
          <>
            Return to the NBHD app and tap &ldquo;Create my space&rdquo; again.{" "}
            {returnToApp.email ? (
              <>
                You&apos;ll be offered &ldquo;Continue as {returnToApp.email}&rdquo; to finish signing in.
              </>
            ) : (
              <>You&apos;ll be offered the option to continue with your account.</>
            )}
          </>
        }
      >
        <button type="button" onClick={() => void finishWebRouting(returnToApp.created)} className={`${authLinkCls} min-h-[44px] self-start text-[0.9375rem]`}>
          Continue on the web instead
        </button>
      </AuthFrame>
    );
  }

  return (
    <AuthFrame
      title="Begin your journey"
      subtitle={
        <>
          Your private AI companion, in the <span className="text-white">NBHD app</span>. 30-day free trial.
        </>
      }
    >
      <AppleSignInButton
        flow="authenticate"
        label="Sign up with Apple"
        disabled={loading}
        onAuthenticated={finishAuthentication}
        onBusyChange={setAppleBusy}
        buttonClassName={authAppleCls}
      />
      {appleEligible ? <AuthDivider label="or with email" /> : null}

      <form onSubmit={handleSubmit} className="flex flex-col gap-7">
        <div className="flex flex-col gap-5">
          <label className="flex flex-col gap-1.5">
            <span className={authLabelCls}>What should your assistant call you?</span>
            <input id="displayName" type="text" autoComplete="given-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} className={authFieldCls} placeholder="Your name" />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={authLabelCls}>Email</span>
            <input id="email" type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} className={authFieldCls} placeholder="you@example.com" />
          </label>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="password" className={authLabelCls}>
              Password
            </label>
            <input id="password" type="password" required autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} className={authFieldCls} placeholder="Create a password" />
            <PasswordStrengthMeter password={password} />
          </div>
          <label className="flex flex-col gap-1.5">
            <span className={authLabelCls}>Confirm password</span>
            <input id="confirmPassword" type="password" required autoComplete="new-password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} className={authFieldCls} placeholder="Type it again" />
          </label>
        </div>

        {error && (
          <AuthError>
            {emailExists ? (
              <>
                An account with this email already exists.{" "}
                <Link href={loginHref} className="underline hover:text-white">
                  Sign in
                </Link>{" "}
                to continue.
              </>
            ) : (
              error
            )}
          </AuthError>
        )}

        <button type="submit" disabled={loading || appleBusy} className={`${authPrimaryCls} self-start`}>
          {loading ? "Creating account…" : "Create account"}
        </button>
      </form>

      <p className="text-[0.9375rem] text-os-muted">
        Already have an account?{" "}
        <Link href={loginHref} className={authLinkCls}>
          Sign in
        </Link>
      </p>
      <AuthLegal verb="creating an account" />
    </AuthFrame>
  );
}

export default function SignupPage() {
  return (
    <Suspense fallback={null}>
      <SignupPageInner />
    </Suspense>
  );
}
