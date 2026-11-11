import type { RecommendationRow } from '../api/types';
import { BarrierBlock } from './BarrierBlock';
import { ActionBadge, actionRail, actionSpec } from './action';
import { TagChip } from './StateChip';
import { Pill } from './ui';
import { TrendMatrix, TrendPair } from './trend';
import { Field, Disclosure, Chip, cn } from './primitives';
import { Term, TermInfo, TermLabel, TermText } from './Tooltip';
import { EM_DASH, EN_DASH, bps, count, inr, isNum, num, price, pctFrac, text } from '../lib/format';

export type Density = 'comfortable' | 'compact';

/**
 * The plan rail: stop — entry band — target, laid out to scale.
 *
 * Geometry only. Every printed number still comes from the API; this just puts
 * the three of them on one axis so the asymmetry between risk and reward is
 * visible without arithmetic. It renders only when the four levels are present
 * and actually ordered (stop < entry_lo <= entry_hi < target) — a long plan
 * with an inverted stop is a backend bug, and drawing it anyway would turn that
 * bug into a picture that looks fine.
 */
export function PlanRail({ r }: { r: RecommendationRow }) {
  const { stop, entry_lo: lo, entry_hi: hi, target } = r;
  if (!isNum(stop) || !isNum(lo) || !isNum(hi) || !isNum(target)) return null;
  if (!(stop < lo && lo <= hi && hi < target)) return null;

  const span = target - stop;
  if (span <= 0) return null;
  const pos = (v: number) => ((v - stop) / span) * 100;

  // Centre of the entry band, clamped so the label cannot hang off either end.
  const entryMid = Math.max(12, Math.min(88, (pos(lo) + pos(hi)) / 2));

  return (
    <div aria-hidden="true">
      <div className="relative h-1.5 w-full rounded-full bg-neg/25">
        {/* From the entry band up to target: the reward leg. */}
        <div className="absolute inset-y-0 rounded-full bg-pos/25" style={{ left: `${pos(hi)}%`, right: '0%' }} />
        {/* The entry band itself, in slot-1 ink, with a surface ring either side. */}
        <div
          className="absolute -inset-y-0.5 rounded-full bg-s1 ring-2 ring-surface"
          style={{ left: `${pos(lo)}%`, width: `${Math.max(1.5, pos(hi) - pos(lo))}%` }}
        />
      </div>
      {/* "entry" is positioned over the band it names. Spacing the three labels
          evenly would park "entry" at the midpoint of the stop-to-target span,
          which is a different place and reads as a claim about where the entry
          sits. */}
      <div className="relative mt-1 h-3.5 text-[9.5px] uppercase tracking-wide text-muted">
        <span className="absolute left-0">▼ stop</span>
        <span className="absolute -translate-x-1/2" style={{ left: `${entryMid}%` }}>
          entry
        </span>
        <span className="absolute right-0">target ▲</span>
      </div>
    </div>
  );
}

export function entryBand(r: RecommendationRow): string {
  if (isNum(r.entry_lo) && isNum(r.entry_hi)) return `${price(r.entry_lo)} ${EN_DASH} ${price(r.entry_hi)}`;
  if (isNum(r.entry_lo)) return price(r.entry_lo);
  if (isNum(r.entry_hi)) return price(r.entry_hi);
  return EM_DASH;
}

/**
 * The 90% interval as a one-line pill, or an honest em-dash.
 *
 * Interval only — never the midpoint, never a bare point estimate. `compact`
 * swaps the spelled-out label for the ⓘ that carries the same glossary entry;
 * it changes how much room the label takes, not what is claimed.
 */
