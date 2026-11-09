import { useEffect, useRef, useState } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  CrosshairMode,
  type CandlestickData,
  type IChartApi,
  type Time,
} from 'lightweight-charts';
import type { Bar } from '../api/types';
import { tokens, type Mode } from '../lib/theme';
import { count, isNum, isStr, price } from '../lib/format';

const HEIGHT = 420;

type Readout = { d: string; o: number; h: number; l: number; c: number } | null;

/**
 * Daily candles.
 *
 * Up is blue and down is red, the same diverging poles the heat map uses, for
 * the same reason: the conventional green/red is the one pair red-green CVD
 * erases. The OHLC readout above the chart is the relief channel — every value
 * the candle encodes is also printed as text on hover and on crosshair move.
 */
export function Candles({ bars, symbol, mode }: { bars: Bar[]; symbol: string; mode: Mode }) {
  const host = useRef<HTMLDivElement>(null);
  const [readout, setReadout] = useState<Readout>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;

    const t = tokens(mode);
    let chart: IChartApi | null = null;

    try {
      chart = createChart(el, {
        autoSize: true,
        layout: {
          background: { type: ColorType.Solid, color: t.surface },
          textColor: t.muted,
          fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
          attributionLogo: false,
        },
        // Hairline, solid, one step off the surface — recessive, never dashed.
        grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
        rightPriceScale: { borderColor: t.axis },
        timeScale: { borderColor: t.axis, fixLeftEdge: true, fixRightEdge: true },
        crosshair: { mode: CrosshairMode.Normal },
      });

      const series = chart.addSeries(CandlestickSeries, {
        upColor: t.polePos,
        downColor: t.poleNeg,
        wickUpColor: t.polePos,
        wickDownColor: t.poleNeg,
        borderVisible: false,
      });

      // lightweight-charts throws if data is unsorted or has duplicate times,
      // and the API makes no promise about bar order. Sorting and de-duping
      // here is data hygiene, not computation: no value is altered.
      const seen = new Set<string>();
      const data: CandlestickData<Time>[] = bars
        .filter(
          (b): b is Bar & { d: string; o: number; h: number; l: number; c: number } =>
            isStr(b.d) && isNum(b.o) && isNum(b.h) && isNum(b.l) && isNum(b.c),
        )
        .sort((a, b) => a.d.localeCompare(b.d))
        .filter((b) => (seen.has(b.d) ? false : (seen.add(b.d), true)))
        .map((b) => ({ time: b.d as Time, open: b.o, high: b.h, low: b.l, close: b.c }));

      series.setData(data);
      chart.timeScale().fitContent();

      const last = data[data.length - 1];
      if (last) {
        setReadout({
          d: String(last.time),
          o: last.open,
          h: last.high,
          l: last.low,
          c: last.close,
        });
      }

      chart.subscribeCrosshairMove((param) => {
        const point = param.seriesData.get(series) as CandlestickData<Time> | undefined;
        if (!point || param.time === undefined) {
          // Pointer left the plot: fall back to the last bar rather than
          // blanking the row, so the header never flickers empty.
          if (last) setReadout({ d: String(last.time), o: last.open, h: last.high, l: last.low, c: last.close });
          return;
        }
        setReadout({
          d: String(param.time),
          o: point.open,
          h: point.high,
          l: point.low,
          c: point.close,
        });
      });
    } catch (e) {
      console.error('[trading-agent/ui] candle chart failed', e);
    }

    return () => {
      chart?.remove();
    };
  }, [bars, mode]);

  const usable = bars.filter((b) => isStr(b.d) && isNum(b.c)).length;

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
        <span className="font-semibold text-ink">{symbol}</span>
        {readout ? (
          <>
            <span className="text-muted">{readout.d}</span>
            <span className="tnum text-ink2">
              O {price(readout.o)} · H {price(readout.h)} · L {price(readout.l)} ·{' '}
              <span className="font-semibold text-ink">C {price(readout.c)}</span>
            </span>
          </>
        ) : (
          <span className="text-muted">no bars</span>
        )}
        <span className="ml-auto text-muted">{count(usable)} sessions</span>
      </div>
      <div ref={host} style={{ height: HEIGHT }} className="w-full overflow-hidden rounded border border-line" />
      {usable === 0 && <p className="mt-2 text-sm text-muted">The API returned no usable bars for this symbol.</p>}
    </div>
  );
}
