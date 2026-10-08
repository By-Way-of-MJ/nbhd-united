"use client";

import Link from "next/link";
import { useState } from "react";

import { SkyCanvas } from "@/components/open-sky/sky-canvas";
import { NorthStarRenderer } from "@/lib/sky-art/north-star";

/** Horizons header: the North Star sky with the page title over it. */
export function HorizonsSkyHeader({ title, statement }: { title: string; statement?: string }) {
  const [painter] = useState(() => new NorthStarRenderer());
  return (
    <header className="relative -mx-4 mb-8 overflow-hidden sm:-mx-6 md:mx-0">
      <SkyCanvas
        painter={painter}
        label="A sky of faint stars with one bright north star, above a mountain ridge at first light"
        className="os-band-fade absolute inset-0 h-full w-full"
      />
      <div className="relative min-h-[270px] px-4 pb-8 pt-5 sm:min-h-[230px] sm:px-6 md:px-8 md:pt-7">
        <h1 className="os-page-title">{title}</h1>
        {statement ? <p className="os-serif mt-3 max-w-[520px] text-2xl !italic leading-snug text-os-ink">{statement}</p> : null}
        <div className="mt-1 flex items-center gap-4">
          <span className="os-label !text-xs">North Star</span>
          <Link href="#north-star" className="os-focus inline-flex min-h-[44px] items-center text-xs text-os-accent">{statement ? "Manage" : "Set your direction"}</Link>
        </div>
      </div>
    </header>
  );
}
