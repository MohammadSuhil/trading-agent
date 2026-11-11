import { useState, type ReactNode } from 'react';
import { postJson, useApi } from '../api/client';
import type { Backtest, Calibration, CalibrationBin, N, Simulation } from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { DataTable, type Column } from '../components/DataTable';
import { SufficiencyChip } from '../components/StateChip';
import { Chip, Meter, Panel, StatTile } from '../components/primitives';
import { Term, TermInfo } from '../components/Tooltip';
import { Reliability } from '../charts/Reliability';
import { Histogram } from '../charts/Histogram';
import { EquityCurve } from '../charts/EquityCurve';
import type { Mode } from '../lib/theme';
import { EM_DASH, EN_DASH, count, inr, isNum, num, pctFrac, signedPctFrac, text } from '../lib/format';

/**
 * The refusal panel.
 *
 * This is the shape an honest "we don't know yet" takes: the claim that is NOT
 * being made, the shortfall in the server's own numbers, and a bar showing how
 * far off it is. No chart, because a chart of twelve observations reads as
 * evidence of calibration to anyone who glances at it, and a glance is what
 * dashboards get.
 */
function Refusal({
  heading,
  have,
  need,
  unit,
  unitTerm,
  note,
  secondary,
}: {
  heading: string;
  have: unknown;
  need: unknown;
  unit: string;
  /** Glossary code for the unit, so the refusal explains its own yardstick. */
  unitTerm?: string;
  note?: string;
  secondary?: { have: unknown; need: unknown; unit: string };
}) {
  // Meter takes a 0..1 fraction. The two raw counts are printed above it in
  // full, so the bar is only ever a shape for a number already on screen.
  const progress = isNum(have) && isNum(need) && need > 0 ? Math.min(1, have / need) : null;
  return (
    <div className="rounded-lg border border-warning/50 bg-warning/[0.14] p-4">
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="mt-0.5 font-semibold">
          !
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[14px] font-semibold text-ink">{heading}</h3>
          <p className="tnum mt-1.5 text-[22px] font-semibold tracking-[-0.01em] text-ink">
            {isNum(have) ? count(have) : EM_DASH} of {isNum(need) ? count(need) : EM_DASH} {unit}
            {unitTerm && <TermInfo code={unitTerm} className="ml-2 align-middle" />}
          </p>
          {secondary && (
            <p className="tnum mt-0.5 text-sm text-ink2">
              {isNum(secondary.have) ? count(secondary.have) : EM_DASH} of{' '}
              {isNum(secondary.need) ? count(secondary.need) : EM_DASH} {secondary.unit}
            </p>
          )}
          <div className="mt-3 max-w-sm">
            <Meter fraction={progress} tone="warning" label={`${unit} collected against the threshold`} />
          </div>
          {note && <p className="mt-3 text-sm text-ink2">{note}</p>}
        </div>
      </div>
    </div>
  );
}

const BIN_COLUMNS: Column<CalibrationBin>[] = [
  {
    key: 'p_stated',
    header: 'Stated',
    align: 'right',
    // Deliberately NOT sortable — see DataTable's forbidden-sort guard. These
    // are probability columns; the table is ordered as the server sent it.
    render: (b) => pctFrac(b.p_stated, 1),
  },
  { key: 'p_realised', header: 'Realised', align: 'right', render: (b) => pctFrac(b.p_realised, 1) },
  { key: 'n', header: 'n', align: 'right', render: (b) => count(b.n) },
];

