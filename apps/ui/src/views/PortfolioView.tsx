import { useCallback, useMemo, useState } from 'react';
import { postJson, useApi } from '../api/client';
import type { Portfolio, PositionRow, TradeEvent } from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { DataTable, type Column } from '../components/DataTable';
import { TagChip } from '../components/StateChip';
import { Chip, Delta, Hero, Meter, Panel } from '../components/primitives';
import { Term, TermInfo, TermLabel } from '../components/Tooltip';
import { EquityCurve } from '../charts/EquityCurve';
import { Kpi } from '../components/ui';
import type { Mode } from '../lib/theme';
import { EM_DASH, count, inr, isNum, pctFrac, price, signedInr, signedPctFrac, stamp, text } from '../lib/format';

const BASE_COLUMNS: Column<PositionRow>[] = [
  {
    key: 'symbol',
    header: 'Symbol',
    sortable: true,
    value: (r) => r.symbol,
    render: (r) => <span className="font-medium text-ink">{text(r.symbol)}</span>,
  },
  {
    key: 'industry',
    header: 'Industry',
    sortable: true,
    value: (r) => r.industry,
    render: (r) => <span className="text-ink2">{text(r.industry)}</span>,
  },
  { key: 'qty', header: 'Qty', align: 'right', sortable: true, value: (r) => r.qty, render: (r) => count(r.qty) },
  {
    key: 'entry',
    header: 'Entry',
    align: 'right',
    sortable: true,
    value: (r) => r.entry,
    render: (r) => price(r.entry),
    term: 'entry',
  },
  { key: 'last', header: 'Last', align: 'right', sortable: true, value: (r) => r.last, render: (r) => price(r.last) },
  {
    key: 'stop',
    header: 'Stop',
    align: 'right',
    sortable: true,
    value: (r) => r.stop,
    render: (r) => price(r.stop),
    term: 'stop',
  },
  {
    key: 'target',
    header: 'Target',
    align: 'right',
    sortable: true,
    value: (r) => r.target,
    render: (r) => price(r.target),
    term: 'target',
  },
  {
    key: 'pnl',
    header: 'P&L',
    align: 'right',
    sortable: true,
    value: (r) => r.pnl,
    render: (r) => <Delta value={r.pnl} format={(v) => signedInr(v)} />,
    term: 'pnl',
  },
  {
    key: 'pnl_pct',
    header: 'P&L %',
    align: 'right',
    sortable: true,
    value: (r) => r.pnl_pct,
    render: (r) => <Delta value={r.pnl_pct} format={(v) => signedPctFrac(v)} />,
    term: 'pnl_pct',
  },
  {
    key: 'risk_rupees',
    header: 'Risk',
    align: 'right',
    sortable: true,
    value: (r) => r.risk_rupees,
    render: (r) => inr(r.risk_rupees),
    term: 'risk',
  },
  {
    key: 'opened',
    header: 'Opened',
    sortable: true,
    value: (r) => r.opened,
    render: (r) => <span className="whitespace-nowrap text-ink2">{stamp(r.opened)}</span>,
  },
  {
    key: 'horizon',
    header: 'Horizon',
    sortable: true,
    value: (r) => r.horizon,
    render: (r) => <TagChip value={r.horizon} />,
    term: 'SWING',
  },
];

