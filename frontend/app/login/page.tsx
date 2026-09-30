"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useState } from "react";

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
import { fetchMe, login } from "@/lib/api";
import {
  completeAuthentication,
  getAccessToken,
  getAuthenticationEpoch,
} from "@/lib/auth";
import { hasPendingAppAuthorize } from "@/lib/app-authorize";
import { decidePostAuthRoute } from "@/lib/post-auth-route";

function LoginPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const fromApp = searchParams.get("from") === "app";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
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
    let webRedesign = false;
    try {
      const me = await fetchMe();
      needsOnboarding = !me.tenant || me.tenant.status !== "active";
      webRedesign = !!me.tenant?.web_redesign;
    } catch {
      // Preserve the existing safe fallback to onboarding.
    }
    const destination = decidePostAuthRoute({
      hasPendingHandoff: false,
      fromApp: false,
      created,
      needsOnboarding,
    });
    const inviteReturn = searchParams.get("next");
    if (destination === "journal" && inviteReturn && /^\/friends\/invite\/[A-Za-z0-9_-]+$/.test(inviteReturn)) {
      // This unbounded path is served by SWA's rewrite, not Next's route table.
      window.location.assign(inviteReturn);
      return;
    }
    // Web redesign tenants land on the Overview; everyone else on Journal.
    router.push(destination === "journal" ? (webRedesign ? "/overview" : "/journal") : "/onboarding");
  };

  const finishAuthentication = async (
    result: Omit<AppleAuthenticationResult, "created"> & { created?: boolean },
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
    setLoading(true);
    const attemptEpoch = getAuthenticationEpoch();
    const attemptAccessToken = getAccessToken();

    try {
      const tokens = await login(email, password);
      await finishAuthentication(
        tokens,
        attemptEpoch,
        attemptAccessToken,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed.");
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
            Return to the NBHD app and tap &ldquo;Sign in&rdquo; again.{" "}
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
    <AuthFrame title="Welcome back" subtitle="Sign in to your neighborhood.">
      <AppleSignInButton
        flow="authenticate"
        label="Sign in with Apple"
        disabled={loading}
        onAuthenticated={finishAuthentication}
        onBusyChange={setAppleBusy}
        buttonClassName={authAppleCls}
      />
      {appleEligible ? <AuthDivider label="or with email" /> : null}

      <form onSubmit={handleSubmit} className="flex flex-col gap-7">
        <div className="flex flex-col gap-5">
          <label className="flex flex-col gap-1.5">
            <span className={authLabelCls}>Email</span>
            <input id="email" type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} className={authFieldCls} placeholder="you@example.com" />
          </label>
          <div className="flex flex-col gap-1.5">
            <div className="flex items-baseline justify-between">
              <label htmlFor="password" className={authLabelCls}>
                Password
              </label>
              <Link href="/forgot-password" className={`${authLinkCls} text-[0.8125rem]`}>
                Forgot?
              </Link>
            </div>
            <input id="password" type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} className={authFieldCls} placeholder="••••••••" />
          </div>
        </div>

        {error && (
          <AuthError>
            <p>{error}</p>
            <p className="mt-1 text-os-muted">
              <Link href="/forgot-password" className="underline hover:text-white">
                Reset your password
              </Link>{" "}
              if you don&apos;t remember it.
            </p>
          </AuthError>
        )}

        <button type="submit" disabled={loading || appleBusy} className={`${authPrimaryCls} self-start`}>
          {loading ? "Signing in…" : "Sign in"}
        </button>
      </form>

      <p className="text-[0.9375rem] text-os-muted">
        New here?{" "}
        <Link href="/signup" className={authLinkCls}>
          Begin your journey
        </Link>
      </p>
      <AuthLegal />
    </AuthFrame>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginPageInner />
    </Suspense>
  );
}
