import type { ChildStatus, SourceStatus, SourceType } from '../types';

export const TYPE_META: Record<SourceType, { label: string; color: string }> = {
  page: { label: 'Page', color: '#34c759' },
  pdf: { label: 'PDF', color: '#8e63ce' },
  gdrive: { label: 'Google Drive', color: '#4a86e8' },
  panopto: { label: 'Panopto', color: '#f6b26b' },
  youtube: { label: 'YouTube video', color: '#e06666' },
};

export type StatusTone = 'success' | 'warning' | 'neutral' | 'error';

export const STATUS_META: Record<SourceStatus | ChildStatus, { label: string; tone: StatusTone; active: boolean }> = {
  crawling: { label: 'Crawling', tone: 'warning', active: true },
  crawl_success: { label: 'Crawl success', tone: 'success', active: false },
  crawl_failed: { label: 'Crawl failed', tone: 'error', active: false },
  ingest_started: { label: 'Ingest started', tone: 'warning', active: true },
  ingest_complete: { label: 'Ingest complete', tone: 'success', active: false },
  ingest_failed: { label: 'Ingest failed', tone: 'error', active: false },
  pending: { label: 'Not ingested', tone: 'neutral', active: false },
  ingested: { label: 'Ingested', tone: 'success', active: false },
  failed: { label: 'Failed', tone: 'error', active: false },
};
