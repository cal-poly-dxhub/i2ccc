import { ChevronLeft, ChevronRight } from 'lucide-react';

interface PagerProps {
  page: number; // zero-based
  /** Total pages, when known. Without it the pager shows only the current page number. */
  pageCount?: number;
  hasNext: boolean;
  onChange: (page: number) => void;
  label: string;
}

export default function Pager({ page, pageCount, hasNext, onChange, label }: PagerProps) {
  if (page === 0 && !hasNext) return null;
  return (
    <nav aria-label={label} className="flex items-center justify-end gap-2 text-xs text-[var(--color-text-secondary)]">
      <button type="button" className="icon-btn" disabled={page === 0} aria-label={`${label}: previous page`} onClick={() => onChange(page - 1)}>
        <ChevronLeft size={14} aria-hidden="true" />
      </button>
      <span aria-live="polite">
        Page {page + 1}
        {pageCount ? ` of ${pageCount}` : ''}
      </span>
      <button
        type="button"
        className="icon-btn"
        disabled={!hasNext}
        aria-label={`${label}: next page`}
        onClick={() => onChange(page + 1)}
      >
        <ChevronRight size={14} aria-hidden="true" />
      </button>
    </nav>
  );
}
