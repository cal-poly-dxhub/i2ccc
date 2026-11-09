import { useCallback, useEffect, useRef, useState } from 'react';
import { mockSources } from '../data/mockSources';
import type { Source, SourceChild } from '../types';
import { detectType, normalizeUrl } from '../utils';

export type AddResult = 'added' | 'unsupported' | 'duplicate';

/**
 * Mock data layer. Replace the bodies of addSource / rerunSource / deleteSource / ingestAll
 * with the API calls in BACKEND_PLAN.md (POST /api/sources, POST /api/sources/{id}/rerun,
 * DELETE /api/sources/{id}, POST /api/ingest) and poll GET /api/sources for status.
 */
/** A page source finds subpages and documents. A direct link (video, Drive, PDF) is itself the document, so it has none. */
function fakeChildren(source: Source): SourceChild[] {
  if (source.type !== 'page') return [];
  const base = source.url.replace(/\/$/, '');
  return [
    { id: `${source.id}-1`, title: 'Subpage 1', url: `${base}/about`, type: 'page', status: 'pending' },
    { id: `${source.id}-2`, title: 'Subpage 2', url: `${base}/about/team`, type: 'page', parentId: `${source.id}-1`, status: 'pending' },
    { id: `${source.id}-3`, parentId: `${source.id}-1`, title: 'slides.pdf', url: `${base}/slides.pdf`, type: 'pdf', status: 'pending' },
  ];
}

export function useSources() {
  const [sources, setSources] = useState<Source[]>(mockSources);
  const timers = useRef<number[]>([]);

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach(window.clearTimeout);
  }, []);

  const later = useCallback((fn: () => void, ms: number) => {
    timers.current.push(window.setTimeout(fn, ms));
  }, []);

  const update = useCallback((id: string, patch: (s: Source) => Source) => {
    setSources((prev) => prev.map((s) => (s.id === id ? patch(s) : s)));
  }, []);

  const crawl = useCallback(
    (source: Source) => {
      later(() => update(source.id, (s) => ({ ...s, status: 'crawl_success', children: fakeChildren(s).length ? fakeChildren(s) : undefined })), 3000);
    },
    [later, update],
  );

  const addSource = useCallback(
    (rawUrl: string): AddResult => {
      const normalized = normalizeUrl(rawUrl);
      if (!normalized) return 'unsupported';
      if (sources.some((s) => normalizeUrl(s.url) === normalized)) return 'duplicate';
      const url = rawUrl.trim();
      const source: Source = {
        id: crypto.randomUUID(),
        title: url.replace(/^https?:\/\//i, ''),
        url,
        type: detectType(url),
        status: 'crawling',
        addedAt: new Date().toISOString(),
      };
      setSources((prev) => [source, ...prev]);
      crawl(source);
      return 'added';
    },
    [sources, crawl],
  );

  const rerunSource = useCallback(
    (id: string) => {
      const source = sources.find((s) => s.id === id);
      if (!source) return;
      update(id, (s) => ({ ...s, status: 'crawling', error: undefined, children: undefined }));
      crawl(source);
    },
    [sources, update, crawl],
  );

  const deleteSource = useCallback((id: string) => {
    setSources((prev) => prev.filter((s) => s.id !== id));
  }, []);

  /** Ingest every crawled document that is not ingested yet, whichever source it belongs to. */
  const ingestAll = useCallback(() => {
    const targets = sources.filter(
      (s) => s.status === 'crawl_success' || (s.status === 'ingest_started' && s.children?.some((c) => c.status === 'pending')),
    );
    targets.forEach((target, i) => {
      update(target.id, (s) => ({ ...s, status: 'ingest_started' }));
      later(
        () =>
          update(target.id, (s) => ({
            ...s,
            status: 'ingest_complete',
            children: s.children?.map((c) => (c.status === 'pending' ? { ...c, status: 'ingested' } : c)),
          })),
        3000 + i * 1500,
      );
    });
  }, [sources, update, later]);

  // Documents waiting to be ingested. Each source's own document (the page text, or the
  // file for a direct link) counts too, so it is added on top of the pending children.
  // The real API returns this total, see BACKEND_PLAN.md.
  const pendingCount = sources.reduce((n, s) => {
    if (s.status === 'crawling' || s.status === 'crawl_failed') return n;
    return n + (s.children?.filter((c) => c.status === 'pending').length ?? 0) + (s.status === 'crawl_success' ? 1 : 0);
  }, 0);

  return { sources, addSource, rerunSource, deleteSource, ingestAll, pendingCount };
}
