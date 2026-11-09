import { ChevronDown, ChevronRight, RotateCw, Trash2 } from 'lucide-react';
import { Fragment, useState } from 'react';
import type { Source, SourceChild } from '../types';
import { timeAgo } from '../utils';
import Pager from './Pager';
import StatusBadge from './StatusBadge';
import TypeBadge from './TypeBadge';

const SOURCES_PER_PAGE = 10;
const CHILDREN_PER_PAGE = 10;

interface Props {
  sources: Source[];
  onRerun: (id: string) => void;
  onDeleteSource: (id: string) => void;
}

function childCountLabel(source: Source): string | null {
  if (source.status === 'crawling') return 'crawling…';
  const n = source.children?.length;
  if (n === undefined) return null;
  return `${n} item${n === 1 ? '' : 's'} found`;
}

interface TreeRow {
  child: SourceChild;
  /** 1 for items found directly on the source, 2 for items found on those pages, and so on. */
  level: number;
}

/** Flattens the children into display order: each page followed by everything found on it. */
function flattenTree(children: SourceChild[]): TreeRow[] {
  const ids = new Set(children.map((c) => c.id));
  const byParent = new Map<string | undefined, SourceChild[]>();
  for (const child of children) {
    // A missing or unknown parent means it was found on the source itself
    const key = child.parentId && ids.has(child.parentId) ? child.parentId : undefined;
    byParent.set(key, [...(byParent.get(key) ?? []), child]);
  }

  const rows: TreeRow[] = [];
  const seen = new Set<string>();
  const walk = (parent: string | undefined, level: number) => {
    for (const child of byParent.get(parent) ?? []) {
      if (seen.has(child.id)) continue;
      seen.add(child.id);
      rows.push({ child, level });
      walk(child.id, level + 1);
    }
  };
  walk(undefined, 1);
  return rows;
}

interface SourceRowsProps {
  source: Source;
  onRerun: (source: Source) => void;
  onDelete: (source: Source) => void;
}

/** One source row plus, when open, all of its levels as a paginated tree. */
function SourceRows({ source, onRerun, onDelete }: SourceRowsProps) {
  const [open, setOpen] = useState(true);
  const [page, setPage] = useState(0);

  const children = source.children ?? [];
  const hasChildren = children.length > 0;
  const count = childCountLabel(source);

  const tree = flattenTree(children);
  const pageCount = Math.max(1, Math.ceil(tree.length / CHILDREN_PER_PAGE));
  const safePage = Math.min(page, pageCount - 1);
  const visible = tree.slice(safePage * CHILDREN_PER_PAGE, (safePage + 1) * CHILDREN_PER_PAGE);
  const showFooter = open && hasChildren && pageCount > 1;

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
              disabled={source.status === 'crawling'}
              aria-label={`Rerun ${source.title}`}
              title="Delete old content and recrawl"
              onClick={() => onRerun(source)}
            >
              <RotateCw size={14} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="icon-btn"
              disabled={source.status === 'crawling'}
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
        visible.map(({ child, level }) => (
          <tr key={child.id} className="border-b border-[var(--color-border-soft)]">
            <td className="py-3 pl-12 pr-5">
              <div
                className="border-l border-[var(--color-border)] pl-4"
                style={{ marginLeft: level > 1 ? `${(level - 1) * 1.5}rem` : undefined }}
              >
                {child.title}
                <span className="ml-2.5 text-xs text-[var(--color-text-muted)]">level {level}</span>
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
            <Pager page={safePage} pageCount={pageCount} onChange={setPage} label={`${source.title} documents`} />
          </td>
        </tr>
      )}
    </Fragment>
  );
}

export default function SourcesTable({ sources, onRerun, onDeleteSource }: Props) {
  const [page, setPage] = useState(0);

  const pageCount = Math.max(1, Math.ceil(sources.length / SOURCES_PER_PAGE));
  const safePage = Math.min(page, pageCount - 1);
  const visible = sources.slice(safePage * SOURCES_PER_PAGE, (safePage + 1) * SOURCES_PER_PAGE);

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

  if (sources.length === 0) {
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
            {visible.map((source) => (
              <SourceRows key={source.id} source={source} onRerun={handleRerun} onDelete={handleDelete} />
            ))}
          </tbody>
        </table>
      </div>
      <Pager page={safePage} pageCount={pageCount} onChange={setPage} label="Sources" />
    </div>
  );
}
