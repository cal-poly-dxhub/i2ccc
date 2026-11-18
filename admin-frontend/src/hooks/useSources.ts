import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../services/api';
import type { SourceStatus, SourcesPage } from '../types';
import { matchSource } from '../urlPatterns';

export type AddResult = 'added' | 'unsupported' | 'duplicate' | 'failed';

export const SOURCES_PER_PAGE = 10;
// Poll faster while something is crawling, ingesting or being removed
const ACTIVE_POLL_MS = 5_000;
const IDLE_POLL_MS = 30_000;
const BUSY_STATUSES: SourceStatus[] = ['crawling', 'ingest_started', 'cleaning_up'];

const ACTION_ERRORS: Record<string, string> = {
  crawl_in_progress: 'That source is still crawling. Try again when the crawl finishes.',
  cleanup_in_progress: 'That source is still being removed. Try again when it finishes.',
  source_not_found: 'That source no longer exists.',
};

function actionMessage(error: unknown): string {
  return (error instanceof api.ApiError && ACTION_ERRORS[error.code]) || 'Something went wrong. Try again.';
}

/**
 * One page of sources from GET /api/sources, polled, plus the actions that change them.
 * Nothing is fetched until enabled is true, so no request goes out before sign-in has a token.
 */
export function useSources(enabled: boolean) {
  // cursors[i] fetches page i; page 0 has none
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [page, setPage] = useState(0);
  const [data, setData] = useState<SourcesPage | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const latestRequest = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++latestRequest.current;
    try {
      const result = await api.listSources(SOURCES_PER_PAGE, cursors[page]);
      if (requestId !== latestRequest.current) return; // a newer request superseded this one
      if (result.sources.length === 0 && page > 0) {
        // Deletes emptied this page
        setPage(page - 1);
        return;
      }
      setData(result);
      setLoadError(false);
    } catch {
      if (requestId === latestRequest.current) setLoadError(true);
    }
  }, [cursors, page]);

  useEffect(() => {
    if (enabled) refresh();
  }, [refresh, enabled]);

  const active = data?.sources.some((s) => BUSY_STATUSES.includes(s.status)) ?? false;
  useEffect(() => {
    if (!enabled) return;
    const id = window.setInterval(refresh, active ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh, active, enabled]);

  const goToPage = useCallback(
    (next: number) => {
      if (next > page) {
        setCursors((prev) => {
          const copy = prev.slice(0, page + 1);
          copy[next] = data?.nextCursor;
          return copy;
        });
      }
      setPage(next);
    },
    [page, data],
  );

  const addSource = useCallback(
    async (rawUrl: string): Promise<AddResult> => {
      // Same listing as the server (shared/source_urls.yaml), so obvious mistakes skip the round trip
      if (!matchSource(rawUrl)) return 'unsupported';
      try {
        await api.addSource(rawUrl.trim());
      } catch (error) {
        if (error instanceof api.ApiError && error.code === 'unsupported_url') return 'unsupported';
        if (error instanceof api.ApiError && error.code === 'url_already_exists') return 'duplicate';
        return 'failed';
      }
      setActionError(null);
      // New sources are listed first
      if (page === 0) await refresh();
      else setPage(0);
      return 'added';
    },
    [page, refresh],
  );

  const runAction = useCallback(
    async (action: () => Promise<unknown>) => {
      try {
        await action();
        setActionError(null);
      } catch (error) {
        setActionError(actionMessage(error));
      }
      await refresh();
    },
    [refresh],
  );

  const rerunSource = useCallback((id: string) => runAction(() => api.rerunSource(id)), [runAction]);
  const deleteSource = useCallback((id: string) => runAction(() => api.deleteSource(id)), [runAction]);
  /** Ingest every crawled document that is not ingested yet, whichever source it belongs to. */
  const ingestAll = useCallback(() => runAction(api.startIngest), [runAction]);

  return {
    sources: data?.sources ?? [],
    pendingCount: data?.pendingCount ?? 0,
    loaded: data !== null,
    loadError,
    actionError,
    page,
    hasNextPage: Boolean(data?.nextCursor),
    goToPage,
    addSource,
    rerunSource,
    deleteSource,
    ingestAll,
  };
}
