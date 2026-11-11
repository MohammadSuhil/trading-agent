import { useMemo, useState } from 'react';
import { useApi } from '../api/client';
import type { Screener as ScreenerPayload, ScreenerRow } from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { DataTable, type Column } from '../components/DataTable';
import { ActionBadge, ACTION_ORDER, actionSpec } from '../components/action';
import { ReasonChip, TagChip } from '../components/StateChip';
import { Delta, Panel, cn } from '../components/primitives';
import { count, crore, num, price, signedPctFrac, stamp, text } from '../lib/format';

const LIMITS = [25, 50, 100, 200];

/**
 * Columns.
 *
 * Note what is NOT here and cannot be added: a probability column, and
 * therefore no sort-by-probability control. The screener ranks on `composite`,
 * which the backend computed; ranking on a probability point estimate would
 * hand the reader a league table built from numbers whose uncertainty the sort
 * throws away. `DataTable` enforces this at runtime too.
 *
 * `term` on a column is the glossary code for that figure; the ⓘ beside the
 * header is its own tab stop and carries the server's wording.
 */
const COLUMNS: Column<ScreenerRow>[] = [
  {
    key: 'symbol',
    header: 'Symbol',
    sortable: true,
    value: (r) => r.symbol,
    className: 'max-w-[13rem]',
    render: (r) => (
      <div className="min-w-0">
        <div className="font-semibold text-ink">{text(r.symbol)}</div>
        <div className="truncate text-[11px] text-muted">{text(r.name, '')}</div>
      </div>
    ),
  },
  {
    key: 'action',
    header: 'Action',
    sortable: true,
    // Sorted by the enum's own order, not alphabetically: BUY before WATCH
    // before AVOID is the order that matters, and A-Z would bury BUY under
    // AVOID.
    value: (r) => {
      const spec = actionSpec(r.action);
      return spec ? ACTION_ORDER.indexOf(spec.code) : 99;
    },
    render: (r) => <ActionBadge value={r.action} size="sm" />,
  },
  {
    key: 'composite',
    header: 'Composite',
    align: 'right',
    sortable: true,
    value: (r) => r.composite,
    render: (r) => <span className="font-semibold text-ink">{num(r.composite)}</span>,
    term: 'composite',
  },
  {
    key: 'industry',
    header: 'Industry',
    sortable: true,
    value: (r) => r.industry,
    className: 'max-w-[11rem]',
    render: (r) => <span className="block truncate text-ink2">{text(r.industry)}</span>,
  },
  { key: 'close', header: 'Close', align: 'right', sortable: true, value: (r) => r.close, render: (r) => price(r.close) },
  {
    key: 'ret_1d',
    header: 'Ret 1d',
    align: 'right',
    sortable: true,
    value: (r) => r.ret_1d,
    render: (r) => <Delta value={r.ret_1d} format={(v) => signedPctFrac(v)} />,
    term: 'ret_1d',
  },
  {
    key: 'ret_21d',
    header: 'Ret 21d',
    align: 'right',
    sortable: true,
    value: (r) => r.ret_21d,
    render: (r) => <Delta value={r.ret_21d} format={(v) => signedPctFrac(v)} />,
    term: 'ret_21d',
  },
  {
    key: 'rvol_20',
    header: 'RVol 20',
    align: 'right',
    sortable: true,
    value: (r) => r.rvol_20,
    render: (r) => num(r.rvol_20),
    term: 'RVOL_SPIKE',
    title: 'Relative volume over 20 sessions, as sent by the API',
  },
  {
    key: 'adtv_cr',
    header: 'ADTV',
    align: 'right',
    sortable: true,
    value: (r) => r.adtv_cr,
    render: (r) => crore(r.adtv_cr),
    term: 'TURNOVER_EXPANSION',
    title: 'Average daily traded value, ₹ crore',
  },
  {
    key: 'trend_state',
    header: 'Trend',
    sortable: true,
    value: (r) => r.trend_state,
    render: (r) => (
      <span className="whitespace-nowrap">
        <TagChip value={r.trend_state} plain />
      </span>
    ),
  },
  {
    key: 'adx14',
    header: 'ADX 14',
    align: 'right',
    sortable: true,
    value: (r) => r.adx14,
    render: (r) => num(r.adx14),
    term: 'ADX_TRENDING',
  },
  {
    key: 'rs_63',
    header: 'RS 63',
    align: 'right',
    sortable: true,
    value: (r) => r.rs_63,
    render: (r) => num(r.rs_63),
    term: 'RS_VS_NIFTY',
    title: 'Relative strength over 63 sessions, as sent by the API',
  },
  {
    key: 'rr',
    header: 'R:R',
    align: 'right',
    sortable: true,
    value: (r) => r.rr,
    render: (r) => num(r.rr),
    term: 'R:R',
  },
  {
    key: 'expectancy_atr',
    header: 'Expectancy',
    align: 'right',
    sortable: true,
    value: (r) => r.expectancy_atr,
    render: (r) => num(r.expectancy_atr),
    term: 'expectancy',
  },
  {
    key: 'sleeve',
    header: 'Sleeve',
    sortable: true,
    value: (r) => r.sleeve,
    render: (r) => (
      <span className="whitespace-nowrap">
        <TagChip value={r.sleeve} plain />
      </span>
    ),
  },
  {
    key: 'reasons',
    header: 'Why',
    className: 'w-[18rem]',
    render: (r) => {
      // Two chips and a count, never the full stack: a cell that grows to five
      // chips sets the height of every other cell in its row, and 50 rows of
      // that is the wall of text this redesign exists to remove. The rest are
      // one hover away, and all of them are on the card in Today.
      const codes = (r.reasons ?? []).filter((c): c is string => typeof c === 'string' && c.length > 0);
      const head = codes.slice(0, 2);
      const rest = codes.length - head.length;
      return (
        <div className="flex max-w-[18rem] items-center gap-1 overflow-hidden">
          {head.map((code, i) => (
            <ReasonChip key={`${code}-${i}`} code={code} compact />
          ))}
          {rest > 0 && <span className="shrink-0 text-[10.5px] text-muted">+{rest}</span>}
          {codes.length === 0 && <span className="text-muted">—</span>}
        </div>
      );
    },
  },
];

