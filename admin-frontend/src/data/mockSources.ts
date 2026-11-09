import type { Source } from '../types';

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();

/** A larger crawl, so the document pagination and level filter have something to page through. */
const bigCrawlChildren: Source['children'] = Array.from({ length: 24 }, (_, i) => {
  const n = i + 1;
  if (n <= 10) return { id: `b1-${n}`, title: `Program page ${n}`, url: `https://internet2.edu/community/programs/page-${n}`, type: 'page' as const, status: 'ingested' as const };
  if (n <= 20) return { id: `b1-${n}`, parentId: `b1-${n - 10}`, title: `Program detail ${n - 10}`, url: `https://internet2.edu/community/programs/page-${n - 10}/detail`, type: 'page' as const, status: 'ingested' as const };
  return { id: `b1-${n}`, parentId: 'b1-1', title: `Handout ${n - 20}.pdf`, url: `https://internet2.edu/community/files/handout-${n - 20}.pdf`, type: 'pdf' as const, status: 'ingested' as const };
});

export const mockSources: Source[] = [
  {
    id: 'p1',
    title: 'Cloud Forum 2026 — Abstracts & Slides',
    url: 'https://internet2.edu/cloud-forum/2026-cloud-forum-schedule/cloud-forum-2026-abstracts/',
    type: 'page',
    status: 'crawl_success',
    addedAt: ago(2),
    children: [
      { id: 'p1-1', title: 'Cloud Forum 2026 — Schedule', url: 'https://internet2.edu/cloud-forum/2026-cloud-forum-schedule/', type: 'page', status: 'pending' },
      { id: 'p1-2', title: 'Keynote: Multi-Cloud Strategy — slides.pdf', url: 'https://drive.google.com/file/d/1a2B3cD4eF5gH6iJ7kL8mN9oP0qR1sT2u/view', type: 'gdrive', status: 'pending' },
      { id: 'p1-3', title: 'FinOps for R&E — slides.pdf', url: 'https://drive.google.com/file/d/9xZ7qW8eR5tY6uI3oP2aS1dF4gH7jK0lM/view', type: 'gdrive', status: 'pending' },
    ],
  },
  {
    id: 'p2',
    title: 'AI WG — Meeting Agendas and Recordings',
    url: 'https://spaces.at.internet2.edu/spaces/CAI/pages/327975528/Meeting+Agendas+and+Recordings',
    type: 'page',
    status: 'ingest_started',
    addedAt: ago(30),
    children: [
      { id: 'p2-1', title: 'Sept AI WG Meeting — recording', url: 'https://internet2.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=8f3e2a41-6b7c-4d1e-9a52-3c8f0b7d1e64', type: 'panopto', status: 'ingested' },
      { id: 'p2-2', title: 'Aug AI WG Meeting — recording', url: 'https://internet2.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=19e7c3b8-2d4a-4f66-8b19-7a5e0d3c9f21', type: 'panopto', status: 'pending' },
    ],
  },
  {
    id: 'b1',
    title: 'Community Programs',
    url: 'https://internet2.edu/community/programs',
    type: 'page',
    status: 'ingest_complete',
    addedAt: ago(60 * 5),
    children: bigCrawlChildren,
  },
  {
    id: 'y1',
    title: 'I2 events — cloud track recording',
    url: 'https://www.youtube.com/watch?v=rEDqGLGK5qE',
    type: 'youtube',
    status: 'ingest_complete',
    addedAt: ago(60),
  },
  {
    id: 'c1',
    title: 'Cloud Infrastructure Community Program',
    url: 'https://internet2.edu/services/cloud-infrastructure-community-program',
    type: 'page',
    status: 'crawl_failed',
    addedAt: ago(60 * 24),
    error: 'Request timed out after 30s',
  },
];
