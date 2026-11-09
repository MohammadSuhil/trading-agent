import { Chip, cn, type Tone } from './primitives';
import { Term } from './Tooltip';
import { useGlossary } from '../lib/glossary';
import { humanEnum, isStr, text } from '../lib/format';

/**
 * Every closed enum in the contract, rendered as a chip.
 *
 * The rule the whole file exists to enforce: a state is NEVER conveyed by
 * colour alone. Each entry carries a glyph and the word, so the screen still
 * works in greyscale, under forced-colors, and for a red-green-colourblind
 * reader. Unknown values fall through to a neutral chip showing the raw string
 * rather than being swallowed — a new server-side enum should be visible, not
 * invisible.
 *
 * Where /api/glossary defines the code, the chip carries the server's wording
 * as its tooltip. Nothing in this file writes a definition of its own; the two
 * remaining hand-written `title`s (DEGRADED, RECONSTRUCTED) describe the state
 * of this dashboard's own plumbing, which the glossary does not cover and is
 * not a trading term.
 */

type Spec = { tone: Tone; glyph: string; label?: string; title?: string };

const UNKNOWN: Spec = { tone: 'neutral', glyph: '?' };

function render(value: unknown, table: Record<string, Spec>, fallbackLabel?: string) {
  if (!isStr(value)) {
    return (
      <Chip tone="neutral" glyph="?" title="The API did not send this field">
        {fallbackLabel ?? 'unknown'}
      </Chip>
    );
  }
  const spec = table[value.toUpperCase()] ?? UNKNOWN;
  return (
    <Chip tone={spec.tone} glyph={spec.glyph} title={spec.title}>
      {spec.label ?? humanEnum(value)}
    </Chip>
  );
}

/** As `render`, but the chip body is a glossary term when the server defines it. */
function renderTermed(value: unknown, table: Record<string, Spec>, termCode?: string) {
  if (!isStr(value)) {
    return (
      <Chip tone="neutral" glyph="?" title="The API did not send this field">
        unknown
      </Chip>
    );
  }
  const spec = table[value.toUpperCase()] ?? UNKNOWN;
  return (
    <Chip tone={spec.tone} glyph={spec.glyph} title={spec.title}>
      <Term code={termCode ?? value}>{spec.label ?? humanEnum(value)}</Term>
    </Chip>
  );
}

/* --------------------------------------------------- /api/health.status */

const HEALTH: Record<string, Spec> = {
  OK: { tone: 'good', glyph: '✓', label: 'Healthy' },
  DEGRADED: { tone: 'warning', glyph: '!', label: 'Degraded', title: 'Some services are down — numbers may be stale' },
};
export const HealthChip = ({ value }: { value: unknown }) => render(value, HEALTH);

/* ---------------------------------------------------- data_quality flag */

const QUALITY: Record<string, Spec> = {
  OK: { tone: 'good', glyph: '✓', label: 'Data OK' },
  PARTIAL: { tone: 'warning', glyph: '!', label: 'Data PARTIAL', title: 'Part of the input set was unavailable' },
};
export const QualityChip = ({ value }: { value: unknown }) => render(value, QUALITY);

/* ------------------------------------------------------- /api/context.regime */

const REGIME: Record<string, Spec> = {
  TREND_UP: { tone: 'pos', glyph: '▲', label: 'Trend up' },
  TREND_DOWN: { tone: 'neg', glyph: '▼', label: 'Trend down' },
  CHOP: { tone: 'neutral', glyph: '≈', label: 'Chop' },
  HIGH_VOL: { tone: 'warning', glyph: '!', label: 'High vol' },
  UNKNOWN: { tone: 'neutral', glyph: '?', label: 'Unknown' },
};
// The glossary defines `regime` (what the field is), not each member, so that
// is the entry the chip carries.
export const RegimeChip = ({ value }: { value: unknown }) => renderTermed(value, REGIME, 'regime');

/* -------------------------------------------------------- /api/risk.state */

const RULE: Record<string, Spec> = {
  OK: { tone: 'good', glyph: '✓', label: 'OK' },
  WARN: { tone: 'warning', glyph: '!', label: 'WARN' },
  BREACH: { tone: 'critical', glyph: '✕', label: 'BREACH' },
  DISPLAY_ONLY: { tone: 'neutral', glyph: '◦', label: 'DISPLAY ONLY' },
};
export const RuleStateChip = ({ value }: { value: unknown }) =>
  // DISPLAY_ONLY is in the glossary; OK / WARN / BREACH are not, and Term
  // passes those through as plain text.
  renderTermed(value, RULE, isStr(value) ? value : undefined);

