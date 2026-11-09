import { useState } from 'react';
import type { SimBucket } from '../api/types';
import { useMeasure } from '../lib/useMeasure';
import { tokens, type Mode } from '../lib/theme';
import { count, isNum, num } from '../lib/format';

const HEIGHT = 300;
const M = { top: 12, right: 12, bottom: 38, left: 48 };
const MAX_BAR = 24; // bars are capped; the band's leftover is air, not ink

type Hover = { bucket: number; n: number; x: number; y: number } | null;

/** Round a y-axis top up to a clean number so the ticks read as 0 / 10 / 20. */
function niceMax(v: number): number {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (v <= step * mag) return step * mag;
  }
  return 10 * mag;
}

/**
 * Bootstrap outcome histogram.
 *
 * Renders only when `/api/simulation` reports `status: "OK"`. Bars are coloured
 * by the sign of the bucket, using the same two diverging poles as the rest of
 * the app — that is polarity, not identity, and the signed x-axis labels carry
 * the same information for anyone the colour does not reach.
 */
export function Histogram({ buckets, mode }: { buckets: SimBucket[]; mode: Mode }) {
  const [ref, { width }] = useMeasure<HTMLDivElement>();
  const [hover, setHover] = useState<Hover>(null);
  const t = tokens(mode);

  const data = buckets
    .filter((b): b is SimBucket & { bucket: number; count: number } => isNum(b.bucket) && isNum(b.count))
    .sort((a, b) => a.bucket - b.bucket);

  const w = Math.max(0, width - M.left - M.right);
  const h = HEIGHT - M.top - M.bottom;
  const top = niceMax(Math.max(0, ...data.map((d) => d.count)));
  const band = data.length > 0 ? w / data.length : 0;
  const barW = Math.min(MAX_BAR, Math.max(1, band - 2)); // 2px surface gap
  const sy = (v: number) => M.top + (1 - v / top) * h;

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((k) => Math.round(top * k));
  const uniqueTicks = Array.from(new Set(ticks));

  // Label about six buckets along the x-axis, not all of them.
  const labelEvery = Math.max(1, Math.ceil(data.length / 6));

  return (
    <div ref={ref} className="relative w-full" style={{ height: HEIGHT }}>
      {width > 0 && data.length > 0 && (
        <svg
          width={width}
          height={HEIGHT}
          role="img"
          aria-label={`Histogram of simulated outcomes across ${data.length} buckets. Each bar is focusable for its bucket and count.`}
        >
          {uniqueTicks.map((v) => (
            <g key={v}>
              <line x1={M.left} x2={M.left + w} y1={sy(v)} y2={sy(v)} stroke={t.grid} strokeWidth={1} />
              <text x={M.left - 8} y={sy(v) + 4} textAnchor="end" fill={t.muted} fontSize={11} className="tnum">
                {count(v)}
              </text>
            </g>
          ))}
          <line x1={M.left} x2={M.left + w} y1={sy(0)} y2={sy(0)} stroke={t.axis} strokeWidth={1} />

          {data.map((d, i) => {
            const cx = M.left + band * (i + 0.5);
            const x = cx - barW / 2;
            const y = sy(d.count);
            const bh = Math.max(0, sy(0) - y);
            const fill = d.bucket >= 0 ? t.polePos : t.poleNeg;
            return (
              <g
                key={`${d.bucket}-${i}`}
                tabIndex={0}
                role="img"
                aria-label={`Bucket ${num(d.bucket)}: ${count(d.count)} paths`}
                onPointerEnter={() => setHover({ bucket: d.bucket, n: d.count, x: cx, y })}
                onPointerLeave={() => setHover(null)}
                onFocus={() => setHover({ bucket: d.bucket, n: d.count, x: cx, y })}
                onBlur={() => setHover(null)}
              >
                {/* Hit target spans the whole band, not just the painted bar. */}
                <rect x={M.left + band * i} y={M.top} width={Math.max(1, band)} height={h} fill="transparent" />
                {/* 4px rounded data-end, square at the baseline: drawn as a
                    rounded rect with the bottom corners squared off by a
                    second rect, so the bar still grows from the baseline. */}
                <rect x={x} y={y} width={barW} height={bh} rx={4} fill={fill} className="hover:opacity-80" />
                {bh > 4 && <rect x={x} y={Math.max(y, sy(0) - 4)} width={barW} height={Math.min(4, bh)} fill={fill} />}
              </g>
            );
          })}

          {data.map((d, i) =>
            i % labelEvery === 0 ? (
              <text
                key={`l${i}`}
                x={M.left + band * (i + 0.5)}
                y={HEIGHT - 20}
                textAnchor="middle"
                fill={t.muted}
                fontSize={11}
                className="tnum"
              >
                {num(d.bucket, 2)}
              </text>
            ) : null,
          )}

          <text x={M.left + w / 2} y={HEIGHT - 2} textAnchor="middle" fill={t.ink2} fontSize={11}>
            outcome bucket (units as sent by /api/simulation)
          </text>
        </svg>
      )}

      {width > 0 && data.length === 0 && (
        <div className="flex h-full items-center justify-center text-sm text-muted">
          No histogram buckets were returned.
        </div>
      )}

      {hover && (
        <div
          className="pointer-events-none absolute z-10 rounded border border-line bg-surface px-2 py-1.5 text-xs shadow-lg"
          style={{ left: Math.min(Math.max(0, hover.x + 10), Math.max(0, width - 140)), top: Math.max(0, hover.y - 36) }}
        >
          <div className="tnum font-semibold text-ink">{count(hover.n)} paths</div>
          <div className="tnum text-muted">bucket {num(hover.bucket, 2)}</div>
        </div>
      )}
    </div>
  );
}
