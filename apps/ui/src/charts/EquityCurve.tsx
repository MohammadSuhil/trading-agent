import { useState } from 'react';
import type { BenchmarkPoint, EquityPoint } from '../api/types';
import { useMeasure } from '../lib/useMeasure';
import { tokens, type Mode } from '../lib/theme';
import { Empty } from '../components/primitives';
import { inr, isNum, pctFrac, stamp } from '../lib/format';

const HEIGHT = 320;
const M = { top: 28, right: 20, bottom: 36, left: 68 };

type Point = {
  i: number;
  d: string;
  equity: number;
  drawdown_pct?: number | null;
  heat_pct?: number | null;
  px: number;
  py: number;
  benchVal?: number;
  benchPy?: number;
  cmpVal?: number;
  cmpPy?: number;
  /** Breadth regime -- the one that set the strategy's caps that day. */
  regime?: string;
  /** Index regime -- information only (and the comparison line's block source). */
  indexRegime?: string;
};

export type ComparisonPoint = {
  d?: string | null;
  equity?: number | null;
  regime?: string | null;
  index_regime?: string | null;
};

const REGIME_WORD: Record<string, string> = {
  TREND_UP: 'bull', TREND_DOWN: 'bear', CHOP: 'sideways', HIGH_VOL: 'high volatility', UNKNOWN: 'not yet measurable',
};

/**
 * Equity curve vs benchmark chart, with an optional comparison strategy.
 *
 * Renders account value alongside a benchmark rebased to the same starting
 * rupee value, and -- when `comparisonCurve` is given -- a second strategy
 * over the same dates (e.g. the ADR-0004 regime-filter variant). Values are
 * matched by DATE, never by position, so a missing day cannot shift a line.
 * Requires at least 2 recorded sessions; otherwise displays an honest refusal.
 */
