import { AlertTriangle, Check, Loader2 } from 'lucide-react';
import { STATUS_META, type StatusTone } from '../data/sourceMeta';
import type { ChildStatus, SourceStatus } from '../types';

const TONE_CLASS: Record<StatusTone, string> = {
  success: 'bg-[color-mix(in_srgb,var(--color-success)_16%,transparent)] text-[var(--color-success)]',
  warning: 'bg-[color-mix(in_srgb,var(--color-warning)_16%,transparent)] text-[var(--color-warning)]',
  error: 'bg-[color-mix(in_srgb,var(--color-error)_16%,transparent)] text-[var(--color-error)]',
  neutral: 'bg-[var(--color-surface-muted)] text-[var(--color-text-secondary)]',
};

export default function StatusBadge({ status }: { status: SourceStatus | ChildStatus }) {
  const { label, tone, active } = STATUS_META[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${TONE_CLASS[tone]}`}>
      {active && <Loader2 size={12} className="spin" aria-hidden="true" />}
      {tone === 'success' && <Check size={12} aria-hidden="true" />}
      {tone === 'error' && <AlertTriangle size={12} aria-hidden="true" />}
      {label}
    </span>
  );
}