const TRADE_COLUMNS: Column<TradeEvent>[] = [
  {
    key: 'd',
    header: 'Date',
    sortable: true,
    value: (r) => r.ts ?? r.d,
    render: (r) => <span className="whitespace-nowrap text-ink2">{stamp(r.ts ?? r.d)}</span>,
  },
  {
    key: 'symbol',
    header: 'Symbol',
    sortable: true,
    value: (r) => r.symbol,
    render: (r) => <span className="font-semibold text-ink">{text(r.symbol)}</span>,
  },
  {
    key: 'side',
    header: 'Side',
    sortable: true,
    value: (r) => r.side,
    render: (r) => {
      const isBuy = r.side?.toUpperCase() === 'BUY';
      return <Chip tone={isBuy ? 'pos' : 'neg'}>{text(r.side)}</Chip>;
    },
  },
  {
    key: 'qty',
    header: 'Shares',
    align: 'right',
    sortable: true,
    value: (r) => r.qty,
    render: (r) => count(r.qty),
  },
  {
    key: 'price',
    header: 'Fill price',
    align: 'right',
    sortable: true,
    value: (r) => r.price,
    render: (r) => price(r.price),
  },
  {
    key: 'notional',
    header: 'Notional',
    align: 'right',
    sortable: true,
    value: (r) => r.notional,
    render: (r) => inr(r.notional),
  },
  {
    key: 'cost_rupees',
    header: 'Costs',
    align: 'right',
    sortable: true,
    value: (r) => r.cost_rupees,
    term: 'cost',
    render: (r) => (isNum(r.cost_rupees) ? inr(r.cost_rupees) : EM_DASH),
  },
  {
    key: 'pnl_rupees',
    header: 'Realised P&L',
    align: 'right',
    sortable: true,
    value: (r) => r.pnl_rupees,
    term: 'pnl',
    render: (r) => (isNum(r.pnl_rupees) ? <Delta value={r.pnl_rupees} format={(v) => signedInr(v)} /> : EM_DASH),
  },
  {
    key: 'note',
    header: 'Note',
    render: (r) => <span className="text-ink2">{text(r.note, '')}</span>,
  },
];

/**
 * Drawdown tone. The thresholds here decide a COLOUR, never a number — the
 * figure printed is always the server's `drawdown_pct` verbatim.
 */
function drawdownTone(v: unknown): 'good' | 'warning' | 'critical' | undefined {
  if (!isNum(v)) return undefined;
  const d = Math.abs(v);
  if (d >= 0.1) return 'critical';
  if (d >= 0.05) return 'warning';
  return 'good';
}

function toneClass(t: 'good' | 'warning' | 'critical' | undefined): string {
  if (t === 'critical') return 'text-critical-ink';
  if (t === 'warning') return 'text-warning-ink';
  return 'text-ink';
}