function CalibrationPanel({ data, mode }: { data: Calibration | null; mode: Mode }) {
  const insufficient = data?.state !== 'OK';
  const bins = data?.bins ?? [];

  return (
    <Panel
      title="Calibration"
      subtitle="Do stated probabilities match realised frequencies?"
      right={<SufficiencyChip value={data?.state} />}
    >
      {insufficient ? (
        <Refusal
          heading={
            data?.state === 'NOT_BUILT'
              ? 'Calibrator not built yet (M8). No calibration figure is being claimed.'
              : 'Calibration is not established. No calibration figure is being claimed.'
          }
          have={data?.n_eff}
          need={data?.needed}
          unit="effective observations (n_eff)"
          unitTerm="n_eff"
          note={text(
            data?.note,
            'The backend has not seen enough resolved outcomes to say whether its probabilities are honest.',
          )}
        />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <StatTile
              label="n_eff"
              value={count(data?.n_eff)}
              footnote={
                <>
                  threshold {count(data?.needed)} <TermInfo code="n_eff" className="ml-1 align-middle" />
                </>
              }
            />
            <StatTile
              label="Resolution"
              value={num(data?.resolution)}
              footnote={
                <>
                  as sent by the API <TermInfo code="resolution" className="ml-1 align-middle" />
                </>
              }
            />
            <StatTile
              label="Brier skill score"
              value={num(data?.bss)}
              footnote={
                <>
                  as sent by the API <TermInfo code="bss" className="ml-1 align-middle" />
                </>
              }
            />
          </div>
          <Reliability bins={bins} mode={mode} />
          {/* The chart's table twin. */}
          <DataTable
            rows={bins}
            columns={BIN_COLUMNS}
            rowKey={(_b, i) => `bin-${i}`}
            dense
            caption="Calibration bins: stated probability, realised frequency and bin count"
            empty="state is OK but no bins were returned."
          />
          {data?.note && <p className="text-xs text-muted">{data.note}</p>}
        </div>
      )}
    </Panel>
  );
}

/**
 * `p_lo` / `p_hi` carry no unit in the contract. They are rendered as percents
 * when they look like probabilities and as plain numbers otherwise, rather than
 * being multiplied by 100 on faith.
 */
function interval(lo: unknown, hi: unknown): string {
  if (!isNum(lo) || !isNum(hi)) return EM_DASH;
  const probable = lo >= 0 && hi <= 1;
  return probable ? `${pctFrac(lo, 1)} ${EN_DASH} ${pctFrac(hi, 1)}` : `${num(lo)} ${EN_DASH} ${num(hi)}`;
}

function SimulationPanel({ data, mode }: { data: Simulation | null; mode: Mode }) {
  const insufficient = data?.status !== 'OK';
  const buckets = data?.histogram ?? [];

  return (
    <Panel
      title="Simulation"
      subtitle="Block-bootstrap of realised history"
      right={<SufficiencyChip value={data?.status} />}
    >
      {insufficient ? (
        // No p_lo / p_hi here on purpose, even if the server sent them: an
        // interval computed from too little history is still a claim about the
        // future, and the whole point of this panel is that the claim is not
        // being made yet.
        <Refusal
          heading="Insufficient history. No simulated outcome is being reported."
          have={data?.n_days}
          need={data?.needed_days}
          unit="sessions"
          secondary={{ have: data?.n_trades, need: data?.needed_trades, unit: 'trades' }}
          note={text(data?.note, 'The bootstrap needs more sessions and more resolved trades before it means anything.')}
        />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-4">
            <StatTile
              label="Outcome interval"
              value={<span className="tnum">{interval(data?.p_lo, data?.p_hi)}</span>}
              footnote="interval only — no midpoint"
            />
            <StatTile label="Sessions" value={count(data?.n_days)} footnote={`threshold ${count(data?.needed_days)}`} />
            <StatTile label="Trades" value={count(data?.n_trades)} footnote={`threshold ${count(data?.needed_trades)}`} />
            <StatTile
              label="Block length"
              value={count(data?.block_len)}
              footnote={
                <>
                  bootstrap block <TermInfo code="block_len" className="ml-1 align-middle" />
                </>
              }
            />
          </div>
          <Histogram buckets={buckets} mode={mode} />
          {data?.note && <p className="text-xs text-muted">{data.note}</p>}
        </div>
      )}
    </Panel>
  );
}

