"use client";

import { Suspense } from "react";

import { ClusterPage } from "@/components/neighborhood/cluster/cluster-page";

// The cluster is chosen by `?id=` (static export: no dynamic route segments).
export default function Page() {
  return (
    <Suspense fallback={null}>
      <ClusterPage />
    </Suspense>
  );
}
