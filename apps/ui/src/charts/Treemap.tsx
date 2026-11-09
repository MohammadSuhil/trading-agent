import { useMemo, useState } from 'react';
import { hierarchy, interpolateLab, treemap, treemapSquarify, type HierarchyRectangularNode } from 'd3';
import type { HeatmapRow } from '../api/types';
import { useMeasure, textWidth } from '../lib/useMeasure';
import { tokens, inkOn, type Mode } from '../lib/theme';
import { count, crore, isNum, pctFrac, signedPctFrac, text } from '../lib/format';

/**
 * Industry treemap — area is turnover, colour is the 1-day return.
 *
 * Two encodings, two jobs: area carries magnitude (which industries actually
 * traded), colour carries polarity (which way they went). Colour is a
 * DIVERGING scale — two opposed hues with a neutral grey at zero — not a
 * rainbow and not a one-hue ramp, because the reader's question is "which side
 * of flat", and grey has to mean "nothing happened".
 *
 * Blue is up and red is down. Not green/red: green-vs-red is the pair that
 * disappears for red-green colour blindness, and a trading screen is the worst
 * possible place to hide the sign of a number. Every cell that has room also
 * prints its signed value, every cell has a tooltip, and the table underneath
 * carries all of it — so colour is never the only channel.
 */

const HEIGHT = 460;
const PAD = 2; // the surface gap: 2px of background between cells, no borders
const NAME_FONT = '500 11px system-ui, -apple-system, "Segoe UI", sans-serif';
const VALUE_FONT = '600 12px system-ui, -apple-system, "Segoe UI", sans-serif';

type Leaf = { name: string; value: number; row: HeatmapRow };
type TreeNode = { name: string; value?: number; row?: HeatmapRow; children?: TreeNode[] };

type Hover = { row: HeatmapRow; x: number; y: number } | null;

function useDivergingScale(rows: HeatmapRow[], mode: Mode) {
  return useMemo(() => {
    const t = tokens(mode);
    const mags = rows.map((r) => (isNum(r.ret_1d) ? Math.abs(r.ret_1d) : 0));
    // Symmetric domain so the neutral midpoint sits exactly on zero. An
    // asymmetric domain would paint a flat industry faintly red on a down day,
    // which is a lie told by the colour scale rather than the data.
    const maxAbs = Math.max(1e-9, ...mags);
    const up = interpolateLab(t.poleMid, t.polePos);
    const dn = interpolateLab(t.poleMid, t.poleNeg);
    const color = (v: unknown): string => {
      if (!isNum(v)) return t.sunken; // no value: surface grey, never a pole
      const k = Math.min(1, Math.abs(v) / maxAbs);
      return v >= 0 ? up(k) : dn(k);
    };
    return { color, maxAbs, up, dn, t };
  }, [rows, mode]);
}

function Legend({
  maxAbs,
  up,
  dn,
}: {
  maxAbs: number;
  up: (t: number) => string;
  dn: (t: number) => string;
}) {
  // Eleven stops is enough to read as continuous without shipping a 256-stop
  // gradient definition into the DOM.
  const stops = Array.from({ length: 11 }, (_, i) => {
    const k = i / 10; // 0 -> left (most negative), 1 -> right (most positive)
    const v = (k - 0.5) * 2; // -1 .. 1
    return { offset: `${k * 100}%`, color: v >= 0 ? up(v) : dn(-v) };
  });
  return (
    <div className="flex items-center gap-3">
      <span className="text-[11px] text-muted">▼ {signedPctFrac(-maxAbs, 1)}</span>
      <svg width={160} height={10} role="img" aria-label="Colour scale: red for a negative 1-day return, grey at zero, blue for positive">
        <defs>
          <linearGradient id="ta-heat-legend" x1="0" x2="1">
            {stops.map((s) => (
              <stop key={s.offset} offset={s.offset} stopColor={s.color} />
            ))}
          </linearGradient>
        </defs>
        <rect x="0" y="0" width="160" height="10" rx="5" fill="url(#ta-heat-legend)" />
      </svg>
      <span className="text-[11px] text-muted">▲ {signedPctFrac(maxAbs, 1)}</span>
      <span className="text-[11px] text-muted">· colour = ret 1d, area = turnover</span>
    </div>
  );
}

