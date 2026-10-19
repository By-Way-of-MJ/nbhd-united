import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";

import { AppShell } from "@/components/app-shell";
import { ThemeProvider } from "@/components/theme-provider";
import { Providers } from "@/app/providers";

import "./globals.css";

// Fonts are self-hosted (app/fonts/, SIL OFL) so the static-export build never
// fetches Google Fonts — that download flaked in CI and failed builds. Variable
// files declare the same weight range next/font/google served.
//
// globals.css :root redefines --font-body/-display/-headline/-serif with the
// literal family names ("Plus Jakarta Sans", …) and wins over the classes
// below (it is imported after them), so those four @font-face rules keep the
// real names via `declarations`. --font-measure is only set here: next/font/local
// points the variable at the default family name, so Schibsted must not override it.

const plusJakarta = localFont({
  src: "./fonts/PlusJakartaSans-Variable.woff2",
  variable: "--font-body",
  display: "swap",
  weight: "400 700",
  declarations: [{ prop: "font-family", value: "'Plus Jakarta Sans'" }],
});

const dmSerif = localFont({
  src: "./fonts/DMSerifDisplay-Regular.woff2",
  variable: "--font-display",
  display: "swap",
  weight: "400",
  adjustFontFallback: "Times New Roman",
  declarations: [{ prop: "font-family", value: "'DM Serif Display'" }],
});

const spaceGrotesk = localFont({
  src: "./fonts/SpaceGrotesk-Variable.woff2",
  variable: "--font-headline",
  display: "swap",
  weight: "400 700",
  declarations: [{ prop: "font-family", value: "'Space Grotesk'" }],
});

const instrumentSerif = localFont({
  // Open Sky page titles use the upright face; legacy pull-quotes the italic.
  src: [
    { path: "./fonts/InstrumentSerif-Regular.woff2", weight: "400", style: "normal" },
    { path: "./fonts/InstrumentSerif-Italic.woff2", weight: "400", style: "italic" },
  ],
  variable: "--font-serif",
  display: "swap",
  adjustFontFallback: "Times New Roman",
  declarations: [{ prop: "font-family", value: "'Instrument Serif'" }],
});

// THE MEASURE onboarding screens (building / paused → subscribe).
const schibstedGrotesk = localFont({
  src: "./fonts/SchibstedGrotesk-Variable.woff2",
  variable: "--font-measure",
  display: "swap",
  weight: "400 500",
});

export const metadata: Metadata = {
  // Production web origin also configured by APPLE_SIWA_REDIRECT_URI in config/settings/base.py.
  metadataBase: new URL("https://hoodunited.org"),
  title: "Neighborhood United",
  description: "Your AI-powered personal assistant.",
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "16x16 32x32 48x48", type: "image/x-icon" },
      { url: "/icons/favicon/favicon-32x32.png", sizes: "32x32", type: "image/png" },
      { url: "/icons/favicon/favicon-16x16.png", sizes: "16x16", type: "image/png" },
      { url: "/icons/favicon/favicon.svg", sizes: "any", type: "image/svg+xml" },
    ],
    apple: [
      { url: "/icons/apple-touch-icon.png", sizes: "180x180", type: "image/png" },
    ],
  },
  manifest: "/manifest.json",
  openGraph: {
    title: "Neighborhood United",
    description: "Your AI-powered personal assistant.",
    images: [{
      url: "/images/og/nbhd-social-card-1200x630.png",
      width: 1200,
      height: 630,
      alt: "NBHD constellation N mark — Your personal assistant for real life",
    }],
  },
  twitter: {
    card: "summary_large_image",
    title: "Neighborhood United",
    description: "Your AI-powered personal assistant.",
    images: [{
      url: "/images/og/nbhd-social-card-1200x630.png",
      alt: "NBHD constellation N mark — Your personal assistant for real life",
    }],
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#0b0f13",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`h-full ${plusJakarta.variable} ${dmSerif.variable} ${spaceGrotesk.variable} ${instrumentSerif.variable} ${schibstedGrotesk.variable}`}
    >
      <body className="overflow-x-hidden bg-bg">
        <ThemeProvider>
          <Providers>
            {/* No .content-fade-up here — AppShell already applies it around
                <main>; doubling the wrapper ran two stacked entrance
                animations over the whole app on first paint. */}
            <AppShell>{children}</AppShell>
          </Providers>
        </ThemeProvider>
      </body>
    </html>
  );
}