export function Screener({ version }: { version: number }) {
  const [limit, setLimit] = useState(50);
  const [query, setQuery] = useState('');
  const [action, setAction] = useState<string | null>(null);

  const screener = useApi<ScreenerPayload>(`/api/screener?limit=${limit}`, version);
  const rows = useMemo(() => screener.data?.rows ?? [], [screener.data]);

  const counts = useMemo(() => {
    const m = new Map<string, number>();
    rows.forEach((r) => {
      const c = actionSpec(r.action)?.code;
      if (c) m.set(c, (m.get(c) ?? 0) + 1);
    });
    return m;
  }, [rows]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((r) => {
      if (action && actionSpec(r.action)?.code !== action) return false;
      if (!q) return true;
      return [r.symbol, r.name, r.industry, r.sleeve, r.trend_state]
        .filter((v): v is string => typeof v === 'string')
        .some((v) => v.toLowerCase().includes(q));
    });
  }, [rows, query, action]);

  return (
    <ApiGate states={[screener]}>
      <Panel
        title="Screener"
        subtitle={`${count(filtered.length)} of ${count(rows.length)} rows · as of ${stamp(screener.data?.as_of)}`}
        right={
          // One filter row, above the content it scopes.
          <div className="flex flex-wrap items-center justify-end gap-2">
            {ACTION_ORDER.filter((c) => (counts.get(c) ?? 0) > 0).map((c) => (
              <button
                key={c}
                type="button"
                aria-pressed={action === c}
                onClick={() => setAction((a) => (a === c ? null : c))}
                className={cn('rounded-md', action === c && 'ring-2 ring-accent ring-offset-1 ring-offset-surface')}
              >
                <span className="flex items-center gap-1">
                  {/* tooltip={false}: this badge is inside a filter button, and
                      a nested <button> would break both controls. The ⓘ for
                      these codes lives in the table's own Action column. */}
                  <ActionBadge value={c} size="sm" tooltip={false} />
                  <span className="tnum text-[10.5px] text-muted">{counts.get(c)}</span>
                </span>
              </button>
            ))}
            <label className="flex items-center gap-1.5 text-xs text-muted">
              <span>Search</span>
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="symbol, name, industry"
                className="w-48 rounded-lg border border-line bg-surface px-2.5 py-1.5 text-xs text-ink placeholder:text-muted"
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted">
              <span>Limit</span>
              <select
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
                className="rounded-lg border border-line bg-surface px-2 py-1.5 text-xs text-ink"
              >
                {LIMITS.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
          </div>
        }
        bodyClassName="p-0"
      >
        <DataTable
          rows={filtered}
          columns={COLUMNS}
          rowKey={(r, i) => `${text(r.symbol, 'row')}-${i}`}
          initialSort={{ key: 'composite', dir: 'desc' }}
          dense
          caption="Screener candidates ranked by composite score"
          empty={query || action ? 'No row matches that filter.' : 'The screener returned no rows.'}
        />
      </Panel>
    </ApiGate>
  );
}