export function EquityCurve({
  equityCurve,
  benchmarkCurve,
  comparisonCurve,
  labels,
  mode,
}: {
  equityCurve: EquityPoint[];
  benchmarkCurve: BenchmarkPoint[];
  comparisonCurve?: ComparisonPoint[];
  labels?: { primary?: string; comparison?: string; benchmark?: string };
  mode: Mode;
}) {
  const L = {
    primary: labels?.primary ?? 'Equity',
    comparison: labels?.comparison ?? 'Comparison',
    benchmark: labels?.benchmark ?? 'Benchmark (rebased to start)',
  };
  const [ref, { width }] = useMeasure<HTMLDivElement>();
  const [hover, setHover] = useState<Point | null>(null);
  const t = tokens(mode);

  const validEquity = (equityCurve ?? []).filter(
    (p): p is EquityPoint & { d: string; equity: number } => typeof p.d === 'string' && isNum(p.equity),
  );

  const first = validEquity[0];
  if (!first || validEquity.length < 2) {
    return (
      <Empty>
        Equity curve needs at least 2 recorded sessions ({equityCurve?.length ?? 0} recorded so far). As daily paper
        runs accumulate, your account value and the Nifty Total Return benchmark will plot here.
      </Empty>
    );
  }

  const w = Math.max(0, width - M.left - M.right);
  const h = HEIGHT - M.top - M.bottom;

  // Rebase benchmark to start of equity curve if possible
  const benchByDate = new Map<string, number>();
  (benchmarkCurve ?? []).forEach((b) => {
    if (typeof b.d === 'string' && isNum(b.close)) {
      benchByDate.set(b.d, b.close);
    }
  });

  const cmpByDate = new Map<string, { equity: number }>();
  (comparisonCurve ?? []).forEach((c) => {
    if (typeof c.d === 'string' && isNum(c.equity)) cmpByDate.set(c.d, { equity: c.equity });
  });

  const e0 = first.equity;
  const b0 = benchByDate.get(first.d);
  const canRebaseBench = isNum(b0) && b0 > 0;

  // Collect values to determine Y scale
  const allValues = validEquity.map((p) => p.equity);
  validEquity.forEach((p) => {
    const c = cmpByDate.get(p.d);
    if (c) allValues.push(c.equity);
    if (canRebaseBench) {
      const bc = benchByDate.get(p.d);
      if (isNum(bc)) {
        allValues.push((bc / b0) * e0);
      }
    }
  });

  const minY = Math.min(...allValues);
  const maxY = Math.max(...allValues);
  const pad = Math.max((maxY - minY) * 0.08, minY * 0.02, 100);
  const y0 = Math.max(0, minY - pad);
  const y1 = maxY + pad;

  const sx = (i: number) => M.left + (i / (validEquity.length - 1)) * w;
  const sy = (v: number) => M.top + (1 - (v - y0) / (y1 - y0 || 1)) * h;

  const points: Point[] = validEquity.map((p, i) => {
    const ep = p as EquityPoint & { regime?: string | null; index_regime?: string | null };
    const pt: Point = {
      i,
      d: p.d,
      equity: p.equity,
      drawdown_pct: p.drawdown_pct,
      heat_pct: p.heat_pct,
      px: sx(i),
      py: sy(p.equity),
      regime: typeof ep.regime === 'string' ? ep.regime : undefined,
      indexRegime: typeof ep.index_regime === 'string' ? ep.index_regime : undefined,
    };
    if (canRebaseBench) {
      const bc = benchByDate.get(p.d);
      if (isNum(bc)) {
        const val = (bc / b0) * e0;
        pt.benchVal = val;
        pt.benchPy = sy(val);
      }
    }
    const c = cmpByDate.get(p.d);
    if (c) {
      pt.cmpVal = c.equity;
      pt.cmpPy = sy(c.equity);
    }
    return pt;
  });

  // Line paths
  const equityPath = points.map((p, idx) => `${idx === 0 ? 'M' : 'L'} ${p.px} ${p.py}`).join(' ');
  const benchPoints = points.filter((p): p is Point & { benchVal: number; benchPy: number } => p.benchVal !== undefined);
  const benchPath =
    benchPoints.length >= 2
      ? benchPoints.map((p, idx) => `${idx === 0 ? 'M' : 'L'} ${p.px} ${p.benchPy}`).join(' ')
      : null;

  const cmpPoints = points.filter((p): p is Point & { cmpVal: number; cmpPy: number } => p.cmpVal !== undefined);
  const cmpPath =
    cmpPoints.length >= 2
      ? cmpPoints.map((p, idx) => `${idx === 0 ? 'M' : 'L'} ${p.px} ${p.cmpPy}`).join(' ')
      : null;

  // Hover column width: one session's horizontal slot (at least 1px).
  const colW = Math.max(1, w / Math.max(1, points.length - 1));

  // 4 Y ticks
  const yTicks = [0, 0.33, 0.66, 1].map((pct) => y0 + pct * (y1 - y0));

  // 3 X ticks (start, middle, end)
  const xIndices = [0, Math.floor((points.length - 1) / 2), points.length - 1];

  return (
    <div ref={ref} className="relative w-full" style={{ height: HEIGHT }}>
      {width > 0 && (
        <>
          <div className="absolute right-4 top-1 z-10 flex flex-wrap items-center gap-4 text-[11px] text-muted">
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-3 rounded" style={{ backgroundColor: t.series1 }} />
              <span className="text-ink">{L.primary}</span>
            </span>
            {cmpPath && (
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block h-0 w-3 border-b border-dashed"
                  style={{ borderColor: t.warning, borderWidth: '2px' }}
                />
                <span className="text-ink">{L.comparison}</span>
              </span>
            )}
            {benchPath && (
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block h-0 w-3 border-b border-dotted"
                  style={{ borderColor: t.muted, borderWidth: '2px' }}
                />
                <span>{L.benchmark}</span>
              </span>
            )}
          </div>

          <svg
            width={width}
            height={HEIGHT}
            role="img"
            aria-label={`Equity curve showing ${points.length} sessions of account value vs benchmark`}
          >
            {/* Grid & Y-axis labels */}
            {yTicks.map((v, idx) => {
              const yPos = sy(v);
              return (
                <g key={`y-${idx}`}>
                  <line x1={M.left} x2={M.left + w} y1={yPos} y2={yPos} stroke={t.grid} strokeWidth={1} />
                  <text x={M.left - 8} y={yPos + 4} textAnchor="end" fill={t.muted} fontSize={10} className="tnum">
                    {inr(v)}
                  </text>
                </g>
              );
            })}

            {/* X-axis labels */}
            {xIndices.map((idx) => {
              const pt = points[idx];
              if (!pt) return null;
              const anchor = idx === 0 ? 'start' : idx === points.length - 1 ? 'end' : 'middle';
              return (
                <text
                  key={`x-${idx}`}
                  x={pt.px}
                  y={HEIGHT - 12}
                  textAnchor={anchor}
                  fill={t.muted}
                  fontSize={10}
                  className="tnum"
                >
                  {stamp(pt.d)}
                </text>
              );
            })}

            {/* Axes */}
            <line x1={M.left} x2={M.left + w} y1={M.top + h} y2={M.top + h} stroke={t.axis} strokeWidth={1} />
            <line x1={M.left} x2={M.left} y1={M.top} y2={M.top + h} stroke={t.axis} strokeWidth={1} />

            {/* Benchmark line (dotted) */}
            {benchPath && (
              <path
                d={benchPath}
                fill="none"
                stroke={t.muted}
                strokeWidth={1.5}
                strokeDasharray="1.5 3"
                strokeLinecap="round"
              />
            )}

            {/* Comparison strategy (dashed) */}
            {cmpPath && (
              <path d={cmpPath} fill="none" stroke={t.warning} strokeWidth={1.75} strokeDasharray="6 4" />
            )}

            {/* Equity line (solid) */}
            <path
              d={equityPath}
              fill="none"
              stroke={t.series1}
              strokeWidth={2}
            />

            {/* Vertical crosshair on hover */}
            {hover && (
              <line
                x1={hover.px}
                x2={hover.px}
                y1={M.top}
                y2={M.top + h}
                stroke={t.axis}
                strokeWidth={1}
                strokeDasharray="2 2"
              />
            )}

            {/* Interactive hit areas */}
            {points.map((p, idx) => (
              <g
                key={`pt-${idx}`}
                tabIndex={0}
                role="img"
                aria-label={`${stamp(p.d)}: ${L.primary} ${inr(p.equity)}${
                  p.cmpVal !== undefined ? `, ${L.comparison} ${inr(p.cmpVal)}` : ''
                }`}
                onPointerEnter={() => setHover(p)}
                onPointerLeave={() => setHover(null)}
                onFocus={() => setHover(p)}
                onBlur={() => setHover(null)}
              >
                {/* Full-height column per session: with several lines, a hit
                    circle on the primary line alone made the others un-hoverable. */}
                <rect
                  x={p.px - colW / 2}
                  y={M.top}
                  width={colW}
                  height={h}
                  fill="transparent"
                />
                {hover?.i === p.i && (
                  <>
                    {p.benchPy !== undefined && (
                      <circle cx={p.px} cy={p.benchPy} r={4} fill={t.muted} stroke={t.surface} strokeWidth={1.5} />
                    )}
                    {p.cmpPy !== undefined && (
                      <circle cx={p.px} cy={p.cmpPy} r={4} fill={t.warning} stroke={t.surface} strokeWidth={1.5} />
                    )}
                    <circle cx={p.px} cy={p.py} r={5} fill={t.series1} stroke={t.surface} strokeWidth={2} />
                  </>
                )}
              </g>
            ))}
          </svg>

          {hover && (
            <div
              className="pointer-events-none absolute z-20 rounded-lg border border-line-strong bg-raised p-2.5 text-xs shadow-pop"
              style={{
                left: Math.min(hover.px + 14, Math.max(10, width - 250)),
                top: Math.max(10, hover.py - 20),
              }}
            >
              <div className="font-mono text-[11px] font-semibold text-ink">{stamp(hover.d)}</div>
              <div className="tnum mt-1 text-[13px] font-semibold text-ink">
                {L.primary}: {inr(hover.equity)}
              </div>
              {isNum(hover.drawdown_pct) && (
                <div className="tnum text-ink2">
                  {/* stored as a positive fraction below the high-water mark; a
                      "+" read as a gain, so it is always shown as a loss */}
                  Drawdown: {hover.drawdown_pct === 0 ? pctFrac(0) : `−${pctFrac(Math.abs(hover.drawdown_pct))}`}
                </div>
              )}
              {isNum(hover.heat_pct) && (
                <div className="tnum text-ink2">
                  Heat: {pctFrac(hover.heat_pct)}
                </div>
              )}
              {hover.regime && (
                <div className="text-[11px] text-ink2">
                  Market regime (sets limits): <b>{REGIME_WORD[hover.regime] ?? hover.regime.toLowerCase()}</b>
                </div>
              )}
              {hover.cmpVal !== undefined && (
                <div className="tnum mt-1 border-t border-line pt-1">
                  <div className="font-semibold" style={{ color: t.warning }}>
                    {L.comparison}: {inr(hover.cmpVal)}
                  </div>
                  <div className="text-ink2">
                    vs {L.primary.toLowerCase()}: {hover.cmpVal - hover.equity >= 0 ? '+' : '−'}
                    {inr(Math.abs(hover.cmpVal - hover.equity))}
                  </div>
                  {hover.indexRegime === 'TREND_DOWN' && (
                    <div className="text-[11px] text-muted">index bear — new buys blocked on this line</div>
                  )}
                </div>
              )}
              {hover.indexRegime && (
                <div className="mt-1 text-[11px] text-muted">
                  Index regime (information only): {REGIME_WORD[hover.indexRegime] ?? hover.indexRegime.toLowerCase()}
                </div>
              )}
              {hover.benchVal !== undefined && (
                <div className="tnum mt-1 border-t border-line pt-1 text-[11px] text-muted">
                  {L.benchmark}: {inr(hover.benchVal)}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
