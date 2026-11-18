import { ChevronDown, ChevronRight, RotateCw, Trash2 } from 'lucide-react';
import { Fragment, useEffect, useState } from 'react';
import { listDocuments } from '../services/api';
import type { Source, SourceChild } from '../types';
import { timeAgo } from '../utils';
import Pager from './Pager';
import StatusBadge from './StatusBadge';
import TypeBadge from './TypeBadge';

// Must match the first page of children the API embeds in each source
const CHILDREN_PER_PAGE = 10;

interface Props {
  sources: Source[];
  page: number;
  hasNextPage: boolean;
  onPageChange: (page: number) => void;
  onRerun: (id: string) => void;
  onDeleteSource: (id: string) => void;
}

// Rerun and delete wait until the crawl or the previous rerun/delete has finished
const isBusy = (source: Source) => source.status === 'crawling' || source.status === 'cleaning_up';

function childCountLabel(source: Source): string | null {
  if (source.status === 'crawling') return 'crawling…';
  const n = source.childCount;
  if (n === 0) return null;
  return `${n} item${n === 1 ? '' : 's'} found`;
}

/**
 * One page of a source's children. Page 0 comes embedded in the source; later pages are
 * fetched with the cursor the previous page returned. Refetched whenever the source is
 * (each poll), so statuses stay current.
 */
function useChildrenPage(source: Source) {
  const [page, setPage] = useState(0);
  // cursors[i] fetches page i, recorded when the user moves forward
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [fetched, setFetched] = useState<{ page: number; children: SourceChild[]; nextCursor?: string } | null>(null);

  const pageCount = Math.max(1, Math.ceil(source.childCount / CHILDREN_PER_PAGE));
  // A rerun can shrink the list under the current page
  const safePage = page < pageCount ? page : 0;

  useEffect(() => {
    if (safePage === 0) return;
    let cancelled = false;
    listDocuments(source.id, CHILDREN_PER_PAGE, cursors[safePage])
      .then((result) => {
        if (!cancelled) setFetched({ page: safePage, children: result.documents, nextCursor: result.nextCursor });
      })
      .catch(() => undefined); // keep showing the last page; the next poll retries
    return () => {
      cancelled = true;
    };
  }, [source, safePage, cursors]);

  const onPage0 = safePage === 0;
  const current = !onPage0 && fetched?.page === safePage ? fetched : null;
  const children = onPage0 ? source.children : (current?.children ?? []);
  const nextCursor = onPage0 ? source.childrenNextCursor : current?.nextCursor;

  const goTo = (next: number) => {
    if (next > safePage) {
      setCursors((prev) => {
        const copy = prev.slice(0, safePage + 1);
        copy[next] = nextCursor;
        return copy;
      });
    }
    setPage(next);
  };

  return { page: safePage, pageCount, children, hasNext: Boolean(nextCursor), goTo };
}

interface SourceRowsProps {
  source: Source;
  onRerun: (source: Source) => void;
  onDelete: (source: Source) => void;
}

