import { useState } from 'react';
import type { CalibrationBin } from '../api/types';
import { useMeasure } from '../lib/useMeasure';
import { tokens, type Mode } from '../lib/theme';
import { count, isNum, pctFrac } from '../lib/format';

const HEIGHT = 320;
const M = { top: 12, right: 16, bottom: 36, left: 48 };

type Point = { x: number; y: number; n: number | null; px: number; py: number };

/**
 * Reliability diagram — stated probability against realised frequency.
 *
 * This chart only ever renders when `/api/calibration` says `state: "OK"`.
 * When the server says INSUFFICIENT the view shows the refusal and the
 * shortfall instead, because a reliability curve drawn from twelve
 * observations is a picture of noise that reads as evidence.
 *
 * One series, so no legend box: the axis titles already say what is plotted.
 * The diagonal is the reference — points above it mean the stated probability
 * was too low.
 */
export function Reliability({ bins, mode }: { bins: CalibrationBin[]; mode: Mode }) {
  const [ref, { width }] = useMeasure<HTMLDivElement>();
  const [hover, setHover] = useState<Point | null>(null);
  const t = tokens(mode);

  const w = Math.max(0, width - M.left - M.right);
  const h = HEIGHT - M.top - M.bottom;
  const sx = (v: number) => M.left + v * w;
  const sy = (v: number) => M.top + (1 - v) * h;

  const points: Point[] = bins
    .filter((b): b is CalibrationBin & { p_stated: number; p_realised: number } => isNum(b.p_stated) && isNum(b.p_realised))
    .map((b) => ({
      x: b.p_stated,
      y: b.p_realised,
      n: isNum(b.n) ? b.n : null,
      px: sx(b.p_stated),
      py: sy(b.p_realised),
    }));

  const ticks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <div ref={ref} className="relative w-full" style={{ height: HEIGHT }}>
      {width > 0 && (
        <svg
          width={width}
          height={HEIGHT}
          role="img"
          aria-label={`Reliability diagram: ${points.length} bins of stated probability against realised frequency, with a perfect-calibration diagonal. The same values are listed in the table below.`}
        >
          {/* Gridlines: solid hairlines, one step off the surface. */}
          {ticks.map((v) => (
            <g key={`g${v}`}>
              <line x1={M.left} x2={M.left + w} y1={sy(v)} y2={sy(v)} stroke={t.grid} strokeWidth={1} />
              <text x={M.left - 8} y={sy(v) + 4} textAnchor="end" fill={t.muted} fontSize={11} className="tnum">
                {Math.round(v * 100)}%
              </text>
              <text x={sx(v)} y={HEIGHT - 14} textAnchor="middle" fill={t.muted} fontSize={11} className="tnum">
                {Math.round(v * 100)}%
              </text>
            </g>
          ))}

          {/* Perfect-calibration reference. */}
          <line x1={sx(0)} y1={sy(0)} x2={sx(1)} y2={sy(1)} stroke={t.axis} strokeWidth={1} />
          {/* Captioned in the top-left corner rather than rotated along the
              line: a reliability diagram's points hug the diagonal, so a label
              riding it lands on top of the data. The upper-left is the one
              region that is reliably empty. */}
          <text x={M.left + 10} y={M.top + 14} fill={t.muted} fontSize={10}>
            diagonal = perfectly calibrated
          </text>

          <line x1={M.left} x2={M.left + w} y1={sy(0)} y2={sy(0)} stroke={t.axis} strokeWidth={1} />
          <line x1={M.left} x2={M.left} y1={M.top} y2={sy(0)} stroke={t.axis} strokeWidth={1} />

          {points.map((p, i) => (
            <g
              key={i}
              tabIndex={0}
              role="img"
              aria-label={`Stated ${pctFrac(p.x, 0)}, realised ${pctFrac(p.y, 0)}, n ${count(p.n)}`}
              onPointerEnter={() => setHover(p)}
              onPointerLeave={() => setHover(null)}
              onFocus={() => setHover(p)}
              onBlur={() => setHover(null)}
            >
              {/* 24px transparent hit target — an 8px dot is a pinpoint. */}
              <circle cx={p.px} cy={p.py} r={12} fill="transparent" />
              {/* 2px surface ring so overlapping bins stay legible. */}
              <circle cx={p.px} cy={p.py} r={5} fill={t.series1} stroke={t.surface} strokeWidth={2} />
            </g>
          ))}

          <text x={M.left + w / 2} y={HEIGHT - 1} textAnchor="middle" fill={t.ink2} fontSize={11}>
            stated probability
          </text>
          <text
            x={12}
            y={M.top + h / 2}
            textAnchor="middle"
            fill={t.ink2}
            fontSize={11}
            transform={`rotate(-90 12 ${M.top + h / 2})`}
          >
            realised frequency
          </text>
        </svg>
      )}

      {hover && (
        <div
          className="pointer-events-none absolute z-10 rounded border border-line bg-surface px-2 py-1.5 text-xs shadow-lg"
          style={{ left: Math.min(hover.px + 14, Math.max(0, width - 150)), top: Math.max(0, hover.py - 10) }}
        >
          <div className="tnum font-semibold text-ink">{pctFrac(hover.y, 1)} realised</div>
          <div className="tnum text-muted">
            at {pctFrac(hover.x, 1)} stated · n {count(hover.n)}
          </div>
        </div>
      )}
    </div>
  );
}
