"use client";

import Link from "next/link";
import { useSyncExternalStore } from "react";
import { isLoggedIn } from "@/lib/auth";
import { AppStoreBadge } from "@/components/app-store-badge";
import { SpacePhoto } from "@/components/landing/space-photo";
import "@/components/landing/living-universe.css";

const steps = [
  ["Talk naturally", "Daily check-ins, voice notes, stream of consciousness. Just talk. Your assistant is always listening."],
  ["Patterns emerge", "Your assistant extracts lessons, tracks goals, and connects the dots you can't see yet."],
  ["See your constellation", "A living map of who you're becoming. Your growth, your connections, your universe."],
];
const features = [
  ["Journal", "Write first. Your day, with the time."],
  ["Horizons", "Your North Star, goals and to-dos."],
  ["Constellation", "The lessons your life keeps teaching."],
  ["Fuel", "Training on the calendar."],
  ["Core", "Guided calm, eclipse to sun."],
  ["Neighborhood", "Your people, closest first."],
];
function subscribeAuth(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener("focus", callback);
  return () => { window.removeEventListener("storage", callback); window.removeEventListener("focus", callback); };
}
const loggedOutSnapshot = () => false;

export default function LandingPage() {
  // The public page is statically rendered; auth only changes its CTA after hydration.
  const loggedIn = useSyncExternalStore(subscribeAuth, isLoggedIn, loggedOutSnapshot);
  const cta = loggedIn ? "Open my NBHD" : "Begin your journey";
  const href = loggedIn ? "/overview" : "/signup";
  const actions = <div className="universe-actions"><Link className="universe-button" href={href}>{cta}</Link><AppStoreBadge height={48} /></div>;
  return <div className="living-universe">
    <link rel="preload" as="image" href="/space/carina-nebula-1200.jpg" media="(max-width: 640px)" fetchPriority="high" />
    <link rel="preload" as="image" href="/space/carina-nebula-2400.jpg" media="(min-width: 641px)" fetchPriority="high" />
    <a href="#home-content" className="skip-link">Skip to main content</a>
    <section className="universe-hero" aria-label="Intro">
      <SpacePhoto name="carina-nebula" alt="The Cosmic Cliffs in the Carina Nebula, seen by the James Webb Space Telescope" motion="drift" hero />
      <div className="universe-scrim" aria-hidden="true" />
      <div className="universe-twinkles" aria-hidden="true">{[0, 1, 2, 3, 4].map((star) => <span key={star} />)}</div>
      <header className="universe-nav universe-gutter">
        <Link href="/" aria-label="NBHD home page" className="universe-wordmark">NBHD</Link>
        <nav aria-label="Site">
          <a href="#how" className="universe-nav-detail">How it works</a>
          <Link href="/legal/privacy" className="universe-nav-detail">Privacy</Link>
          <Link href={loggedIn ? "/overview" : "/login"} className="universe-button">{loggedIn ? "Open my NBHD" : "Log in"}<span aria-hidden="true">→</span></Link>
        </nav>
      </header>
      <div id="home-content" className="universe-hero-copy universe-gutter universe-rise">
        <p className="universe-label">Neighborhood United</p>
        <h1>Explore the universe <em>inside you</em></h1>
        <p className="universe-intro">A private AI companion that listens, learns, and helps you grow, through natural conversation, right from your phone.</p>
        {actions}
      </div>
      <p className="universe-caption">The Cosmic Cliffs, Carina Nebula · 7,600 light-years away · NASA, ESA, CSA, STScI</p>
    </section>
    <div>
      <section id="how" className="universe-how universe-gutter" aria-labelledby="how-title">
        <h2 id="how-title" className="universe-label">How it works</h2>
        <div className="universe-steps">{steps.map(([title, copy], index) => <article key={title}>
          <span className="universe-step-number">0{index + 1}</span><h3>{title}</h3><p>{copy}</p>
        </article>)}</div>
      </section>
      <section className="universe-shape" aria-labelledby="shape-title">
        <SpacePhoto name="ngc-1300" alt="Barred spiral galaxy NGC 1300, seen by Hubble" motion="slow" />
        <div className="universe-scrim" aria-hidden="true" />
        <div className="universe-shape-copy universe-gutter">
          <p className="universe-label">Every life has a shape</p>
          <h2 id="shape-title">Your days, drawn into something you can see.</h2>
          <p>Journal the way you talk. Your assistant gathers the threads into goals, lessons and a North Star, and shows you the shape they make over months, not minutes.</p>
        </div>
        <p className="universe-caption">NGC 1300 · barred spiral galaxy · NASA, ESA, Hubble</p>
      </section>
      <section className="universe-inside universe-gutter" aria-labelledby="inside-title">
        <h2 id="inside-title" className="universe-label">What&apos;s inside</h2>
        <dl>{features.map(([title, copy]) => <div key={title}><dt>{title}</dt><dd>{copy}</dd></div>)}</dl>
      </section>
      <section className="universe-quote" aria-label="The universe inside us">
        <SpacePhoto name="hubble-deep-field" alt="Hubble eXtreme Deep Field: thousands of galaxies" motion="slow" />
        <div className="universe-scrim" aria-hidden="true" />
        <div className="universe-quote-copy">
          <blockquote>“There are as many neurons in your brain as stars in the Milky Way. We carry a universe inside us.”</blockquote>
          <p>Almost every point of light behind these words is a whole galaxy.</p>
        </div>
        <p className="universe-caption">Hubble eXtreme Deep Field · NASA, ESA</p>
      </section>
      <section className="universe-final" aria-labelledby="get-started-title">
        <div className="universe-whirlpool"><SpacePhoto name="whirlpool-m51" alt="Whirlpool galaxy M51, seen by Hubble" motion="spin" /></div>
        <div className="universe-scrim" aria-hidden="true" />
        <h2 id="get-started-title">Your constellation is waiting.</h2>
        {actions}
        <p className="universe-caption">Whirlpool galaxy M51 · NASA, ESA, Hubble</p>
      </section>
    </div>
    <footer className="universe-footer universe-gutter">
      <p>© By Way of MJ LLC · Space images: NASA, ESA, CSA, STScI (Webb and Hubble)</p>
      <nav aria-label="Footer links">
        <Link href="/legal/privacy">Privacy</Link><Link href="/legal/terms">Terms</Link>
        <Link href="/legal/refund">Refund Policy</Link><Link href="/legal/commerce-disclosure">特定商取引法</Link>
        <a href="mailto:mj@bywayofmj.com">Contact</a>
      </nav>
    </footer>
  </div>;
}