/** One source row plus, when open, all of its levels as a paginated tree. */
function SourceRows({ source, onRerun, onDelete }: SourceRowsProps) {
  const [open, setOpen] = useState(true);
  const childrenPage = useChildrenPage(source);

  const hasChildren = source.childCount > 0;
  const count = childCountLabel(source);
  const showFooter = open && hasChildren && childrenPage.pageCount > 1;

  return (
    <Fragment>
      <tr className="border-b border-[var(--color-border-soft)] bg-[var(--color-surface-muted)]">
        <td className="px-5 py-3.5">
          <div className="flex items-start gap-2">
            {hasChildren ? (
              <button
                type="button"
                className="mt-0.5 text-[var(--color-text-muted)]"
                aria-expanded={open}
                aria-label={`${open ? 'Collapse' : 'Expand'} ${source.title}`}
                onClick={() => setOpen((v) => !v)}
              >
                {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </button>
            ) : (
              <span className="w-3.5" aria-hidden="true" />
            )}
            <div className="min-w-0">
              <span className="font-semibold">{source.title}</span>
              {count && <span className="ml-2.5 text-xs text-[var(--color-text-muted)]">{count}</span>}
              <div className="mt-0.5 break-all font-mono text-xs text-[var(--color-text-secondary)]">{source.url}</div>
              {source.error && <div className="mt-1 text-xs text-[var(--color-error)]">{source.error}</div>}
            </div>
          </div>
        </td>
        <td className="px-5 py-3.5"><TypeBadge type={source.type} /></td>
        <td className="px-5 py-3.5 text-[var(--color-text-secondary)]">{timeAgo(source.addedAt)}</td>
        <td className="px-5 py-3.5"><StatusBadge status={source.status} /></td>
        <td className="px-5 py-3.5">
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="icon-btn"
              disabled={isBusy(source)}
              aria-label={`Rerun ${source.title}`}
              title="Delete old content and recrawl"
              onClick={() => onRerun(source)}
            >
              <RotateCw size={14} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="icon-btn"
              disabled={isBusy(source)}
              aria-label={`Delete ${source.title}`}
              title="Delete source and its documents"
              onClick={() => onDelete(source)}
            >
              <Trash2 size={14} aria-hidden="true" />
            </button>
          </div>
        </td>
      </tr>

      {open &&
        childrenPage.children.map((child) => (
          <tr key={child.id} className="border-b border-[var(--color-border-soft)]">
            <td className="py-3 pl-12 pr-5">
              <div
                className="border-l border-[var(--color-border)] pl-4"
                style={{ marginLeft: child.level > 1 ? `${(child.level - 1) * 1.5}rem` : undefined }}
              >
                {child.title}
                <span className="ml-2.5 text-xs text-[var(--color-text-muted)]">level {child.level}</span>
                <div className="mt-0.5 break-all font-mono text-xs text-[var(--color-text-secondary)]">{child.url}</div>
              </div>
            </td>
            <td className="px-5 py-3"><TypeBadge type={child.type} /></td>
            <td className="px-5 py-3" />
            <td className="px-5 py-3"><StatusBadge status={child.status} /></td>
            <td className="px-5 py-3" />
          </tr>
        ))}

      {showFooter && (
        <tr className="border-b border-[var(--color-border-soft)]">
          <td colSpan={5} className="px-5 py-2.5">
            <Pager
              page={childrenPage.page}
              pageCount={childrenPage.pageCount}
              hasNext={childrenPage.hasNext}
              onChange={childrenPage.goTo}
              label={`${source.title} documents`}
            />
          </td>
        </tr>
      )}
    </Fragment>
  );
}

export default function SourcesTable({ sources, page, hasNextPage, onPageChange, onRerun, onDeleteSource }: Props) {
  const handleRerun = (source: Source) => {
    const ok = window.confirm(
      `Rerun "${source.title}"?\n\nThis deletes its existing content from storage and search, then crawls the URL again.`,
    );
    if (ok) onRerun(source.id);
  };

  const handleDelete = (source: Source) => {
    const ok = window.confirm(
      `Delete "${source.title}"?\n\nThis permanently removes the source and all of its documents from storage and search. Content added outside this page is not affected.`,
    );
    if (ok) onDeleteSource(source.id);
  };

  if (sources.length === 0 && page === 0) {
    return <p className="card p-8 text-center text-sm text-[var(--color-text-secondary)]">No sources yet. Add a URL above.</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="card overflow-hidden">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)] text-left text-[0.7rem] uppercase tracking-wider text-[var(--color-text-muted)]">
              <th className="w-[48%] px-5 py-3 font-semibold">Source / Document</th>
              <th className="w-[14%] px-5 py-3 font-semibold">Type</th>
              <th className="w-[14%] px-5 py-3 font-semibold">Added</th>
              <th className="w-[16%] px-5 py-3 font-semibold">Status</th>
              <th className="w-[8%] px-5 py-3"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {sources.map((source) => (
              <SourceRows key={source.id} source={source} onRerun={handleRerun} onDelete={handleDelete} />
            ))}
          </tbody>
        </table>
      </div>
      <Pager page={page} hasNext={hasNextPage} onChange={onPageChange} label="Sources" />
    </div>
  );
}
