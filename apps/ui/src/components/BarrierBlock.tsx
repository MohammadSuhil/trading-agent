import type { Barrier } from '../api/types';
import { TermLabel } from './Tooltip';
import { cn } from '../lib/cn';
import { EM_DASH, EN_DASH, count, isNum, text } from '../lib/format';

/**
 * The barrier probability block — the honesty rules live here, and they are the
 * product, not the chrome.
 *
 * 1. A bare point estimate is never rendered. A single number like "54%" reads
 *    as knowledge; the model does not have that much knowledge, and the reader
 *    has no way to tell from the glyph how much it has.
 * 2. `state: "UNAVAILABLE"` renders an em-dash and the server's own reason. Not
 *    a zero, not a blank, not a hidden row — a visible refusal with a cause.
 * 3. `state: "OK"` renders the INTERVAL, with `n_eff` beside it, because the
 *    interval plus the effective sample size is the whole claim.
 * 4. When the interval is too wide it is *all* that renders. At that width a
 *    midpoint is noise, and showing it would let a reader quote a number the
 *    data does not support. The verdict is the SERVER'S — `barrier.wide` — so
 *    the threshold lives in exactly one place and the two cannot drift. The
 *    documented 15pp test is kept only as the fallback for a response that
 *    predates the flag; a server that says "wide" always wins.
 * 5. Where a point estimate is shown at all (tight interval only) it is in
 *    smaller type than the interval. The interval is the finding; the point is
 *    a footnote to it.
 *
 * `compact` is a density choice, not an honesty one. It drops the 0–100 track
 * and the model's prose note — both of which are still one click away in the
 * card's details — and keeps the interval, n_eff and the midpoint rule exactly
 * as above. Nothing that constrains what may be claimed changes with density.
 */

/** Above this width (in probability units) the midpoint is not shown at all. */
const WIDE_INTERVAL = 0.15;

/** The code the glossary files this figure under. */
const P_UP = 'P(up barrier first)';

/** Probabilities print as whole percents, matching the spec's "41–57%". */
function pp(v: number): string {
  return String(Math.round(v * 100));
}

function Unavailable({ reason, compact }: { reason: string; compact: boolean }) {
  return (
    <div className={cn('rounded-lg border border-line bg-sunken', compact ? 'px-2.5 py-1.5' : 'px-3 py-2')}>
      <TermLabel code={P_UP} />
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-lg font-semibold leading-none text-muted" aria-hidden="true">
          {EM_DASH}
        </span>
        <span className="sr-only">Not available.</span>
        <span className={cn('text-ink2', compact ? 'line-clamp-2 text-[11px]' : 'text-xs')}>{reason}</span>
      </div>
    </div>
  );
}

export function BarrierBlock({ barrier, compact = false }: { barrier?: Barrier | null; compact?: boolean }) {
  const b = barrier ?? null;
  const lo = b?.ci_lo;
  const hi = b?.ci_hi;
  const hasInterval = isNum(lo) && isNum(hi) && hi >= lo;
  const ok = b?.state === 'OK' && hasInterval;

  if (!ok) {
    // Covers three distinct cases with one treatment, on purpose: no barrier
    // object at all, state UNAVAILABLE, and state OK with a null interval. All
    // three mean the same thing to a reader — there is no usable probability —
    // and dressing them differently would imply a distinction that does not
    // affect any decision.
    const reason =
      text(b?.reason, '') ||
      (b?.state === 'OK'
        ? 'state OK but no interval was returned'
        : b?.state
          ? 'no reason given'
          : 'no barrier estimate for this candidate');
    return <Unavailable reason={reason} compact={compact} />;
  }

  const width = hi - lo;
  // The server's flag is authoritative when present; the local test is the
  // fallback, not a second opinion. `wide === true` and `wide === false` are
  // both answers — only `null`/absent falls through to the 15pp rule.
  const wide = typeof b?.wide === 'boolean' ? b.wide : width > WIDE_INTERVAL;
  const mid = b?.p_up;
  const pDn = b?.p_dn;
  const pVert = b?.p_vert;
  const nEff = b?.n_eff;
  const note = text(b?.reason, '');

  if (compact) {
    // One line on a card: the interval at full weight, the label and the
    // evidence count beside it, both carrying their glossary entry.
    return (
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1 rounded-lg border border-line bg-sunken px-2.5 py-1.5">
        <span className="tnum text-[15px] font-semibold leading-tight text-ink">
          {pp(lo)}
          {EN_DASH}
          {pp(hi)}%
        </span>
        <TermLabel code={P_UP} className="normal-case tracking-normal" />
        <span className="ml-auto text-[10.5px] text-muted">
          <TermLabel code="n_eff" className="mr-1 normal-case tracking-normal" />
          <span className="tnum">{isNum(nEff) ? count(nEff) : EM_DASH}</span>
        </span>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-line bg-sunken px-3 py-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <TermLabel code={P_UP} />
        <span className="flex shrink-0 items-baseline gap-1 whitespace-nowrap text-[11px] text-muted">
          <TermLabel code="n_eff" className="normal-case tracking-normal" />
          <span className="tnum">{isNum(nEff) ? count(nEff) : EM_DASH}</span>
        </span>
      </div>

      {/* The interval is the headline. Nothing else in this block is larger. */}
      <div className="tnum mt-1 text-base font-semibold leading-tight text-ink">
        {pp(lo)}
        {EN_DASH}
        {pp(hi)}%
      </div>

      {/* The same claim, drawn: a band on a 0–100 track. It gives the width a
          shape, which is the part a reader skips when it is only digits. */}
      <div
        className="mt-2"
        role="img"
        aria-label={`Interval ${pp(lo)} to ${pp(hi)} percent on a 0 to 100 percent scale`}
      >
        <div className="relative h-2 w-full rounded-full bg-s1/20">
          <div
            className="absolute inset-y-0 rounded-full bg-s1"
            style={{ left: `${Math.max(0, lo * 100)}%`, width: `${Math.max(1, Math.min(100, width * 100))}%` }}
          />
          {/* Midpoint tick, and only inside a tight band. A 2px surface-coloured
              notch, never a label — rule 4 keeps the number itself off screen. */}
          {!wide && isNum(mid) && (
            <div
              className="absolute inset-y-0 w-[2px] bg-sunken"
              style={{ left: `calc(${Math.max(0, Math.min(100, mid * 100))}% - 1px)` }}
            />
          )}
        </div>
        <div className="mt-1 flex justify-between text-[10px] text-muted">
          <span>0%</span>
          <span>100%</span>
        </div>
      </div>

      {wide ? (
        <p className="mt-2 text-[11px] text-muted">Interval too wide to quote a midpoint — midpoint withheld.</p>
      ) : (
        // Strictly smaller type than the interval above (text-[11px] vs
        // text-base), and always carrying the word "midpoint" so it cannot be
        // mistaken for a settled figure.
        <p className="tnum mt-2 text-[11px] text-ink2">
          <span className="text-muted">midpoint</span> {isNum(mid) ? `${pp(mid)}%` : EM_DASH}
          {isNum(pDn) && (
            <>
              {' · '}
              <span className="text-muted">down</span> {pp(pDn)}%
            </>
          )}
          {isNum(pVert) && (
            <>
              {' · '}
              <span className="text-muted">vertical</span> {pp(pVert)}%
            </>
          )}
        </p>
      )}

      {note && <p className="mt-1 text-[11px] leading-relaxed text-muted">{note}</p>}
    </div>
  );
}
