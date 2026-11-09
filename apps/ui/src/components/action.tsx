import { useGlossary } from '../lib/glossary';
import { InfoPop } from './Tooltip';
import { cn } from './primitives';
import { text } from '../lib/format';

/**
 * Action tokens — one table, used by Today, Screener and Portfolio alike.
 *
 * The product is the distinction between "buy this" and "we ranked this but you
 * are not being told to buy it". So the six actions are separated on THREE
 * channels at once, any one of which is enough on its own:
 *
 *   • FORM    solid fill = transact today (BUY, SELL). Everything else is an
 *             outline. This is the channel that survives a greyscale print and
 *             a 1-second glance, and it is the one doing most of the work.
 *   • GLYPH   ▲ ◇ = ½ ▼ ✕ — distinct shapes, not six variations of a triangle.
 *   • WORD    always spelled out. Never an icon alone.
 *
 * Hue is the fourth channel and the least load-bearing: blue BUY / neutral
 * WATCH / aqua HOLD / orange TRIM / red SELL / grey AVOID, from the validated
 * slots. Note that WATCH is deliberately the quietest thing on the screen — it
 * is the absence of an instruction, and it should not look like one.
 *
 * HOLD, TRIM and SELL only ever describe something already in the portfolio;
 * BUY, WATCH and AVOID describe a candidate. That split is the backend's, and
 * `family` carries it so a view can say which kind of list it is showing.
 */

export type ActionCode = 'BUY' | 'WATCH' | 'HOLD' | 'TRIM' | 'SELL' | 'AVOID';

export type ActionSpec = {
  code: ActionCode;
  glyph: string;
  /** Solid fill = an instruction to transact today. */
  solid: boolean;
  family: 'candidate' | 'position';
  /** Badge classes. */
  badge: string;
  /** The 3px rail down the left of a card or row, and the dot on a tab. */
  rail: string;
  /** The solid state of a pill tab for this action. */
  tab: string;
  /** The 3px top rule on the KPI tile that counts this action. */
  tile: string;
};

export const ACTION_ORDER: ActionCode[] = ['BUY', 'WATCH', 'HOLD', 'TRIM', 'SELL', 'AVOID'];

export const ACTIONS: Record<ActionCode, ActionSpec> = {
  BUY: {
    code: 'BUY',
    glyph: '▲',
    solid: true,
    family: 'candidate',
    badge: 'border-buy-fill bg-buy-fill text-white',
    rail: 'bg-buy-fill',
    tab: 'bg-buy-fill text-white',
    tile: 'bg-buy-fill',
  },
  WATCH: {
    code: 'WATCH',
    glyph: '◇',
    solid: false,
    family: 'candidate',
    badge: 'border-dashed border-line-strong bg-surface text-ink2',
    rail: 'bg-line-strong',
    // rail-active, not rail: the rail colour is near-black in dark mode and an
    // active tab painted in it reads as a hole in the page.
    tab: 'bg-rail-active text-white',
    tile: 'bg-line-strong',
  },
  HOLD: {
    code: 'HOLD',
    glyph: '=',
    solid: false,
    family: 'position',
    badge: 'border-s3/45 bg-s3/10 text-s3-ink',
    rail: 'bg-s3',
    tab: 'bg-s3 text-white',
    tile: 'bg-s3',
  },
  TRIM: {
    code: 'TRIM',
    glyph: '½',
    solid: false,
    family: 'position',
    badge: 'border-s2/45 bg-s2/10 text-s2-ink',
    rail: 'bg-s2',
    tab: 'bg-s2 text-white',
    tile: 'bg-s2',
  },
  SELL: {
    code: 'SELL',
    glyph: '▼',
    solid: true,
    family: 'position',
    badge: 'border-sell-fill bg-sell-fill text-white',
    rail: 'bg-sell-fill',
    tab: 'bg-sell-fill text-white',
    tile: 'bg-sell-fill',
  },
  AVOID: {
    code: 'AVOID',
    glyph: '✕',
    solid: false,
    family: 'candidate',
    badge: 'border-line bg-sunken text-muted',
    rail: 'bg-line-strong',
    tab: 'bg-line-strong text-ink',
    tile: 'bg-line-strong',
  },
};

export function actionSpec(value: unknown): ActionSpec | null {
  if (typeof value !== 'string') return null;
  const k = value.trim().toUpperCase();
  return (ACTIONS as Record<string, ActionSpec | undefined>)[k] ?? null;
}

/** The rail/dot colour for a row whose action may be unknown. */
export function actionRail(value: unknown): string {
  return actionSpec(value)?.rail ?? 'bg-line';
}

/**
 * The action badge: colour + glyph + word, with the server's own one-line
 * definition on hover and on focus.
 *
 * An action the table does not know is shown verbatim in a neutral badge rather
 * than swallowed — a seventh enum member landing in production should be
 * visible on the screen, not invisible.
 */
export function ActionBadge({
  value,
  size = 'md',
  className,
  tooltip = true,
}: {
  value: unknown;
  size?: 'sm' | 'md';
  className?: string;
  /**
   * Set false where the badge sits INSIDE another control. The tooltip trigger
   * is a <button>, and a button inside a button is invalid HTML that browsers
   * silently un-nest — which breaks the outer control, not just the inner one.
   */
  tooltip?: boolean;
}) {
  const g = useGlossary();
  const spec = actionSpec(value);
  const code = typeof value === 'string' && value.trim() ? value.trim().toUpperCase() : null;
  const entry = code ? g.get(code) : null;

  const pad = size === 'sm' ? 'px-2 py-0.5 text-[10px] gap-1' : 'px-2.5 py-1 text-[11px] gap-1.5';
  const shell = cn(
    'inline-flex items-center rounded-full border font-semibold uppercase tracking-[0.07em] leading-4',
    pad,
    spec ? spec.badge : 'border-line bg-sunken text-muted',
    className,
  );

  const body = (
    <>
      <span aria-hidden="true" className="text-[0.85em] leading-none">
        {spec?.glyph ?? '?'}
      </span>
      <span>{code ?? 'no action'}</span>
    </>
  );

  if (!entry || !tooltip) return <span className={shell}>{body}</span>;

  return (
    <InfoPop
      className={cn(shell, 'cursor-help')}
      label={`${code} — what this means`}
      panel={
        <>
          <p className="text-[13px] font-semibold leading-snug text-ink">{text(entry.short, code ?? '')}</p>
          <p className="mt-1 text-[12px] leading-relaxed text-ink2">{text(entry.plain, '')}</p>
          {text(entry.why, '') && (
            <div className="mt-2 border-t border-line pt-2">
              <div className="label">Why it counts</div>
              <p className="mt-0.5 text-[12px] leading-relaxed text-ink2">{text(entry.why, '')}</p>
            </div>
          )}
          <p className="mt-2 font-mono text-[10px] uppercase tracking-wide text-muted">
            {code} · action · from /api/glossary
          </p>
        </>
      }
    >
      {body}
    </InfoPop>
  );
}
