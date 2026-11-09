import type { ReactNode } from 'react';
import { EM_DASH, dirGlyph, dirOf, isNum } from '../lib/format';
import { TermLabel } from './Tooltip';
import { cn } from '../lib/cn';

// Re-exported so the ~15 call sites that already import it from here keep
// working; the definition moved to lib/cn to break the primitives↔Tooltip cycle.
export { cn };

/* ------------------------------------------------------------------ panel */

export function Panel({
  title,
  subtitle,
  right,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn('panel overflow-hidden', className)}>
      {(title || right) && (
        <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3 border-b border-line px-4 py-3.5">
          <div className="min-w-0">
            {title && <h2 className="text-[15px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-[12px] text-muted">{subtitle}</p>}
          </div>
          {right && <div className="shrink-0">{right}</div>}
        </header>
      )}
      <div className={cn('p-4', bodyClassName)}>{children}</div>
    </section>
  );
}

/* ------------------------------------------------------------------- chip */

export type Tone = 'neutral' | 'good' | 'warning' | 'serious' | 'critical' | 'info' | 'pos' | 'neg';

const CHIP_TONE: Record<Tone, string> = {
  // Tinted background + full-strength border of the mark token, and TEXT in
  // that hue's `*-ink` step — the one re-stepped until it cleared 4.5:1 on the
  // card. The mark colour and the label colour are different jobs; using one
  // token for both is how chips end up with 3:1 text in them. Status colour
  // never carries the meaning on its own either: every chip also prints a glyph
  // and a word.
  neutral: 'border-line bg-sunken text-ink2',
  good: 'border-good/40 bg-good/10 text-good-ink',
  warning: 'border-warning/50 bg-warning/10 text-ink',
  serious: 'border-serious/50 bg-serious/10 text-ink',
  critical: 'border-critical/50 bg-critical/10 text-critical-ink',
  info: 'border-accent/40 bg-accent/10 text-accent-ink',
  // Polarity, not status: these are the diverging poles, used where the chip
  // means "up / down", not "healthy / broken".
  pos: 'border-pos/40 bg-pos/10 text-pos-ink',
  neg: 'border-neg/40 bg-neg/10 text-neg-ink',
};

export function Chip({
  tone = 'neutral',
  glyph,
  children,
  title,
  className,
}: {
  tone?: Tone;
  glyph?: string;
  children: ReactNode;
  title?: string;
  className?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4',
        CHIP_TONE[tone],
        className,
      )}
    >
      {glyph && (
        <span aria-hidden="true" className="text-[10px] leading-none">
          {glyph}
        </span>
      )}
      <span className="truncate">{children}</span>
    </span>
  );
}

/* ------------------------------------------------------------------ delta */

/**
 * A signed number with its direction glyph. The glyph is not decoration: it is
 * the secondary encoding that keeps gain/loss readable without colour.
 */
export function Delta({
  value,
  format,
  className,
  showGlyph = true,
}: {
  value: unknown;
  format: (v: unknown) => string;
  className?: string;
  showGlyph?: boolean;
}) {
  const dir = dirOf(value);
  const tone = dir === 'up' ? 'text-pos-ink' : dir === 'down' ? 'text-neg-ink' : 'text-ink2';
  // No glyph on a missing value: an em-dash preceded by a direction mark reads
  // as "flat, and also nothing", which is one claim too many.
  const glyph = showGlyph && isNum(value) ? dirGlyph(value) : null;
  return (
    <span className={cn('tnum inline-flex items-center gap-1', tone, className)}>
      {glyph && (
        <span aria-hidden="true" className="text-[0.7em] leading-none">
          {glyph}
        </span>
      )}
      <span className={isNum(value) ? undefined : 'text-muted'}>{format(value)}</span>
    </span>
  );
}

/* ------------------------------------------------------------- stat tiles */

export function StatTile({
  label,
  value,
  delta,
  footnote,
  tone,
}: {
  label: string;
  value: ReactNode;
  delta?: ReactNode;
  footnote?: ReactNode;
  tone?: 'good' | 'warning' | 'serious' | 'critical';
}) {
  const accent =
    tone === 'good'
      ? 'text-good-ink'
      : tone === 'critical'
        ? 'text-critical-ink'
        : 'text-ink';
  return (
    <div className="panel px-4 py-3.5">
      <div className="label">{label}</div>
      {/* Proportional figures on purpose: tabular-nums makes a standalone
          value like 121 look loose at this size. Columns get tabular, tiles
          do not. */}
      <div className={cn('mt-1.5 text-[26px] font-semibold leading-tight tracking-[-0.02em]', accent)}>{value}</div>
      {delta && <div className="mt-1.5 text-xs">{delta}</div>}
      {footnote && <div className="mt-1.5 text-[11.5px] text-muted">{footnote}</div>}
    </div>
  );
}

