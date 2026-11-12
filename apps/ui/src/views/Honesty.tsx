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
import { EM_DASH, EN_DASH, count, inr, isNum, num, pctFrac, signedPctFrac, stamp, text } from '../lib/format';

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

const STANDARD_WINDOWS: Array<{ value: number; label: string }> = [
  { value: 63, label: '63 sessions (~3M)' },
  { value: 126, label: '126 sessions (~6M)' },
  { value: 252, label: '252 sessions (~1Y)' },
  // 750 is the forward simulation's refusal threshold (PLAN honesty rules)
  { value: 750, label: '750 sessions (~3Y, enables simulation)' },
  { value: 1250, label: '1250 sessions (~5Y)' },
];

/** "2026-11-11T17:43:26.897Z" (UTC) -> "11 Nov, 23:13 IST". */
function istStamp(utc: string | null | undefined): string {
  if (!utc) return EM_DASH;
  // Accept ISO with or without an offset; bare "YYYY-MM-DD HH:MM:SS" is UTC.
  let iso = utc.trim().replace(' ', 'T');
  if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(iso)) iso += 'Z';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return utc;
  return `${t.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
  })} IST`;
}

function SystemIntegrityPanel({
  data,
  mode,
  sessions,
  onSessions,
  onRan,
}: {
  data: Backtest | null;
  mode: Mode;
  sessions: number;
  onSessions: (n: number) => void;
  onRan?: () => void;
}) {
  const [compare, setCompare] = useState<boolean>(true);
  const [running, setRunning] = useState<boolean>(false);
  const [runError, setRunError] = useState<string | null>(null);

  const handleRunBacktest = async () => {
    setRunning(true);
    setRunError(null);
    try {
      await postJson('/api/backtest/run', { sessions, compare });
      onRan?.();
    } catch (err) {
      setRunError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  };

  const statsInsufficient = data?.stats_state !== 'OK';
  const notRun = data?.state === 'NOT_RUN';
  // useApi keeps the previous render while refetching (and on a failed fetch),
  // so the shown data can belong to another window. Say so instead of
  // presenting it under the newly selected window's label.
  const stale = isNum(data?.sessions) && data?.sessions !== sessions;
  // Standard windows plus any window stored by a CLI run (`ta backtest --sessions N`).
  const stored = new Map((data?.windows ?? []).map((w) => [w.sessions ?? -1, w]));
  const windowOptions = [
    ...STANDARD_WINDOWS,
    ...[...stored.keys()]
      .filter((n) => n > 0 && !STANDARD_WINDOWS.some((w) => w.value === n))
      .map((n) => ({ value: n, label: `${n} sessions (CLI run)` })),
  ]
    .sort((x, y) => x.value - y.value)
    .map((o) => ({ ...o, lastRun: stored.get(o.value)?.last_run ?? null }));

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
                onChange={(e) => onSessions(Number(e.target.value))}
                disabled={running}
                className="rounded border border-line bg-surface px-2.5 py-1 text-xs text-ink"
              >
                {windowOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                    {o.lastRun ? ` · last run ${istStamp(o.lastRun)}` : ' · not run yet'}
                  </option>
                ))}
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
                <span>Also run the live-rules comparison (~2× time)</span>
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

          {stale && (
            <p className="mb-3 rounded-lg border border-warning/50 bg-warning/10 p-2.5 text-xs text-warning-ink">
              Loading the {sessions}-session result… The figures below are still the {count(data?.sessions)}-session
              run and will be replaced (if this does not change, the API may be restarting — refresh in a moment).
            </p>
          )}
          {!stale && data?.run && isNum(data?.benchmark?.excess_return_pct) && (
            <p className="mb-2 text-sm">
              <Term code="NIFTY500_PRICE">
                <span className="font-semibold">Benchmark verdict:</span>
              </Term>{' '}
              {data.benchmark?.beats_benchmark ? (
                <span className="text-good-ink">
                  beats the Nifty 500 by {signedPctFrac(data.benchmark?.excess_return_pct, 2)} pts over this window.
                </span>
              ) : (
                <span className="text-critical-ink">
                  trails the Nifty 500 by {pctFrac(Math.abs(data.benchmark?.excess_return_pct ?? 0), 2)} pts over
                  this window ({signedPctFrac(data.total_return_pct, 2)} vs {signedPctFrac(data.benchmark?.return_pct, 2)}).
                  Simply holding the index would have done better.
                </span>
              )}
            </p>
          )}
          {data?.run && (
            <p className="mb-3 text-xs text-ink2">
              Showing the last <b className="text-ink">{count(data.sessions)}-session</b> run, made{' '}
              <b className="text-ink">{istStamp(data.run.run_ts)}</b>, covering {stamp(data.run.start_d)} →{' '}
              {stamp(data.run.end_d)}. It stays here until this window is run again; other windows keep their
              own results.
            </p>
          )}
          {notRun ? (
            <Refusal
              heading={`No backtest stored for the ${sessions}-session window yet.`}
              have={0}
              need={sessions}
              unit="sessions run"
              note={text(data?.note, 'Click "Run historical backtest" to create one for this window.')}
            />
          ) : statsInsufficient ? (
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
                <div className="label mb-2">Frozen control vs live rules vs market</div>
                <EquityCurve
                  equityCurve={data?.equity_curve ?? []}
                  benchmarkCurve={data?.benchmark_curve ?? []}
                  comparisonCurve={data?.comparison?.curve ?? []}
                  labels={{
                    primary: 'Frozen control',
                    comparison: 'Live rules (v2 Sortino-Mom)',
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
  const bm = data?.benchmark;
  const rows: Array<{
    key: string; swatch: ReactNode; label: ReactNode; ret: N; equity: N; trades: N; sharpe: N; dd: N; win: N;
    excess: N;
  }> = [
    {
      key: 'control',
      swatch: <span className="inline-block h-0.5 w-4 rounded bg-s1" />,
      label: <Term code="FROZEN_CONTROL"><span>Frozen control</span></Term>,
      ret: data?.total_return_pct ?? null, equity: data?.final_equity ?? null, trades: data?.n_trades ?? null,
      sharpe: data?.sharpe ?? null, dd: data?.max_drawdown ?? null, win: data?.win_rate ?? null,
      excess: bm?.excess_return_pct ?? null,
    },
  ];
  if (sameWindow && c) {
    rows.push({
      key: 'variant',
      swatch: <span className="inline-block w-4 border-b-2 border-dashed border-warning" />,
      label: <Term code="REGIME_FILTER"><span>Live rules (v2 Sortino-Mom)</span></Term>,
      ret: c.total_return_pct ?? null, equity: c.final_equity ?? null, trades: c.n_trades ?? null,
      sharpe: c.sharpe ?? null, dd: c.max_drawdown ?? null, win: c.win_rate ?? null,
      excess: c.excess_return_pct ?? null,
    });
  }
  if (isNum(bm?.return_pct)) {
    rows.push({
      key: 'bench',
      swatch: <span className="inline-block w-4 border-b-2 border-dotted border-muted" />,
      label: <Term code="NIFTY500_PRICE"><span>Nifty 500 index (benchmark)</span></Term>,
      ret: bm?.return_pct ?? null, equity: null, trades: null, sharpe: null, dd: null, win: null, excess: null,
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
              <th className="px-3 py-2 text-right">vs Nifty 500</th>
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
                <td
                  className={`px-3 py-2 text-right ${
                    isNum(r.excess) ? (r.excess > 0 ? 'text-good-ink' : 'text-critical-ink') : 'text-muted'
                  }`}
                >
                  {r.key === 'bench' ? 'benchmark' : isNum(r.excess) ? `${signedPctFrac(r.excess, 2)} pts` : EM_DASH}
                </td>
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
          <b className="text-ink">Live rules (v2 Sortino-Mom)</b> <TermInfo code="REGIME_FILTER" className="ml-0.5 align-middle" /> —
          cross-sectional Sortino risk-adjusted momentum (0.45 × z-score of 12-month momentum, 0.35 × 126-day return, 0.20 × distance-to-52-week-high, all normalized by 252-day downside semideviation), evaluated on the survivorship-free point-in-time Top-350 liquid NSE EQ universe. Anchored monthly rebalance holds winners ranked ≤ 25 or above SMA200; daily protection via 12.5% initial stop, 18% trailing after +8% gain, and residual correlation gates. Won a pre-registered backtest against the frozen control: OOS 2021–07 +135.18% vs Nifty 500 +106.34% (5/5 starts, SR 1.05, DD 24%); IS 2025-07–2026-11 +46.33% vs +1.97% (5/5 starts, SR 1.61, DD 13.7%). Evaluated over n_trials = 36 (five start dates × six time windows, full Indian delivery costs).
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
  // The selected window drives BOTH the backtest and the simulation panels:
  // each window keeps its own last stored run (ev.backtest_runs).
  const [sessions, setSessions] = useState<number>(126);
  const [runNonce, setRunNonce] = useState(0);
  const calibration = useApi<Calibration>('/api/calibration', version);
  const simulation = useApi<Simulation>(`/api/simulation?sessions=${sessions}`, version + runNonce);
  const backtest = useApi<Backtest>(`/api/backtest?sessions=${sessions}`, version + runNonce);

  return (
    <ApiGate states={[calibration, simulation, backtest]}>
      <div className="space-y-6">
        <p className="max-w-3xl text-sm text-ink2">
          These panels exist to say what the system does <em>not</em> know. Until each has enough history, it
          reports the shortfall instead of a number — a probability the data cannot support is worse than no
          probability, because it gets acted on.
        </p>
        <SystemIntegrityPanel
          data={backtest.data}
          mode={mode}
          sessions={sessions}
          onSessions={setSessions}
          onRan={() => {
            setRunNonce((n) => n + 1);
            onRefresh?.();
          }}
        />
        <CalibrationPanel data={calibration.data} mode={mode} />
        <SimulationPanel data={simulation.data} mode={mode} />
      </div>
    </ApiGate>
  );
}
