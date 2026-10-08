"use client";

import Link from "next/link";
import type { ReactNode } from "react";

/*
 * The Open Sky frame for the sign-in family (sign in, sign up, forgot and
 * reset password): the Webb Cosmic Cliffs on the left — a short band on top on
 * phones — and one calm column of thin-line fields on the right.
 */

export const authLabelCls = "text-[0.6875rem] font-semibold uppercase tracking-[0.14em] text-os-label";
export const authFieldCls =
  "min-h-[46px] w-full border-0 border-b border-os-ring bg-transparent px-0 text-[1.0625rem] text-white outline-none transition placeholder:text-os-faint focus:border-os-accent focus-visible:outline-none";
export const authPrimaryCls =
  "os-focus inline-flex min-h-[52px] items-center justify-center rounded-full border border-os-accent px-8 text-[1rem] font-semibold text-white transition hover:bg-os-accent-soft disabled:cursor-not-allowed disabled:opacity-50";
export const authAppleCls =
  "os-focus flex min-h-[52px] w-full items-center justify-center gap-2.5 rounded-xl bg-white px-4 text-[1rem] font-semibold text-black transition hover:bg-white/90 disabled:cursor-not-allowed disabled:opacity-50";
export const authLinkCls = "os-focus rounded text-os-accent transition hover:text-white";

export function AuthFrame({ title, subtitle, children }: { title: ReactNode; subtitle?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-[100dvh] flex-col bg-os-sky text-os-ink md:flex-row">
      <div className="relative h-[210px] shrink-0 overflow-hidden md:sticky md:top-0 md:h-[100dvh] md:w-[52%] lg:w-[50%]" role="img" aria-label="The Cosmic Cliffs in the Carina Nebula, seen by the James Webb Space Telescope">
        <picture>
          <source media="(max-width: 767px)" srcSet="/space/carina-nebula-1200.jpg" />
          <img src="/space/carina-nebula-2400.jpg" alt="" fetchPriority="high" decoding="async" className="absolute inset-0 h-full w-full object-cover object-[30%_40%]" />
        </picture>
        <div
          aria-hidden="true"
          className="absolute inset-0 hidden md:block"
          style={{ background: "linear-gradient(90deg, rgba(7,9,12,.1) 0%, rgba(7,9,12,.3) 70%, var(--os-sky) 100%), linear-gradient(180deg, rgba(7,9,12,.55) 0%, rgba(7,9,12,0) 35%, rgba(7,9,12,0) 70%, rgba(7,9,12,.72) 100%)" }}
        />
        <div aria-hidden="true" className="absolute inset-0 md:hidden" style={{ background: "linear-gradient(180deg, rgba(7,9,12,.5) 0%, rgba(7,9,12,0) 40%, rgba(7,9,12,.3) 70%, var(--os-sky) 100%)" }} />
        <Link href="/" className="os-focus absolute left-5 top-[calc(env(safe-area-inset-top)+1.25rem)] rounded text-[0.8125rem] tracking-[0.45em] text-white md:left-11 md:top-9 md:text-[0.875rem]" aria-label="NBHD home page">
          NBHD
        </Link>
        <p className="os-serif absolute bottom-[4.5rem] left-11 right-14 hidden text-[1.875rem] italic leading-tight text-white md:block">&ldquo;We carry a universe inside us.&rdquo;</p>
        <span className="absolute bottom-3 right-5 text-[0.6875rem] text-white/70 md:bottom-9 md:left-11 md:right-auto">The Cosmic Cliffs &middot; NASA, ESA, CSA, STScI</span>
      </div>

      <main className="flex flex-1 flex-col justify-center px-6 pb-12 pt-6 sm:px-12 md:px-14 md:py-16 lg:px-20 xl:px-24">
        <div className="mx-auto flex w-full max-w-[440px] flex-col gap-7 md:mx-0">
          <div className="flex flex-col gap-2.5">
            <h1 className="os-serif text-[2.75rem] leading-none text-white md:text-[3.5rem]">{title}</h1>
            {subtitle ? <p className="text-[1rem] leading-relaxed text-os-muted">{subtitle}</p> : null}
          </div>
          {children}
        </div>
      </main>
    </div>
  );
}

export function AuthDivider({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3.5 text-[0.75rem] text-os-faint" aria-hidden="true">
      <span className="h-px flex-1 bg-os-hairline" />
      {label}
      <span className="h-px flex-1 bg-os-hairline" />
    </div>
  );
}

export function AuthError({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="border-l-2 border-os-danger pl-3 text-[0.875rem] leading-relaxed text-os-danger">
      {children}
    </div>
  );
}

export function AuthLegal({ verb = "signing in" }: { verb?: string }) {
  return (
    <p className="text-[0.75rem] leading-relaxed text-os-faint">
      By {verb} you agree to the{" "}
      <Link href="/legal/terms" className="os-focus rounded text-os-muted underline hover:text-white">
        Terms
      </Link>{" "}
      and{" "}
      <Link href="/legal/privacy" className="os-focus rounded text-os-muted underline hover:text-white">
        Privacy Policy
      </Link>
      .
    </p>
  );
}
