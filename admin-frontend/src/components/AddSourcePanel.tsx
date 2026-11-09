import { Plus } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import type { AddResult } from '../hooks/useSources';

const ERRORS: Record<Exclude<AddResult, 'added'>, string> = {
  unsupported: 'Unsupported URL',
  duplicate: 'URL already exists',
};

export default function AddSourcePanel({ onAdd }: { onAdd: (url: string) => AddResult }) {
  const [url, setUrl] = useState('');
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    const result = onAdd(url);
    if (result === 'added') {
      setUrl('');
      setError(null);
    } else {
      setError(ERRORS[result]);
    }
  };

  return (
    <form onSubmit={handleSubmit} noValidate className="card p-5">
      <div className="flex gap-3">
        <input
          type="url"
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setError(null);
          }}
          aria-label="Source URL"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? 'add-source-error' : undefined}
          placeholder="Paste a URL — page, YouTube, Panopto, or Google Drive link…"
          className="flex-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-muted)] px-4 py-2.5 font-mono text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-muted)] focus:outline-2 focus:outline-offset-2 focus:outline-[var(--color-highlight)]"
        />
        <button type="submit" className="btn-primary" disabled={!url.trim()}>
          <Plus size={16} aria-hidden="true" />
          Add source
        </button>
      </div>
      {error && (
        <p id="add-source-error" role="alert" className="mt-3 text-sm font-medium text-[var(--color-error)]">
          {error}
        </p>
      )}
      <p className="mt-3 text-xs text-[var(--color-text-muted)]">
        Public pages only. We crawl the page and its same-domain subpages (up to 2 levels), and download linked
        documents and videos. Nothing is searchable until you click “Ingest new documents”.
      </p>
    </form>
  );
}
