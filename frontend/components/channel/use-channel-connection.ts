"use client";

import { useState } from "react";

import type { LineLinkResponse, TelegramLinkResponse } from "@/lib/api";
import { getErrorMessage } from "@/lib/errors";
import {
  useGenerateLineLinkMutation,
  useGenerateTelegramLinkMutation,
  useLineStatusQuery,
  useTelegramStatusQuery,
  useTenantQuery,
} from "@/lib/queries";

function hasErrorCode(error: unknown, code: string): boolean {
  if (!(error instanceof Error)) return false;
  try {
    const body = JSON.parse(error.message) as { code?: unknown };
    return body.code === code;
  } catch {
    return false;
  }
}

export function useTelegramConnection(pairingOpen: boolean) {
  const [link, setLink] = useState<TelegramLinkResponse | null>(null);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const { data: tenant } = useTenantQuery();
  const [unavailable, setUnavailable] = useState(false);
  const available = Boolean(tenant) && tenant?.telegram_available !== false;
  const statusQuery = useTelegramStatusQuery(
    Boolean(tenant) && !unavailable,
    pairingOpen && Boolean(link),
    available,
  );
  const statusUnavailable = hasErrorCode(statusQuery.error, "telegram_unavailable");
  const generateMutation = useGenerateTelegramLinkMutation();

  const generate = async () => {
    if (!available || unavailable || statusUnavailable) return;
    setGenerationError(null);
    generateMutation.reset();
    try {
      setLink(await generateMutation.mutateAsync());
    } catch (error) {
      setGenerationError(getErrorMessage(error));
      if (hasErrorCode(error, "telegram_unavailable")) {
        setLink(null);
        setUnavailable(true);
      }
    }
  };

  return {
    unavailable: unavailable || statusUnavailable,
    link,
    generationError,
    generate,
    isGenerating: generateMutation.isPending,
    status: statusQuery.data,
    statusError: statusQuery.isError ? getErrorMessage(statusQuery.error) : null,
    statusReady: statusQuery.isFetchedAfterMount && !statusQuery.isError,
    retryStatus: statusQuery.refetch,
  };
}

export function useLineConnection(pairingOpen: boolean) {
  const [link, setLink] = useState<LineLinkResponse | null>(null);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const statusQuery = useLineStatusQuery(true, pairingOpen && Boolean(link));
  const generateMutation = useGenerateLineLinkMutation();

  const generate = async () => {
    setGenerationError(null);
    generateMutation.reset();
    try {
      setLink(await generateMutation.mutateAsync());
    } catch (error) {
      setGenerationError(getErrorMessage(error));
      if (hasErrorCode(error, "line_quota_exhausted")) {
        await statusQuery.refetch();
      }
    }
  };

  return {
    link,
    generationError,
    generate,
    isGenerating: generateMutation.isPending,
    status: statusQuery.data,
    statusError: statusQuery.isError ? getErrorMessage(statusQuery.error) : null,
    statusReady: statusQuery.isFetchedAfterMount && !statusQuery.isError,
    retryStatus: statusQuery.refetch,
  };
}
