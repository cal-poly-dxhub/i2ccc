import type { DocumentsPage, Source, SourcesPage } from '../types';

// In development Vite proxies /api/ to the deployed API (see vite.config.ts), which avoids CORS
const baseUrl = import.meta.env.DEV ? '/api/' : (import.meta.env.VITE_API_ENDPOINT as string);

/** An error response from the admin API. code is the API's error string, e.g. "url_already_exists". */
export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string) {
    super(`${status} ${code}`);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const idToken = sessionStorage.getItem('cognito_id_token');
  if (idToken) headers.Authorization = `Bearer ${idToken}`;

  const response = await fetch(`${baseUrl.replace(/\/$/, '')}/${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401) {
    // Expired or rejected token: sign in again
    sessionStorage.removeItem('cognito_id_token');
    sessionStorage.removeItem('cognito_access_token');
    window.location.reload();
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(response.status, typeof data.error === 'string' ? data.error : 'request_failed');
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

const pageQuery = (limit: number, cursor?: string) =>
  new URLSearchParams({ limit: String(limit), ...(cursor ? { cursor } : {}) }).toString();

export const listSources = (limit: number, cursor?: string) =>
  request<SourcesPage>('GET', `sources?${pageQuery(limit, cursor)}`);

export const listDocuments = (sourceId: string, limit: number, cursor?: string) =>
  request<DocumentsPage>('GET', `sources/${encodeURIComponent(sourceId)}/documents?${pageQuery(limit, cursor)}`);

export const addSource = (url: string) => request<Source>('POST', 'sources', { url });

export const rerunSource = (id: string) => request<Source>('POST', `sources/${encodeURIComponent(id)}/rerun`);

export const deleteSource = (id: string) => request<void>('DELETE', `sources/${encodeURIComponent(id)}`);

export const startIngest = () => request<{ executionArn: string }>('POST', 'ingest');