export function ConfidencePill({ r, compact = false }: { r: RecommendationRow; compact?: boolean }) {
  const lo = r.barrier?.ci_lo;
  const hi = r.barrier?.ci_hi;
  const label = compact ? (
    <TermInfo code="P(up barrier first)" className="ml-0.5" />
  ) : (
    <TermLabel code="P(up barrier first)" className="ml-1 font-medium normal-case tracking-normal opacity-80" />
  );

  if (r.barrier?.state !== 'OK' || !isNum(lo) || !isNum(hi) || hi < lo) {
    return (
      <Pill tone="quiet" glyph="—">
        <span className="sr-only">No probability interval. </span>
        {label}
      </Pill>
    );
  }
  return (
    <Pill tone="accent" glyph="◔">
      <span className="tnum">
        {Math.round(lo * 100)}
        {EN_DASH}
        {Math.round(hi * 100)}%
      </span>
      {label}
    </Pill>
  );
}

/**
 * Size — the single most important rendering rule on this card.
 *
 * Two different questions, never conflated:
 *
 *   • `qty` / `notional` / `risk_rupees` — what you are ADVISED to buy now.
 *     On a BUY this leads, at the largest size on the card after the symbol.
 *   • `would_qty` / `would_notional` / `would_risk_rupees` — AFFORDABILITY:
 *     the most this name could take today given risk appetite, cash, and the
 *     headroom left in its industry. That is a capacity, not an instruction,
 *     so it is set in subordinate type under a label that says so, with the
 *     server's own `afford_note` beside it.
 *
 * A WATCH therefore never shows a promoted zero, and never shows a number a
 * reader could mistake for "buy this many".
 */
export function SizeBlock({ r, dense = false }: { r: RecommendationRow; dense?: boolean }) {
  const spec = actionSpec(r.action);
  const advising = spec?.code === 'BUY' || spec?.code === 'TRIM' || spec?.code === 'SELL' || (isNum(r.qty) && r.qty > 0);

  if (advising) {
    return (
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="tnum text-[18px] font-semibold leading-none text-ink">
          {count(r.qty)} <span className="text-[12px] font-medium text-ink2">sh</span>
        </span>
        <span className="tnum text-[14px] font-semibold leading-none text-ink2">{inr(r.notional)}</span>
        <span className="tnum text-[11.5px] text-muted">
          <TermLabel code="risk" className="mr-1 normal-case tracking-normal" />
          {inr(r.risk_rupees)}
          {isNum(r.risk_pct) && <span className="ml-1">({pctFrac(r.risk_pct)})</span>}
        </span>
      </div>
    );
  }

  const hasWould = isNum(r.would_qty) || isNum(r.would_notional);
  const note = text(r.afford_note, '');

  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11.5px] leading-tight">
        {/* "Could", never "should". The action's own glossary entry is one
            hover away so the distinction is backed by the server's wording. */}
        <Term code={typeof r.action === 'string' ? r.action : 'WATCH'} className="text-muted">
          You could buy
        </Term>
        {hasWould ? (
          <span className="tnum text-ink2">
            <span className="font-semibold text-ink">{count(r.would_qty)}</span>
            {' sh · '}
            {inr(r.would_notional)}
            {isNum(r.would_risk_rupees) && (
              <>
                {' · '}
                <span className="text-muted">{inr(r.would_risk_rupees)} at risk</span>
              </>
            )}
          </span>
        ) : (
          <span className="tnum text-muted" aria-label="no affordable size sent">
            {EM_DASH}
          </span>
        )}
      </div>
      {note && !dense && <p className="mt-0.5 text-[10.5px] leading-tight text-muted">{note}</p>}
    </div>
  );
}