export function Treemap({ rows, mode }: { rows: HeatmapRow[]; mode: Mode }) {
  const [ref, { width }] = useMeasure<HTMLDivElement>();
  const [hover, setHover] = useState<Hover>(null);
  const { color, maxAbs, up, dn, t } = useDivergingScale(rows, mode);

  // Only rows with a positive turnover can be given an area. The rest are not
  // dropped from the product — they stay in the table below, and the count of
  // them is printed — but a zero-area rectangle is not a thing we can draw.
  const { leaves, omitted } = useMemo(() => {
    const usable: Leaf[] = [];
    let skipped = 0;
    for (const r of rows) {
      if (isNum(r.turnover_cr) && r.turnover_cr > 0) {
        usable.push({ name: text(r.industry, 'unnamed'), value: r.turnover_cr, row: r });
      } else {
        skipped += 1;
      }
    }
    return { leaves: usable, omitted: skipped };
  }, [rows]);

  const nodes = useMemo<HierarchyRectangularNode<TreeNode>[]>(() => {
    if (width <= 0 || leaves.length === 0) return [];
    const root = hierarchy<TreeNode>({ name: 'root', children: leaves })
      .sum((d) => d.value ?? 0)
      .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
    const layout = treemap<TreeNode>().tile(treemapSquarify).size([width, HEIGHT]).paddingInner(PAD).round(true);
    return layout(root).leaves();
  }, [leaves, width]);

  const measureName = textWidth(NAME_FONT);
  const measureValue = textWidth(VALUE_FONT);

  return (
    <div>
      <div className="mb-3">
        <Legend maxAbs={maxAbs} up={up} dn={dn} />
      </div>

      <div ref={ref} className="relative w-full" style={{ height: HEIGHT }}>
        {width > 0 && nodes.length > 0 && (
          <svg width={width} height={HEIGHT} role="list" aria-label="Industries by turnover">
            {nodes.map((n) => {
              const w = Math.max(0, n.x1 - n.x0);
              const h = Math.max(0, n.y1 - n.y0);
              const row = n.data.row;
              if (!row || w < 1 || h < 1) return null;

              const fill = color(row.ret_1d);
              const ink = inkOn(fill);
              const name = text(row.industry, 'unnamed');
              const value = signedPctFrac(row.ret_1d, 2);

              // Measure before placing. A label that does not fit is simply not
              // drawn — never clipped, never overflowed. Its value is still in
              // the tooltip and in the table below, so nothing is gated on it.
              const showName = h >= 26 && measureName(name) <= w - 10;
              const showValue = h >= 42 && measureValue(value) <= w - 10;

              const label = `${name}. Turnover ${crore(row.turnover_cr)}. 1-day return ${value}. ${count(row.n)} names.`;

              return (
                <g
                  key={`${name}-${n.x0}-${n.y0}`}
                  role="listitem"
                  tabIndex={0}
                  aria-label={label}
                  className="cursor-default focus:outline-none"
                  onPointerMove={(e) => {
                    const box = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
                    setHover({ row, x: e.clientX - box.left, y: e.clientY - box.top });
                  }}
                  onPointerLeave={() => setHover(null)}
                  onFocus={() => setHover({ row, x: n.x0 + w / 2, y: n.y0 + h / 2 })}
                  onBlur={() => setHover(null)}
                >
                  <rect
                    x={n.x0}
                    y={n.y0}
                    width={w}
                    height={h}
                    rx={3}
                    fill={fill}
                    className="transition-opacity hover:opacity-80"
                  />
                  {showName && (
                    <text x={n.x0 + 5} y={n.y0 + 15} fill={ink} style={{ font: NAME_FONT }} pointerEvents="none">
                      {name}
                    </text>
                  )}
                  {showValue && (
                    <text
                      x={n.x0 + 5}
                      y={n.y0 + 31}
                      fill={ink}
                      opacity={0.85}
                      style={{ font: VALUE_FONT }}
                      pointerEvents="none"
                    >
                      {value}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        )}

        {width > 0 && nodes.length === 0 && (
          <div className="flex h-full items-center justify-center rounded border border-line text-sm text-muted">
            No industry has a turnover to size the map by.
          </div>
        )}

        {hover && (
          <div
            className="pointer-events-none absolute z-10 w-56 rounded border border-line bg-surface p-2 text-xs shadow-lg"
            style={{
              // Flip the tooltip before it leaves the plot rather than letting
              // the card grow a scrollbar.
              left: Math.min(Math.max(0, hover.x + 12), Math.max(0, width - 232)),
              top: Math.min(Math.max(0, hover.y + 12), HEIGHT - 150),
            }}
          >
            <div className="truncate font-semibold text-ink">{text(hover.row.industry, 'unnamed')}</div>
            <dl className="mt-1 space-y-0.5">
              {/* Values lead, labels follow: the reader already knows which
                  industry they are pointing at. */}
              <Row label="ret 1d" value={signedPctFrac(hover.row.ret_1d)} />
              <Row label="ret 5d" value={signedPctFrac(hover.row.ret_5d)} />
              <Row label="ret 21d" value={signedPctFrac(hover.row.ret_21d)} />
              <Row label="turnover" value={crore(hover.row.turnover_cr)} />
              <Row label="breadth" value={pctFrac(hover.row.breadth_pct)} />
              <Row label="names" value={count(hover.row.n)} />
            </dl>
          </div>
        )}
      </div>

      {omitted > 0 && (
        <p className="mt-2 text-xs text-muted">
          {omitted} {omitted === 1 ? 'industry is' : 'industries are'} not on the map — no turnover to size them by.
          They are still in the table below.
        </p>
      )}
      <p className="sr-only">
        The same figures are available as a sortable table immediately below this map.
      </p>
      {/* Keep the scale's hex in the DOM for the palette validator's page mode. */}
      <span className="hidden" data-palette={`${t.polePos},${t.poleNeg}`} />
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-muted">{label}</dt>
      <dd className="tnum font-medium text-ink">{value}</dd>
    </div>
  );
}
