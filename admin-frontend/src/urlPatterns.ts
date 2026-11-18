// The URLs an admin may add. The list is shared with the backend (shared/source_urls.yaml),
// which applies the same rules in src/admin_api/url_check.py; keep the two in step.
import patterns from '../../shared/source_urls.yaml';
import type { SourceType } from './types';

interface SourcePattern {
  label: string;
  type: SourceType;
  pattern: string;
  examples?: string[];
}

const SCHEMES = ['https', 'http'];
const SOURCES = (patterns.sources as Omit<SourcePattern, 'regex'>[]).map((s) => ({
  ...s,
  regex: new RegExp(s.pattern),
}));

/** Labels of the listed sources, for help text. */
export const SUPPORTED_SOURCE_LABELS = SOURCES.map((s) => s.label);

/**
 * Normalized form of a plain http(s) URL on a default port, or null.
 * Lowercase scheme and host, no fragment, no trailing slashes on the path, query kept.
 */
export function normalizeUrl(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return null;
  }
  const scheme = url.protocol.replace(/:$/, '');
  // URL drops a default port, so any port left is a non-default one
  if (!SCHEMES.includes(scheme) || !url.hostname || url.port || url.username || url.password) return null;
  const path = url.pathname.replace(/\/+$/, '');
  return `${scheme}://${url.hostname}${path}${url.search}`;
}

/** The listed source a URL matches, with its type, or null if the URL is not supported. */
export function matchSource(raw: string): { pattern: SourcePattern; type: SourceType } | null {
  const normalized = normalizeUrl(raw);
  if (!normalized) return null;
  const lowered = normalized.toLowerCase(); // patterns are lowercase; the stored URL keeps its case
  const entry = SOURCES.find((s) => s.regex.test(lowered));
  return entry ? { pattern: entry, type: entry.type } : null;
}
