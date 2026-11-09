import type { ReactNode } from 'react';
import { cn } from '../lib/cn';
import { TermInfo } from './Tooltip';
import { useGlossary } from '../lib/glossary';
import { text } from '../lib/format';

/**
 * The KPI tile.
 *
 * Reference anatomy, kept exactly: small uppercase label, a ⓘ in the corner,
 * one big figure, a one-line description beneath it, a delta, and a "drill in"
 * link. Two rules this file adds on top of the look:
 *
 *   • The description is NEVER written here. It is the glossary's own `short`
 *     for the code the tile is about, so the sentence under a number cannot
 *     drift away from the rule that produced it. A tile with no glossary code
 *     simply has no description line.
 *   • A missing figure is an em-dash at body size with a reason beside it, not
 *     an em-dash at 36px — which reads as a redaction, not as "no value".
 */
export function Kpi({
  label,
  term,
  value,
  unit,
  description,
  delta,
  accent,
  drill,
  drillLabel = 'Drill in',
  missing = false,
  missingNote,
}: {
  label: string;
  /** Glossary code. Drives both the ⓘ and the description line. */
  term?: string;
  value: ReactNode;
  unit?: ReactNode;
  /** Overrides the glossary description; use only for non-glossary tiles. */
  description?: ReactNode;
  delta?: ReactNode;
  /** A 3px top rule in the tile's own colour — the tile's identity channel. */
  accent?: string;
  drill?: string;
  drillLabel?: string;
  missing?: boolean;
  missingNote?: string;
}) {
  const g = useGlossary();
  const entry = term ? g.get(term) : null;
  const desc = description ?? (entry ? text(entry.short, '') : '');

  return (
    <div className="panel relative flex flex-col overflow-hidden p-4">
      {accent && <span aria-hidden="true" className={cn('absolute inset-x-0 top-0 h-[3px]', accent)} />}

      <div className="flex items-start justify-between gap-2">
        <span className="label leading-4">{label}</span>
        {term && <TermInfo code={term} className="mt-[1px]" />}
      </div>

      {missing ? (
        <div className="mt-2 flex items-baseline gap-2">
          <span aria-hidden="true" className="text-xl font-semibold leading-none text-muted">
            —
          </span>
          <span className="text-[12px] text-ink2">{missingNote ?? 'not available'}</span>
        </div>
      ) : (
        <div className="mt-1.5 flex items-baseline gap-1.5">
          {/* Proportional figures on purpose: tabular-nums makes a standalone
              display number look loose at this size. Columns get tabular. */}
          <span className="text-kpi font-semibold text-ink">{value}</span>
          {unit && <span className="text-[13px] font-medium text-ink2">{unit}</span>}
        </div>
      )}

      {desc && <p className="mt-1.5 text-[11.5px] leading-snug text-muted">{desc}</p>}

      <div className="mt-auto flex items-end justify-between gap-3 pt-3">
        <div className="text-[11.5px]">{delta}</div>
        {drill && (
          <a
            href={drill}
            className="shrink-0 text-[11.5px] font-medium text-accent-ink hover:underline"
          >
            {drillLabel} <span aria-hidden="true">→</span>
          </a>
        )}
      </div>
    </div>
  );
}

/**
 * Pill tabs with counts.
 *
 * Active is a solid fill in the tab's own colour with white type; inactive is a
 * white pill with a hairline and a colour dot. So the active tab is identified
 * by fill, by weight and by contrast — three channels — and each tab keeps its
 * hue even when it is not selected, which is what lets the owner aim at one
 * without reading the labels.
 */
export type Tab = {
  id: string;
  label: string;
  count?: number;
  /** Classes for the solid active state, e.g. 'bg-buy-fill text-white'. */
  activeClass?: string;
  /** Classes for the inactive dot, e.g. 'bg-buy-fill'. */
  dotClass?: string;
  glyph?: string;
  title?: string;
};

export function PillTabs({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: Tab[];
  value: string;
  onChange: (id: string) => void;
  label: string;
}) {
  return (
    <div role="tablist" aria-label={label} className="flex flex-wrap items-center gap-2">
      {tabs.map((t) => {
        const active = t.id === value;
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={active}
            title={t.title}
            onClick={() => onChange(t.id)}
            className={cn(
              'inline-flex items-center gap-2 rounded-lg border px-3.5 py-2 text-[12.5px] font-semibold transition-colors',
              active
                ? cn('border-transparent shadow-raise', t.activeClass ?? 'bg-rail-active text-white')
                : 'border-line bg-surface text-ink2 hover:border-line-strong hover:text-ink',
            )}
          >
            {!active && t.dotClass && (
              <span aria-hidden="true" className={cn('h-2 w-2 shrink-0 rounded-full', t.dotClass)} />
            )}
            {active && t.glyph && (
              <span aria-hidden="true" className="text-[10px] leading-none">
                {t.glyph}
              </span>
            )}
            <span>{t.label}</span>
            {typeof t.count === 'number' && (
              <span className={cn('tnum text-[11.5px] font-medium', active ? 'text-white/75' : 'text-muted')}>
                ({t.count})
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/**
 * A small status pill for inside a list row — the reference's `Critical` /
 * `High confidence` items. Glyph + word always; colour third.
 */
export function Pill({
  tone = 'neutral',
  glyph,
  children,
  className,
}: {
  tone?: 'neutral' | 'good' | 'warning' | 'critical' | 'accent' | 'quiet';
  glyph?: string;
  children: ReactNode;
  className?: string;
}) {
  const tones: Record<string, string> = {
    neutral: 'border-line bg-sunken text-ink2',
    good: 'border-good/35 bg-good/10 text-good-ink',
    warning: 'border-warning/45 bg-warning/[0.14] text-warning-ink',
    critical: 'border-critical/35 bg-critical/10 text-critical-ink',
    accent: 'border-accent/30 bg-accent-soft text-accent-ink',
    quiet: 'border-transparent bg-transparent text-muted',
  };
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[10.5px] font-semibold leading-4',
        tones[tone],
        className,
      )}
    >
      {glyph && (
        <span aria-hidden="true" className="text-[9px] leading-none">
          {glyph}
        </span>
      )}
      {children}
    </span>
  );
}

/** A section header with a title, optional count and an optional right slot. */
export function SectionHead({
  title,
  subtitle,
  right,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>
        {subtitle && <p className="mt-0.5 text-[12px] text-muted">{subtitle}</p>}
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  );
}