function SystemIntegrityPanel({
  data,
  mode,
  onRefresh,
}: {
  data: Backtest | null;
  mode: Mode;
  onRefresh?: () => void;
}) {
  const [sessions, setSessions] = useState<number>(126);
  const [compare, setCompare] = useState<boolean>(true);
  const [running, setRunning] = useState<boolean>(false);
  const [runError, setRunError] = useState<string | null>(null);

  const handleRunBacktest = async () => {
    setRunning(true);
    setRunError(null);
    try {
      await postJson('/api/backtest/run', { sessions, compare });
      onRefresh?.();
    } catch (err) {
      setRunError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  };

  const statsInsufficient = data?.stats_state !== 'OK';

  return (
    <Panel
      title="System Integrity & Frozen Control"
      subtitle="Cryptographic audit trails and backtest significance"
      right={<SufficiencyChip value={data?.stats_state} />}
    >
      <div className="space-y-6">
        {/* Verification badges */}
        <div className="flex flex-wrap items-center gap-6 rounded-lg border border-line bg-sunken p-3.5">
          <div className="flex flex-wrap items-center gap-2">
            <Term code="FROZEN_CONTROL">
              <span className="text-xs font-semibold text-ink">Control Arm (weights.v1.json):</span>
            </Term>
            {data?.weights_verified ? (
              <Term code="FROZEN_CONTROL">
                <Chip tone="good" glyph="✓">Locked (v1)</Chip>
              </Term>
            ) : (
              <Term code="FROZEN_CONTROL">
                <Chip tone="warning" glyph="!">Unverified</Chip>
              </Term>
            )}
            {data?.weights_sha256 && (
              <span className="font-mono text-[11px] text-muted">
                {data.weights_sha256.slice(0, 12)}
              </span>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Term code="LEDGER_CHAIN">
              <span className="text-xs font-semibold text-ink">Paper Ledger Chain:</span>
            </Term>
            {data?.ledger_verified ? (
              <Term code="LEDGER_CHAIN">
                <Chip tone="good" glyph="✓">Hash-chain verified</Chip>
              </Term>
            ) : (
              <Term code="LEDGER_CHAIN">
                <Chip tone="warning" glyph="!">Unverified</Chip>
              </Term>
            )}
          </div>
        </div>

        {/* Historical Backtest Controls */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-sunken p-3.5">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-xs font-medium text-ink">
              <span>Backtest window:</span>
              <select
                value={sessions}
                onChange={(e) => setSessions(Number(e.target.value))}
                disabled={running}
                className="rounded border border-line bg-surface px-2.5 py-1 text-xs text-ink"
              >
                <option value={63}>63 sessions (~3M)</option>
                <option value={126}>126 sessions (~6M)</option>
                <option value={252}>252 sessions (~1Y)</option>
                {/* 750 is the forward simulation's refusal threshold (PLAN honesty rules) */}
                <option value={750}>750 sessions (~3Y, enables simulation)</option>
                <option value={1250}>1250 sessions (~5Y)</option>
              </select>
            </label>
            <label className="flex items-center gap-1.5 text-xs text-ink">
              <input
                type="checkbox"
                checked={compare}
                disabled={running}
                onChange={(e) => setCompare(e.target.checked)}
              />
              <Term code="REGIME_FILTER">
                <span>Also run the regime-filter comparison (~2× time)</span>
              </Term>
            </label>
            <button
              type="button"
              disabled={running}
              onClick={handleRunBacktest}
              className="rounded-lg border border-accent bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent-ink hover:bg-accent/20 disabled:opacity-50"
            >
              {running
                ? `Running backtest${compare ? 's' : ''}… (a few minutes for long windows)`
                : 'Run historical backtest'}
            </button>
          </div>
          {runError && (
            <div className="w-full text-xs text-critical-ink">
              {runError}
            </div>
          )}
        </div>

        {/* Deflated Backtest Statistics */}
        <div>
          <div className="mb-3">
            <h3 className="text-sm font-semibold text-ink">Deflated Backtest Statistics</h3>
            <p className="text-xs text-muted">Corrected for multiple testing, non-normality and track record length</p>
          </div>

          {statsInsufficient ? (
            <Refusal
              heading="Backtest significance is not established yet."
              have={data?.n_sessions}
              need={data?.needed_sessions ?? 63}
              unit="trading sessions"
              unitTerm="MinTRL"
              note={text(
                data?.note,
                'At least 63 sessions are required before Probabilistic and Deflated Sharpe Ratios are reported.',
              )}
            />
          ) : (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
                <StatTile
                  label="Final Equity"
                  value={inr(data?.final_equity)}
                  footnote="backtest end"
                />
                <StatTile
                  label="Total Return"
                  value={signedPctFrac(data?.total_return_pct, 2)}
                  footnote="compound return"
                />
                <StatTile
                  label="Annualised Sharpe"
                  value={num(data?.sharpe, 2)}
                  footnote="zero-risk benchmark"
                />
                <StatTile
                  label="PSR"
                  value={pctFrac(data?.psr, 1)}
                  footnote={
                    <>
                      Probabilistic Sharpe <TermInfo code="PSR" className="ml-1 align-middle" />
                    </>
                  }
                />
                <StatTile
                  label="DSR"
                  value={pctFrac(data?.dsr, 1)}
                  footnote={
                    <>
                      Deflated Sharpe <TermInfo code="DSR" className="ml-1 align-middle" />
                    </>
                  }
                />
                <StatTile
                  label="MinTRL"
                  value={
                    <>
                      {count(data?.min_trl)}{' '}
                      <span className="text-xs font-normal text-muted">sessions</span>
                    </>
                  }
                  footnote={
                    <>
                      Min Track Record <TermInfo code="MinTRL" className="ml-1 align-middle" />
                    </>
                  }
                />
              </div>

              <div className="mt-4">
                <div className="label mb-2">Control arm vs regime filter vs market</div>
                <EquityCurve
                  equityCurve={data?.equity_curve ?? []}
                  benchmarkCurve={data?.benchmark_curve ?? []}
                  comparisonCurve={data?.comparison?.curve ?? []}
                  labels={{
                    primary: 'Frozen control',
                    comparison: 'Regime filter (rejected)',
                    benchmark: 'Nifty 500 index (price, rebased)',
                  }}
                  mode={mode}
                />
                <ComparisonNotes data={data} />
              </div>
            </div>
          )}
        </div>
      </div>
    </Panel>
  );
}

/**
 * What each line is, how far to trust it, and the numbers side by side.
 *
 * Every figure here is sent by the API (the UI computes nothing); the only
 * logic is choosing which explanation applies -- e.g. saying the comparison
 * is stale rather than drawing a line from a different window.
 */
function ComparisonNotes({ data }: { data: Backtest | null }) {
  const c = data?.comparison;
  const bench = data?.benchmark_curve ?? [];
  const sameWindow = Boolean(c?.same_window && (c?.curve?.length ?? 0) >= 2);
  const rows: Array<{
    key: string; swatch: ReactNode; label: ReactNode; ret: N; equity: N; trades: N; sharpe: N; dd: N; win: N;
  }> = [
    {
      key: 'control',
      swatch: <span className="inline-block h-0.5 w-4 rounded bg-s1" />,
      label: <Term code="FROZEN_CONTROL"><span>Frozen control</span></Term>,
      ret: data?.total_return_pct ?? null, equity: data?.final_equity ?? null, trades: data?.n_trades ?? null,
      sharpe: data?.sharpe ?? null, dd: data?.max_drawdown ?? null, win: data?.win_rate ?? null,
    },
  ];
  if (sameWindow && c) {
    rows.push({
      key: 'variant',
      swatch: <span className="inline-block w-4 border-b-2 border-dashed border-warning" />,
      label: <Term code="REGIME_FILTER"><span>Regime filter (rejected)</span></Term>,
      ret: c.total_return_pct ?? null, equity: c.final_equity ?? null, trades: c.n_trades ?? null,
      sharpe: c.sharpe ?? null, dd: c.max_drawdown ?? null, win: c.win_rate ?? null,
    });
  }
  return (
    <div className="mt-4 space-y-3 text-xs">
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full text-left">
          <thead className="bg-sunken text-[11px] uppercase tracking-wide text-muted">
            <tr>
              <th className="px-3 py-2">Line</th>
              <th className="px-3 py-2 text-right">Return</th>
              <th className="px-3 py-2 text-right">Final equity</th>
              <th className="px-3 py-2 text-right">Trades</th>
              <th className="px-3 py-2 text-right">Sharpe</th>
              <th className="px-3 py-2 text-right">Max drawdown</th>
              <th className="px-3 py-2 text-right">Win rate</th>
            </tr>
          </thead>
          <tbody className="tnum">
            {rows.map((r) => (
              <tr key={r.key} className="border-t border-line">
                <td className="px-3 py-2"><span className="flex items-center gap-2">{r.swatch}{r.label}</span></td>
                <td className="px-3 py-2 text-right">{signedPctFrac(r.ret, 2)}</td>
                <td className="px-3 py-2 text-right">{inr(r.equity)}</td>
                <td className="px-3 py-2 text-right">{count(r.trades)}</td>
                <td className="px-3 py-2 text-right">{num(r.sharpe, 2)}</td>
                <td className="px-3 py-2 text-right">{isNum(r.dd) ? `−${pctFrac(Math.abs(r.dd), 1)}` : '—'}</td>
                <td className="px-3 py-2 text-right">{pctFrac(r.win, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul className="space-y-1.5 text-ink2">
        <li>
          <b className="text-ink">Frozen control</b> <TermInfo code="FROZEN_CONTROL" className="ml-0.5 align-middle" /> —
          the day-1 rules, locked forever. Every change must beat this line to count.
        </li>
        <li>
          <b className="text-ink">Regime filter (rejected)</b> <TermInfo code="REGIME_FILTER" className="ml-0.5 align-middle" /> —
          the same rules plus <Term code="REG-01"><span>REG-01</span></Term>: no new buys while the{' '}
          <Term code="INDEX_REGIME"><span>index regime</span></Term> is bear. Hover the chart to see the regime each day.
          It is <b>switched off</b> in live trading: tested on 2021–2025 data first, it did worse, because its bear
          signal only turns on after a fall and so blocks buying in the recovery (ADR-0004).
          {!sameWindow && (
            <span className="text-warning-ink">
              {' '}
              {c
                ? `Not drawn: the last comparison covered ${c.start_d} → ${c.end_d}, a different window. Re-run with the box ticked.`
                : 'Not drawn yet: tick the box above and run the backtest.'}
            </span>
          )}
        </li>
        <li>
          <b className="text-ink">Nifty 500 index</b> <TermInfo code="NIFTY500_PRICE" className="ml-0.5 align-middle" /> —
          the market itself, rescaled to start at the same ₹ value. It excludes dividends (≈1.2%/yr), so a small
          margin over it is a tie, not a win.
          {bench.length < 2 && <span className="text-warning-ink"> Not drawn: no index data for this window.</span>}
        </li>
        <li className="text-muted">
          All three share the same dates, costs and starting capital. The universe is today&apos;s Nifty 500 applied to
          the past (survivorship), which flatters the strategy lines equally — fine for comparing them, not for
          absolute returns.
        </li>
      </ul>
    </div>
  );
}

export function Honesty({
  version,
  mode,
  onRefresh,
}: {
  version: number;
  mode: Mode;
  onRefresh?: () => void;
}) {
  const calibration = useApi<Calibration>('/api/calibration', version);
  const simulation = useApi<Simulation>('/api/simulation', version);
  const backtest = useApi<Backtest>('/api/backtest', version);

  return (
    <ApiGate states={[calibration, simulation, backtest]}>
      <div className="space-y-6">
        <p className="max-w-3xl text-sm text-ink2">
          These panels exist to say what the system does <em>not</em> know. Until each has enough history, it
          reports the shortfall instead of a number — a probability the data cannot support is worse than no
          probability, because it gets acted on.
        </p>
        <SystemIntegrityPanel data={backtest.data} mode={mode} onRefresh={onRefresh} />
        <CalibrationPanel data={calibration.data} mode={mode} />
        <SimulationPanel data={simulation.data} mode={mode} />
      </div>
    </ApiGate>
  );
}
