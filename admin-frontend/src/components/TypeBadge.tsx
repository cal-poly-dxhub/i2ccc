import { TYPE_META } from '../data/sourceMeta';
import type { SourceType } from '../types';

export default function TypeBadge({ type }: { type: SourceType }) {
  const { label, color } = TYPE_META[type];
  return (
    <span className="inline-flex items-center gap-2 text-xs text-[var(--color-text-secondary)]">
      <span className="h-2.5 w-2.5 rounded-full" style={{ background: color }} aria-hidden="true" />
      {label}
    </span>
  );
}
