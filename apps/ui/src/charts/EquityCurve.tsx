import { useState } from 'react';
import type { BenchmarkPoint, EquityPoint } from '../api/types';
import { useMeasure } from '../lib/useMeasure';
import { tokens, type Mode } from '../lib/theme';
import { Empty } from '../components/primitives';
import { inr, isNum, pctFrac, signedPctFrac, stamp } from '../lib/format';

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
};

/**
 * Equity curve vs benchmark chart.
 *
 * Renders account value alongside the rebased Nifty Total Return Index.
 * Requires at least 2 recorded sessions; otherwise displays an honest refusal.
 */
export function EquityCurve({
  equityCurve,
  benchmarkCurve,
  mode,
}: {
  equityCurve: EquityPoint[];
  benchmarkCurve: BenchmarkPoint[];
  mode: Mode;
}) {
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

  const e0 = first.equity;
  const b0 = benchByDate.get(first.d);
  const canRebaseBench = isNum(b0) && b0 > 0;

  // Collect values to determine Y scale
  const allValues = validEquity.map((p) => p.equity);
  validEquity.forEach((p) => {
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
    const pt: Point = {
      i,
      d: p.d,
      equity: p.equity,
      drawdown_pct: p.drawdown_pct,
      heat_pct: p.heat_pct,
      px: sx(i),
      py: sy(p.equity),
    };
    if (canRebaseBench) {
      const bc = benchByDate.get(p.d);
      if (isNum(bc)) {
        const val = (bc / b0) * e0;
        pt.benchVal = val;
        pt.benchPy = sy(val);
      }
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
              <span className="text-ink">Equity</span>
            </span>
            {benchPath && (
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block h-0 w-3 border-b border-dashed"
                  style={{ borderColor: t.muted, borderWidth: '1.5px' }}
                />
                <span>Benchmark (rebased to start)</span>
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

            {/* Benchmark line (dashed) */}
            {benchPath && (
              <path
                d={benchPath}
                fill="none"
                stroke={t.muted}
                strokeWidth={1.5}
                strokeDasharray="4 4"
              />
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
                aria-label={`${stamp(p.d)}: equity ${inr(p.equity)}`}
                onPointerEnter={() => setHover(p)}
                onPointerLeave={() => setHover(null)}
                onFocus={() => setHover(p)}
                onBlur={() => setHover(null)}
              >
                <circle cx={p.px} cy={p.py} r={Math.max(10, w / points.length / 2)} fill="transparent" />
                {hover?.i === p.i && (
                  <>
                    {p.benchPy !== undefined && (
                      <circle cx={p.px} cy={p.benchPy} r={4} fill={t.muted} stroke={t.surface} strokeWidth={1.5} />
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
                left: Math.min(hover.px + 14, Math.max(10, width - 210)),
                top: Math.max(10, hover.py - 20),
              }}
            >
              <div className="font-mono text-[11px] font-semibold text-ink">{stamp(hover.d)}</div>
              <div className="tnum mt-1 text-[13px] font-semibold text-ink">
                Equity: {inr(hover.equity)}
              </div>
              {isNum(hover.drawdown_pct) && (
                <div className="tnum text-ink2">
                  Drawdown: {signedPctFrac(hover.drawdown_pct)}
                </div>
              )}
              {isNum(hover.heat_pct) && (
                <div className="tnum text-ink2">
                  Heat: {pctFrac(hover.heat_pct)}
                </div>
              )}
              {hover.benchVal !== undefined && (
                <div className="tnum mt-1 border-t border-line pt-1 text-[11px] text-muted">
                  Benchmark (rebased): {inr(hover.benchVal)}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
