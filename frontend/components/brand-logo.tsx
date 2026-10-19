"use client";

import Link from "next/link";

import { BrandMark } from "@/components/brand-mark";
import { useTheme } from "@/components/theme-provider";

/**
 * Brand logo — icon + wordmark.
 * Uses the approved N variant for the surrounding surface.
 */
export function BrandLogo({ size = 32, showText = true }: { size?: number; showText?: boolean }) {
  const { theme } = useTheme();

  return (
    <Link href="/" aria-label="Neighborhood United home" className="flex items-center gap-2 transition hover:opacity-80">
      <BrandMark size={size} variant={theme} />
      {showText && (
        <span className="font-mono text-xs uppercase tracking-[0.24em] text-ink-faint">
          Neighborhood United
        </span>
      )}
    </Link>
  );
}

/** Icon-only version for compact spaces (mobile header, favicon-adjacent). */
export function BrandIcon({ size = 36 }: { size?: number }) {
  const { theme } = useTheme();

  return (
    <Link href="/" aria-label="Neighborhood United home" className="transition hover:opacity-80">
      <BrandMark size={size} variant={theme} />
    </Link>
  );
}
