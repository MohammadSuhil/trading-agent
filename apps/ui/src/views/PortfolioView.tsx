import { useApi } from '../api/client';
import type { Portfolio, PositionRow } from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { DataTable, type Column } from '../components/DataTable';
import { TagChip } from '../components/StateChip';
import { Delta, Hero, Meter, Panel } from '../components/primitives';
import { Kpi } from '../components/ui';
import { count, inr, isNum, pctFrac, price, signedInr, signedPctFrac, stamp, text } from '../lib/format';

const COLUMNS: Column<PositionRow>[] = [
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
  },
  {
    key: 'pnl_pct',
    header: 'P&L %',
    align: 'right',
    sortable: true,
    value: (r) => r.pnl_pct,
    render: (r) => <Delta value={r.pnl_pct} format={(v) => signedPctFrac(v)} />,
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
  { key: 'horizon', header: 'Horizon', sortable: true, value: (r) => r.horizon, render: (r) => <TagChip value={r.horizon} /> },
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

export function PortfolioView({ version }: { version: number }) {
  const pf = useApi<Portfolio>('/api/portfolio', version);
  const p = pf.data;
  const positions = p?.positions ?? [];
  const drawdown = p?.drawdown_pct;

  return (
    <ApiGate states={[pf]}>
      <div className="space-y-5">
        {/* The equity panel leads; the five tiles beside it answer the five
            questions a reader has about it, in the same KPI language as Today. */}
        <div className="grid gap-4 lg:grid-cols-5">
          <section className="panel flex flex-col justify-center p-5 lg:col-span-2">
            <Hero
              label="Equity"
              value={inr(p?.equity)}
              missing={!isNum(p?.equity)}
              missingNote="portfolio equity not reported"
              footnote={
                <>
                  high-water mark {inr(p?.watermark)} · {count(p?.n_positions)} open position
                  {p?.n_positions === 1 ? '' : 's'}
                </>
              }
            />
          </section>

          <div className="grid gap-4 sm:grid-cols-2 lg:col-span-3">
            <Kpi label="Cash" value={inr(p?.cash)} accent="bg-[rgb(var(--nav-portfolio))]" />
            <Kpi label="Deployed" value={inr(p?.deployed)} accent="bg-s1" />
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

        <Panel
          title="Positions"
          subtitle={`${count(positions.length)} held · quantity here is shares you actually own`}
          bodyClassName="p-0"
        >
          <DataTable
            rows={positions}
            columns={COLUMNS}
            rowKey={(r, i) => `${text(r.symbol, 'row')}-${i}`}
            initialSort={{ key: 'pnl', dir: 'desc' }}
            dense
            caption="Open positions with entry, stop, target and P&L"
            empty="No open positions."
          />
        </Panel>
      </div>
    </ApiGate>
  );
}
