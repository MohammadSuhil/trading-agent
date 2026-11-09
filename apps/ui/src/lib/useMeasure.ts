import { useCallback, useLayoutEffect, useRef, useState } from 'react';

/**
 * Size of a container: measured synchronously on mount, then tracked with
 * ResizeObserver.
 *
 * The synchronous first read is not an optimisation, it is the fix for a real
 * blank chart. Waiting for ResizeObserver's first callback means the SVG is
 * skipped on the first commit and only appears a frame later — and in at least
 * one environment (headless Chrome driving a screenshot) that callback never
 * arrived at all, so the treemap rendered as an empty 460px hole with no error
 * anywhere. getBoundingClientRect in a layout effect is available before paint,
 * so the chart is correct on its very first render; the observer then handles
 * everything after (sidebar, window resize, 1440 vs 1920).
 */
export function useMeasure<T extends HTMLElement>(): [React.RefObject<T>, { width: number; height: number }] {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  // Integer sizes only: sub-pixel churn from a flex parent would otherwise
  // re-run the whole treemap layout on every scrollbar twitch.
  const update = useCallback((width: number, height: number) => {
    setSize((prev) => {
      const w = Math.round(width);
      const h = Math.round(height);
      return prev.width === w && prev.height === h ? prev : { width: w, height: h };
    });
  }, []);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;

    const box = el.getBoundingClientRect();
    update(box.width, box.height);

    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (!e) return;
      update(e.contentRect.width, e.contentRect.height);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [update]);

  return [ref, size];
}

const measurers = new Map<string, (s: string) => number>();

/**
 * Real text width for a given CSS font, via an offscreen canvas.
 *
 * Used to decide whether a treemap label actually fits its cell. Guessing with
 * `length * 6` is how labels end up clipped or overflowing their mark; the rule
 * is to measure, and this is the cheap way to measure.
 */
export function textWidth(font: string): (s: string) => number {
  let fn = measurers.get(font);
  if (!fn) {
    const ctx = typeof document !== 'undefined' ? document.createElement('canvas').getContext('2d') : null;
    if (ctx) {
      ctx.font = font;
      fn = (s: string) => ctx.measureText(s).width;
    } else {
      fn = (s: string) => s.length * 6;
    }
    measurers.set(font, fn);
  }
  return fn;
}
