"use client";

import { Suspense } from "react";

import { ProjectPage } from "@/components/neighborhood/project/project-page";

// The project is chosen by `?id=` (static export: no dynamic route segments).
export default function Page() {
  return (
    <Suspense fallback={null}>
      <ProjectPage />
    </Suspense>
  );
}