/** Reason codes: the server's plain wording first, the code beside it. */
export function ReasonList({ codes }: { codes: string[] }) {
  if (codes.length === 0) return <p className="text-[12px] text-muted">No reason codes were sent for this row.</p>;
  return (
    <ul className="space-y-1.5">
      {codes.map((code, i) => (
        <li key={`${code}-${i}`} className="flex items-baseline gap-2">
          <span aria-hidden="true" className="text-[8px] leading-4 text-accent-ink">
            ◆
          </span>
          <TermText code={code} className="text-[12.5px] leading-snug text-ink" />
          <span className="ml-auto shrink-0 font-mono text-[9.5px] uppercase leading-4 text-muted">{code}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * The secondary figures. Every one of them is a glossary term.
 *
 * "Advised risk" is omitted where the row is not advising anything: on a WATCH
 * it is structurally ₹0 and 0.00%, which reads as a measured quantity rather
 * than as an absent instruction. The meaningful number — what you would have at
 * risk if you took the trade — is already on the card, in `SizeBlock`.
 */
export function SecondaryGrid({ r }: { r: RecommendationRow }) {
  const spec = actionSpec(r.action);
  const advising = spec?.code === 'BUY' || spec?.code === 'TRIM' || spec?.code === 'SELL' || (isNum(r.qty) && r.qty > 0);
  return (
    <div className="grid grid-cols-[repeat(2,minmax(0,1fr))] gap-x-4 gap-y-3 sm:grid-cols-[repeat(3,minmax(0,1fr))]">
      <Field label="R:R" term="R:R">
        {num(r.rr)}
      </Field>
      <Field label="Cost" term="cost">
        {bps(r.cost_bps)}
      </Field>
      <Field label="Expectancy" term="expectancy">
        {num(r.expectancy_atr)}
        {/* The unit rides the number, not the em-dash: "— ATR" reads as a
            measurement in ATR that happens to be blank, rather than as an
            absent measurement. */}
        {isNum(r.expectancy_atr) && <span className="ml-1 text-xs text-muted">ATR</span>}
      </Field>
      {advising && (
        <Field label="Advised risk" term="risk">
          {inr(r.risk_rupees)}
          {isNum(r.risk_pct) && <span className="ml-1 text-xs text-muted">({pctFrac(r.risk_pct)})</span>}
        </Field>
      )}
    </div>
  );
}

/** The collapsed half of a card: why it fired, and everything secondary. */
export function CardDetails({ r }: { r: RecommendationRow }) {
  const reasons = (r.reasons ?? []).filter((c): c is string => typeof c === 'string' && c.length > 0);
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div className="space-y-4">
        <div>
          <div className="label mb-2">Why this name</div>
          <ReasonList codes={reasons} />
        </div>
        <SecondaryGrid r={r} />
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="label">Sleeve</span>
          <TagChip value={r.sleeve} plain />
          <span className="label ml-2">Horizon</span>
          <TagChip value={r.horizon} />
          {isNum(r.composite) && <span className="tnum ml-2 text-[11px] text-muted">composite {num(r.composite)}</span>}
        </div>
      </div>

      <div className="space-y-4">
        <div>
          <div className="label mb-2">Trend, stock against its industry</div>
          <TrendMatrix
            stock={[r.trend_20, r.trend_63, r.trend_252]}
            industry={[r.ind_trend_20, r.ind_trend_63, r.ind_trend_252]}
          />
        </div>
        <BarrierBlock barrier={r.barrier} />
        {r.id && <p className="font-mono text-[10px] text-muted">id {r.id}</p>}
      </div>
    </div>
  );
}

export function RecommendationCard({
  r,
  density = 'comfortable',
  open,
  onToggle,
  isOwned = false,
  onPaperBuy,
  buying = false,
}: {
  r: RecommendationRow;
  density?: Density;
  open: boolean;
  onToggle: (next: boolean) => void;
  isOwned?: boolean;
  onPaperBuy?: (r: RecommendationRow) => void;
  buying?: boolean;
}) {
  const reasons = (r.reasons ?? []).filter((c): c is string => typeof c === 'string' && c.length > 0);
  const dense = density === 'compact';
  const canBuy = ((isNum(r.qty) && r.qty > 0) || (isNum(r.would_qty) && r.would_qty > 0)) && !isOwned;

  return (
    <article className="panel relative overflow-hidden transition-shadow hover:shadow-raise">
      {/* Position AND colour: the action is readable from the rail alone while
          scrolling, before any text has been read. Auto-bought / owned BUY cards
          use a green rail so they visually distinguish from unbought BUY cards. */}
      <div
        aria-hidden="true"
        className={cn(
          'absolute inset-y-0 left-0 w-[3px]',
          isOwned && actionSpec(r.action)?.code === 'BUY' ? 'bg-good' : actionRail(r.action),
        )}
      />

      <div className={cn('pl-5', dense ? 'p-3.5 pl-5' : 'p-4 pl-5')}>
        {/* 1 — what is this. 2 — what am I being told to do. Full weight, and
            nothing else on the card competes with these two. */}
        <header className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="truncate text-[17px] font-semibold leading-tight tracking-[-0.015em] text-ink">
              {text(r.symbol)}
            </h3>
            <p className="truncate text-[11.5px] leading-tight text-muted">
              {text(r.name, '')}
              {r.name && r.industry ? ' · ' : ''}
              {text(r.industry, r.name ? '' : 'industry not set')}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {isOwned ? (
              <Chip tone="good" glyph="✓">In portfolio</Chip>
            ) : canBuy ? (
              <button
                type="button"
                disabled={buying}
                onClick={(e) => {
                  e.stopPropagation();
                  onPaperBuy?.(r);
                }}
                className="rounded-lg border border-accent bg-accent/10 px-2.5 py-1 text-[11.5px] font-semibold text-accent-ink hover:bg-accent/20 disabled:opacity-50"
              >
                {buying ? 'Buying…' : 'Paper buy'}
              </button>
            ) : null}
            <ActionBadge value={r.action} />
          </div>
        </header>

        {/* 3 — what is the plan. */}
        {!dense && (
          <div className="mt-3">
            <PlanRail r={r} />
          </div>
        )}

        {/* The band gets its own full-width row, and every track is
            `minmax(0,1fr)` with `min-w-0` items. Sharing a three-column grid
            with the two levels made a five-figure entry range — two ₹ amounts
            and an en-dash, roughly twice the width of its siblings — overrun
            the stop beside it and print two prices as one unreadable number.

            The numbers are tinted to agree with the rail above them: stop in
            the down pole, target in the up pole, the entry band neutral because
            it is neither. Each carries its ▼/▲ so the polarity is not hue-only. */}
        <div
          className={cn(
            'grid grid-cols-[repeat(2,minmax(0,1fr))] gap-x-4 gap-y-2.5',
            dense ? 'mt-2.5' : 'mt-3',
          )}
        >
          <Field label="Buy between" term="entry" size={dense ? 'sm' : 'lg'} className="col-span-2">
            {entryBand(r)}
          </Field>
          <Field label="Stop" term="stop" size={dense ? 'sm' : 'lg'} tone="neg" glyph="▼">
            {price(r.stop)}
          </Field>
          <Field label="Target" term="target" size={dense ? 'sm' : 'lg'} tone="pos" glyph="▲">
            {price(r.target)}
          </Field>
        </div>

        <div className={cn('rounded-lg bg-sunken px-3 py-2', dense ? 'mt-2.5' : 'mt-3')}>
          <SizeBlock r={r} dense={dense} />
        </div>

        {/* 4 — how confident, and what the stock is doing inside its industry.
            The 63-session pair only; all three lookbacks are in the details. */}
        <div className={cn('flex flex-wrap items-start justify-between gap-x-4 gap-y-2', dense ? 'mt-2.5' : 'mt-3')}>
          <div className="flex flex-wrap items-center gap-1.5">
            <ConfidencePill r={r} />
            <TagChip value={r.sleeve} plain />
          </div>
          <TrendPair stock={r.trend_63} industry={r.ind_trend_63} className="shrink-0" />
        </div>

        {/* 5 — why. Collapsed by default: this is the paragraph and the twelve
            secondary figures that made the card unreadable. */}
        <div className="mt-2">
          <Disclosure
            open={open}
            onToggle={onToggle}
            summary={
              <span>
                Why &amp; detail
                <span className="ml-1.5 text-muted">
                  {reasons.length} reason{reasons.length === 1 ? '' : 's'}
                </span>
              </span>
            }
          >
            <CardDetails r={r} />
          </Disclosure>
        </div>
      </div>
    </article>
  );
}