/**
 * The one number a view leads with. Exactly one per view.
 *
 * `missing` matters more than it looks: an em-dash set at 48px renders as a
 * thick horizontal bar that reads as a redaction or a broken image, not as "no
 * value". When the figure is absent the hero steps down to body size, goes
 * muted, and says so in words.
 */
export function Hero({
  label,
  value,
  delta,
  footnote,
  missing = false,
  missingNote,
}: {
  label: string;
  value: ReactNode;
  delta?: ReactNode;
  footnote?: ReactNode;
  missing?: boolean;
  missingNote?: string;
}) {
  return (
    <div>
      <div className="label">{label}</div>
      {missing ? (
        <div className="mt-1 flex items-baseline gap-2">
          <span className="text-2xl font-semibold leading-none text-muted" aria-hidden="true">
            {EM_DASH}
          </span>
          <span className="text-sm text-ink2">{missingNote ?? 'not available'}</span>
        </div>
      ) : (
        <div className="mt-1 flex flex-wrap items-baseline gap-3">
          {/* Proportional figures: tabular-nums makes a display number look loose. */}
          <span className="text-hero font-semibold">{value}</span>
          {delta && <span className="text-sm">{delta}</span>}
        </div>
      )}
      {footnote && <div className="mt-1 text-xs text-muted">{footnote}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ meter */

const METER_FILL: Record<string, string> = {
  good: 'bg-good',
  warning: 'bg-warning',
  serious: 'bg-serious',
  critical: 'bg-critical',
  neutral: 'bg-s1',
  // The expected state. Twelve saturated green bars down a table spend the
  // colour budget on "nothing is wrong", which is exactly when it should be
  // spent on nothing — the ✓ OK chip beside it already says so in words.
  quiet: 'bg-line-strong',
};
const METER_TRACK: Record<string, string> = {
  good: 'bg-good/20',
  warning: 'bg-warning/20',
  serious: 'bg-serious/20',
  critical: 'bg-critical/20',
  neutral: 'bg-s1/20',
  quiet: 'bg-line',
};

/**
 * A headroom / progress meter.
 *
 * `fraction` is 0..1, which is the form this API speaks: `headroom_pct: 1.0`
 * means "all of it left", not "one percent left". The value is clamped into
 * the track so a malformed 1.4 cannot paint outside the card — clamping the
 * BAR only; the number printed beside a meter is always the raw one.
 *
 * The unfilled track is a lighter step of the fill's own hue, so the state
 * reads across the whole bar rather than only across the filled part.
 */
export function Meter({
  fraction,
  tone = 'neutral',
  label,
}: {
  fraction: unknown;
  tone?: 'good' | 'warning' | 'serious' | 'critical' | 'neutral' | 'quiet';
  label: string;
}) {
  if (!isNum(fraction)) {
    return (
      <div className="flex items-center gap-2">
        <div className={cn('h-2 w-full rounded-full', METER_TRACK.neutral)} />
        <span className="text-xs text-muted" aria-label={`${label}: no value`}>
          {EM_DASH}
        </span>
      </div>
    );
  }
  const w = Math.max(0, Math.min(100, fraction * 100));
  return (
    <div
      className={cn('h-2 w-full overflow-hidden rounded-full', METER_TRACK[tone])}
      role="meter"
      aria-valuenow={Math.round(w)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <div className={cn('h-full rounded-full', METER_FILL[tone])} style={{ width: `${w}%` }} />
    </div>
  );
}

/* --------------------------------------------------------- segmented bar */

export type Segment = { name: string; value: number; className: string };

/**
 * One whole split into parts. Thin (8px), fully rounded, no baseline, no inline
 * text — the numbers live beside it. A 2px surface gap separates the parts; no
 * borders, which would add ink that is not data.
 */
export function SegmentedBar({ segments, label }: { segments: Segment[]; label: string }) {
  const usable = segments.filter((s) => isNum(s.value) && s.value > 0);
  const total = usable.reduce((a, s) => a + s.value, 0);
  if (total <= 0) {
    return <div className="h-2 w-full rounded-full bg-sunken" aria-label={`${label}: no value`} />;
  }
  return (
    <div className="flex h-2 w-full gap-[2px]" role="img" aria-label={`${label}. ${usable.map((s) => `${s.name} ${s.value}`).join(', ')}`}>
      {usable.map((s) => (
        <div
          key={s.name}
          className={cn('h-full rounded-full', s.className)}
          style={{ width: `${(s.value / total) * 100}%` }}
          title={`${s.name}: ${s.value}`}
        />
      ))}
    </div>
  );
}

/* ----------------------------------------------------------------- drawer */

/**
 * Native <details> on purpose: Space/Enter toggle, correct AT semantics and
 * find-in-page expansion all come free, and none of that is worth
 * re-implementing with a div and aria-expanded.
 */
export function Drawer({
  summary,
  count,
  children,
  defaultOpen = false,
}: {
  summary: ReactNode;
  count?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  return (
    <details className="panel group overflow-hidden" open={defaultOpen}>
      <summary className="flex cursor-pointer select-none items-center gap-2 px-4 py-3.5 text-[14px] font-semibold text-ink marker:content-[''] hover:bg-sunken">
        <span aria-hidden="true" className="text-xs text-muted transition-transform group-open:rotate-90">
          ▸
        </span>
        <span>{summary}</span>
        {count !== undefined && <span className="text-[12px] font-normal text-muted">{count}</span>}
      </summary>
      <div className="border-t border-line p-4">{children}</div>
    </details>
  );
}

/* ------------------------------------------------------------- empty note */

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-muted">{children}</p>;
}

/**
 * A small label/value row used all over the cards.
 *
 * `term` attaches the server's glossary entry to the LABEL rather than to the
 * number: the label is the thing a reader does not understand, and a tooltip on
 * a figure invites the reading that the figure itself is being explained away.
 */
export function Field({
  label,
  term,
  children,
  className,
  size = 'sm',
  tone,
  glyph,
}: {
  label: string;
  term?: string;
  children: ReactNode;
  className?: string;
  size?: 'sm' | 'lg';
  /** Polarity of the figure, for the plan levels. Always paired with `glyph`. */
  tone?: 'pos' | 'neg';
  glyph?: string;
}) {
  // `min-w-0` is load-bearing, not tidiness. A grid item defaults to
  // `min-width: auto`, so it refuses to shrink below its content even when its
  // track is `minmax(0, 1fr)` — which is exactly how a long entry range
  // ("₹2,966.49 – ₹2,996.31") overran the stop cell beside it and printed two
  // prices as one unreadable number.
  const ink = tone === 'pos' ? 'text-pos-ink' : tone === 'neg' ? 'text-neg-ink' : 'text-ink';
  return (
    <div className={cn('min-w-0', className)}>
      {term ? <TermLabel code={term}>{label}</TermLabel> : <div className="label">{label}</div>}
      <div className={cn('tnum mt-0.5 flex items-baseline gap-1', ink, size === 'lg' ? 'text-[15px] font-semibold' : 'text-sm')}>
        {glyph && (
          <span aria-hidden="true" className="shrink-0 text-[0.7em] leading-none">
            {glyph}
          </span>
        )}
        <span className="min-w-0">{children}</span>
      </div>
    </div>
  );
}

/* --------------------------------------------------------- disclosure */

/**
 * An inline expand/collapse for the secondary half of a card.
 *
 * Native <details> again: Space/Enter, correct AT semantics and find-in-page
 * expansion come free. `open` is controlled from outside so one toggle at the
 * top of Today can expand every card at once without each card inventing its
 * own state.
 */
export function Disclosure({
  summary,
  open,
  onToggle,
  children,
}: {
  summary: ReactNode;
  open: boolean;
  onToggle: (next: boolean) => void;
  children: ReactNode;
}) {
  return (
    <details
      className="group"
      open={open}
      onToggle={(e) => onToggle((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="flex cursor-pointer select-none items-center gap-1.5 rounded py-1 text-[11.5px] font-medium text-ink2 marker:content-[''] hover:text-ink">
        <span aria-hidden="true" className="text-[9px] text-muted transition-transform group-open:rotate-90">
          ▶
        </span>
        {summary}
      </summary>
      <div className="mt-2 border-t border-line pt-3">{children}</div>
    </details>
  );
}