/* ------------------------------------------- context.benchmark.tri_source */

const TRI: Record<string, Spec> = {
  PUBLISHED: { tone: 'neutral', glyph: '✓', label: 'TRI published' },
  RECONSTRUCTED: {
    tone: 'warning',
    glyph: '!',
    label: 'TRI reconstructed',
    title: 'Total-return index was rebuilt locally, not taken from the publisher',
  },
};
export const TriSourceChip = ({ value }: { value: unknown }) => render(value, TRI);

/* ----------------------------------------- calibration / simulation state */

const SUFFICIENCY: Record<string, Spec> = {
  OK: { tone: 'good', glyph: '✓', label: 'Sufficient' },
  INSUFFICIENT: { tone: 'warning', glyph: '!', label: 'Insufficient' },
};
export const SufficiencyChip = ({ value }: { value: unknown }) => render(value, SUFFICIENCY);

/* --------------------------------------------------- free-text enums */

/**
 * `trend_state`, `sleeve`, `horizon` — open vocabularies, neutral chip.
 *
 * Where the glossary defines the value the chip shows the server's plain-English
 * `short` instead of the raw code, because the reader is not a fund manager and
 * `PULLBACK_IN_UPTREND` is not a sentence. The raw code is still on screen in
 * the tooltip footer, so a screenshot can be matched against the ledger.
 */
export function TagChip({
  value,
  title,
  plain = false,
}: {
  value: unknown;
  title?: string;
  /** Show the glossary's plain wording as the chip label. */
  plain?: boolean;
}) {
  const g = useGlossary();
  if (!isStr(value)) return null;
  const entry = g.get(value);
  const label = plain && entry ? text(entry.short, value) : value;
  return (
    <Chip tone="neutral" title={entry ? undefined : title}>
      <Term code={value}>{label}</Term>
    </Chip>
  );
}

/**
 * A reason code.
 *
 * Plain English first, code second and muted — the owner's complaint was that
 * `MA_STACK_ALIGNED` tells a non-specialist nothing, and the server already
 * ships a 2–6 word rendering of it. Both are on screen so neither audience
 * loses: the wording for a reader, the code for a grep.
 */
export function ReasonChip({ code, compact = false }: { code: unknown; compact?: boolean }) {
  const g = useGlossary();
  if (!isStr(code)) return null;
  const entry = g.get(code);
  if (!entry) {
    return (
      <Chip tone="neutral" className="max-w-[9rem]">
        <span className="truncate font-mono text-[10px]">{code}</span>
      </Chip>
    );
  }
  return (
    <Chip tone="neutral" className={compact ? 'max-w-[8.5rem] shrink-0' : 'max-w-full'}>
      <Term code={code} className="inline-flex max-w-full items-baseline gap-1.5">
        <span className="truncate">{text(entry.short, code)}</span>
        {!compact && <span className="font-mono text-[9px] uppercase text-muted">{code}</span>}
      </Term>
    </Chip>
  );
}

/**
 * A service up/down dot.
 *
 * `onRail` is a surface switch, not a different meaning: on the navy rail the
 * tinted-chip treatment has nothing to tint against, so the state moves to a
 * coloured dot beside white-ish type. Glyph and word are unchanged.
 */
export function ServiceChip({ name, up, onRail = false }: { name: string; up: unknown; onRail?: boolean }) {
  const known = typeof up === 'boolean';
  const title = !known ? `${name}: not reported` : up ? `${name} is up` : `${name} is down`;

  if (onRail) {
    return (
      <span
        title={title}
        className="inline-flex items-center gap-1.5 rounded-full bg-white/[0.07] px-2 py-0.5 text-[10.5px] font-medium text-rail-ink"
      >
        <span
          aria-hidden="true"
          className={cn('h-1.5 w-1.5 rounded-full', !known ? 'bg-rail-muted' : up ? 'bg-good' : 'bg-critical')}
        />
        <span aria-hidden="true" className="sr-only">
          {!known ? 'unknown' : up ? 'up' : 'down'}
        </span>
        {name}
      </span>
    );
  }

  return (
    <Chip
      tone={!known ? 'neutral' : up ? 'good' : 'critical'}
      glyph={!known ? '?' : up ? '✓' : '✕'}
      title={title}
    >
      {name}
    </Chip>
  );
}