export function PortfolioView({
  version,
  mode,
  onRefresh,
}: {
  version: number;
  mode?: Mode;
  onRefresh?: () => void;
}) {
  const pf = useApi<Portfolio>('/api/portfolio', version);
  const p = pf.data;
  const hasBenchmark = (p?.benchmark_curve?.length ?? 0) >= 2;
  const positions = p?.positions ?? [];
  const drawdown = p?.drawdown_pct;

  const [closingSymbols, setClosingSymbols] = useState<Set<string>>(new Set());

  const handleClosePosition = useCallback(
    async (symbol: string) => {
      setClosingSymbols((prev) => new Set(prev).add(symbol));
      try {
        await postJson('/api/paper/close', { symbol });
        onRefresh?.();
      } catch (err) {
        alert(`Close position failed for ${symbol}: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        setClosingSymbols((prev) => {
          const next = new Set(prev);
          next.delete(symbol);
          return next;
        });
      }
    },
    [onRefresh],
  );

  const columns = useMemo<Column<PositionRow>[]>(
    () => [
      ...BASE_COLUMNS,
      {
        key: 'action',
        header: 'Action',
        align: 'right',
        render: (r) => {
          const sym = r.symbol ?? '';
          const closing = closingSymbols.has(sym);
          return (
            <button
              type="button"
              disabled={closing || !sym}
              onClick={() => handleClosePosition(sym)}
              className="rounded border border-critical/40 bg-critical/10 px-2 py-0.5 text-[11px] font-medium text-critical-ink hover:bg-critical/20 disabled:opacity-50"
            >
              {closing ? 'Closing…' : 'Close / Sell'}
            </button>
          );
        },
      },
    ],
    [closingSymbols, handleClosePosition],
  );

  return (
    <ApiGate states={[pf]}>
      <div className="space-y-5">
        {/* The equity panel leads; the five tiles beside it answer the five
            questions a reader has about it, in the same KPI language as Today. */}
        <div className="grid gap-4 lg:grid-cols-5">
          <section className="panel flex flex-col justify-center p-5 lg:col-span-2">
            <Hero
              label={
                <span className="inline-flex items-center gap-1.5">
                  Equity <TermInfo code="equity" />
                </span>
              }
              value={inr(p?.equity)}
              missing={!isNum(p?.equity)}
              missingNote="portfolio equity not reported"
              footnote={
                <>
                  <Term code="watermark">high-water mark</Term> {inr(p?.watermark)} · {count(p?.n_positions)} open position
                  {p?.n_positions === 1 ? '' : 's'}
                </>
              }
            />
          </section>

          <div className="grid gap-4 sm:grid-cols-2 lg:col-span-3">
            <Kpi label="Cash" term="cash" value={inr(p?.cash)} accent="bg-[rgb(var(--nav-portfolio))]" />
            <Kpi label="Deployed" term="deployed" value={inr(p?.deployed)} accent="bg-s1" />
            <Kpi
              label="Portfolio heat"
              term="portfolio heat"
              value={pctFrac(p?.heat_pct)}
              accent="bg-[rgb(var(--nav-risk))]"
              missing={!isNum(p?.heat_pct)}
              missingNote="not reported"
              delta={<Meter fraction={p?.heat_pct} tone="warning" label="Portfolio heat" />}
              drill="#/risk"
            />
            <Kpi
              label="Drawdown"
              term="drawdown"
              value={<span className={toneClass(drawdownTone(drawdown))}>{signedPctFrac(drawdown)}</span>}
              accent="bg-[rgb(var(--nav-calibration))]"
              missing={!isNum(drawdown)}
              missingNote="not reported"
              delta={
                <span className="text-muted">
                  <span aria-hidden="true">{isNum(drawdown) && drawdown !== 0 ? '▼' : '–'}</span> from the high-water
                  mark
                </span>
              }
            />
          </div>
        </div>

        {/* Only claim a benchmark when one is actually plotted. The benchmark is
            the Nifty 500 PRICE index (ADR-0005) -- the free archive has no
            total-return series, so the label must not say TRI. */}
        <Panel
          title={hasBenchmark ? 'Equity curve vs. Nifty 500' : 'Equity curve'}
          subtitle={
            hasBenchmark
              ? 'Paper account value alongside the Nifty 500 index (price, dividends excluded), rebased to the same start'
              : 'Paper account value (the Nifty 500 line appears from the second recorded session)'
          }
          right={
            hasBenchmark ? <TermLabel code="NIFTY500_PRICE">Benchmark: Nifty 500</TermLabel> : undefined
          }
        >
          <EquityCurve
            equityCurve={p?.equity_curve ?? []}
            benchmarkCurve={p?.benchmark_curve ?? []}
            labels={{ primary: 'Paper account', benchmark: 'Nifty 500 index (price, rebased)' }}
            mode={mode ?? 'light'}
          />
        </Panel>

        <Panel
          title="Positions"
          subtitle={`${count(positions.length)} held · quantity here is shares you actually own`}
          bodyClassName="p-0"
        >
          <DataTable
            rows={positions}
            columns={columns}
            rowKey={(r, i) => `${text(r.symbol, 'row')}-${i}`}
            initialSort={{ key: 'pnl', dir: 'desc' }}
            dense
            caption="Open positions with entry, stop, target and P&L"
            empty="No open positions."
          />
        </Panel>

        <Panel
          title="Daily trades & ledger log"
          subtitle={`${count((p?.trades ?? []).length)} recorded fills · tamper-evident hash chain`}
          right={<TermLabel code="LEDGER_CHAIN">LEDGER_CHAIN</TermLabel>}
          bodyClassName="p-0"
        >
          <DataTable
            rows={p?.trades ?? []}
            columns={TRADE_COLUMNS}
            rowKey={(r, i) => `${text(r.symbol, 'trade')}-${r.seq ?? i}`}
            initialSort={{ key: 'd', dir: 'desc' }}
            dense
            caption="Ledger trade log and fills"
            empty="No recorded paper trades yet."
          />
        </Panel>
      </div>
    </ApiGate>
  );
}
