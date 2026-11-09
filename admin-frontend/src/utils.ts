import type { SourceType } from './types';

export function timeAgo(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

/** Guess the source type from a pasted URL. The backend will own this later. */
export function detectType(rawUrl: string): SourceType {
  const url = rawUrl.toLowerCase();
  if (/(^|\/\/|\.)(youtube\.com|youtu\.be)/.test(url)) return 'youtube';
  if (url.includes('panopto')) return 'panopto';
  if (url.includes('drive.google.com') || url.includes('docs.google.com')) return 'gdrive';
  return 'page';
}


const PRIVATE_IPV4 = /^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|0\.)/;

/**
 * Returns a normalized form of a public http(s) URL, or null if it is unsupported.
 * Used for validation and duplicate detection. The backend repeats these checks.
 */
export function normalizeUrl(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  const host = url.hostname;
  if (!host.includes('.') || host.startsWith('[') || host === 'localhost' || PRIVATE_IPV4.test(host)) return null;
  url.hash = '';
  const path = url.pathname.length > 1 ? url.pathname.replace(/\/+$/, '') : '';
  return `${url.protocol}//${url.host}${path}${url.search}`;
}
