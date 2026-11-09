import { Term } from './Tooltip';
import { cn } from '../lib/cn';
import { EM_DASH, isStr } from '../lib/format';

/**
 * Trend state, and the pairing that is the point of it.
 *
 * The backend classifies the stock AND its industry with the same classifier at
 * the same three lookbacks, so the two are directly comparable — and the
 * comparison is the information. A name going sideways inside a strongly
 * trending industry is a different trade from the same name going sideways
 * inside a decaying one, and two unrelated chips would throw that away. So they
 * are always rendered as a labelled pair, stock above industry, never alone.
 *
 * Colour says only what it can defend: clearly up, clearly down, or neither.
 * The three "weak" states (UP_DECAYING, RANGE, DOWN_DECAYING) share the neutral
 * ink on purpose — tinting a decaying uptrend green would be a claim the state
 * name explicitly withdraws. Direction still arrives via the glyph and the word.
 *
 * UNDEFINED is NOT sideways. It is "not enough history to say", which the
 * glossary states outright, so it gets the muted ink and a question mark rather
 * than the ◇ that means a real, measured range.
 */

type Spec = { glyph: string; ink: string };

const TREND: Record<string, Spec> = {
  UP_STRONG: { glyph: '▲', ink: 'text-pos-ink' },
  UP: { glyph: '▲', ink: 'text-pos-ink' },
  UP_DECAYING: { glyph: '▴', ink: 'text-ink2' },
  RANGE: { glyph: '◇', ink: 'text-ink2' },
  DOWN_DECAYING: { glyph: '▾', ink: 'text-ink2' },
  DOWN: { glyph: '▼', ink: 'text-neg-ink' },
  DOWN_STRONG: { glyph: '▼', ink: 'text-neg-ink' },
  UNDEFINED: { glyph: '?', ink: 'text-muted' },
};

function specOf(code: unknown): Spec | null {
  if (!isStr(code)) return null;
  return TREND[code.trim().toUpperCase()] ?? null;
}

/**
 * One trend state: glyph + the server's own code, with the glossary entry on
 * hover and on focus. The code is shown rather than a paraphrase because the
 * paraphrase would be ours; the plain-English reading is one hover away.
 */
export function TrendValue({ code, className }: { code: unknown; className?: string }) {
  const spec = specOf(code);
  if (!isStr(code) || !spec) {
    return (
      <span className={cn('text-muted', className)} aria-label="trend state not sent">
        {EM_DASH}
      </span>
    );
  }
  return (
    <span className={cn('inline-flex items-baseline gap-1 whitespace-nowrap', spec.ink, className)}>
      <span aria-hidden="true" className="text-[0.8em] leading-none">
        {spec.glyph}
      </span>
      <Term code={code} className="font-mono text-[10.5px] font-medium uppercase tracking-[0.03em]">
        {code}
      </Term>
    </span>
  );
}

/**
 * The 63-session pair — the primary horizon — as a two-row block.
 *
 * Stock above industry, both labelled, so the relationship reads top to bottom
 * without the reader having to remember which chip was which.
 */
export function TrendPair({
  stock,
  industry,
  className,
}: {
  stock: unknown;
  industry: unknown;
  className?: string;
}) {
  return (
    <dl className={cn('grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-2 gap-y-1', className)}>
      <dt className="label leading-4">Stock</dt>
      <dd className="min-w-0 leading-4">
        <TrendValue code={stock} />
      </dd>
      <dt className="label leading-4">Industry</dt>
      <dd className="min-w-0 leading-4">
        <TrendValue code={industry} />
      </dd>
    </dl>
  );
}

/** The same pair on one line, for a dense list row. */
export function TrendPairInline({
  stock,
  industry,
  className,
}: {
  stock: unknown;
  industry: unknown;
  className?: string;
}) {
  return (
    <span className={cn('inline-flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5', className)}>
      <span className="label leading-4">stk</span>
      <TrendValue code={stock} />
      <span aria-hidden="true" className="text-muted">
        ·
      </span>
      <span className="label leading-4">ind</span>
      <TrendValue code={industry} />
    </span>
  );
}

/** All three lookbacks, stock against industry. For the expansion. */
export function TrendMatrix({
  stock,
  industry,
}: {
  stock: [unknown, unknown, unknown];
  industry: [unknown, unknown, unknown];
}) {
  const cols = ['20', '63', '252'];
  return (
    <table className="w-full border-collapse">
      <caption className="sr-only">Trend state of the stock and of its industry at three lookbacks</caption>
      <thead>
        <tr>
          <th scope="col" className="label w-20 pb-1 text-left font-semibold">
            Lookback
          </th>
          {cols.map((c) => (
            <th key={c} scope="col" className="label pb-1 text-left font-semibold">
              {c}d
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        <tr className="border-t border-line">
          <th scope="row" className="py-1.5 text-left text-[11.5px] font-medium text-ink2">
            Stock
          </th>
          {stock.map((v, i) => (
            <td key={cols[i]} className="py-1.5">
              <TrendValue code={v} />
            </td>
          ))}
        </tr>
        <tr className="border-t border-line">
          <th scope="row" className="py-1.5 text-left text-[11.5px] font-medium text-ink2">
            Industry
          </th>
          {industry.map((v, i) => (
            <td key={cols[i]} className="py-1.5">
              <TrendValue code={v} />
            </td>
          ))}
        </tr>
      </tbody>
    </table>
  );
}
