import { useCallback, useEffect, useState } from 'react';

export type Mode = 'dark' | 'light';

const KEY = 'ta-theme';

function read(): Mode {
  try {
    const v = localStorage.getItem(KEY);
    if (v === 'light' || v === 'dark') return v;
  } catch {
    /* storage blocked — fall through to the light default */
  }
  return 'light';
}

function apply(mode: Mode) {
  document.documentElement.classList.toggle('dark', mode === 'dark');
  document.documentElement.style.colorScheme = mode;
}

/**
 * LIGHT by default (the index.html pre-paint script stamps it); dark is a
 * selected variant behind the toggle. The class lives on <html> because that is
 * what Tailwind's darkMode:'class' looks for, and because the canvas charts
 * read their colours off the same element via getComputedStyle.
 */
export function useTheme(): { mode: Mode; setMode: (m: Mode) => void; toggle: () => void } {
  const [mode, setModeState] = useState<Mode>(read);

  useEffect(() => {
    apply(mode);
    try {
      localStorage.setItem(KEY, mode);
    } catch {
      /* storage blocked — the in-memory state still drives this session */
    }
  }, [mode]);

  const setMode = useCallback((m: Mode) => setModeState(m), []);
  const toggle = useCallback(() => setModeState((m) => (m === 'dark' ? 'light' : 'dark')), []);

  return { mode, setMode, toggle };
}

export type Tokens = {
  surface: string;
  sunken: string;
  ink: string;
  ink2: string;
  muted: string;
  grid: string;
  axis: string;
  polePos: string;
  poleNeg: string;
  poleMid: string;
  series1: string;
  good: string;
  warning: string;
  serious: string;
  critical: string;
};

const FALLBACK: Record<Mode, Tokens> = {
  dark: {
    surface: '#111d2b',
    sunken: '#0d1824',
    ink: '#eaf1f8',
    ink2: '#b4c4d4',
    muted: '#8396a8',
    grid: '#1a2838',
    axis: '#32465d',
    polePos: '#3987e5',
    poleNeg: '#e66767',
    poleMid: '#223246',
    series1: '#3987e5',
    good: '#0ca30c',
    warning: '#fab219',
    serious: '#ec835a',
    critical: '#d03b3b',
  },
  light: {
    surface: '#ffffff',
    sunken: '#f0f6fc',
    ink: '#001c36',
    ink2: '#465d75',
    muted: '#63778c',
    grid: '#e5eef7',
    axis: '#c8d5e3',
    polePos: '#2a78d6',
    poleNeg: '#e34948',
    poleMid: '#e5eef7',
    series1: '#2a78d6',
    good: '#0ca30c',
    warning: '#fab219',
    serious: '#ec835a',
    critical: '#d03b3b',
  },
};

const VAR_OF: Record<keyof Tokens, string> = {
  surface: '--surface-1',
  sunken: '--surface-2',
  ink: '--ink',
  ink2: '--ink-2',
  muted: '--ink-muted',
  grid: '--grid',
  axis: '--axis',
  polePos: '--pole-pos',
  poleNeg: '--pole-neg',
  poleMid: '--pole-mid',
  series1: '--series-1',
  good: '--status-good',
  warning: '--status-warning',
  serious: '--status-serious',
  critical: '--status-critical',
};

/**
 * The same tokens index.css declares, as literal CSS colour strings.
 *
 * lightweight-charts paints to a canvas and the d3 interpolators do colour
 * maths; neither can resolve `var(--x)`, so the triples are read off the live
 * stylesheet and wrapped into `rgb(r g b)` here. Reading them rather than
 * duplicating them keeps one source of truth; FALLBACK covers the brief window
 * where a chart mounts before the stylesheet has applied and
 * getComputedStyle returns empty strings.
 */
export function tokens(mode: Mode): Tokens {
  const out = { ...FALLBACK[mode] };
  if (typeof window === 'undefined') return out;
  const cs = getComputedStyle(document.documentElement);
  (Object.keys(VAR_OF) as (keyof Tokens)[]).forEach((k) => {
    const v = cs.getPropertyValue(VAR_OF[k]).trim();
    // "42 120 214" -> "rgb(42, 120, 214)". The COMMAS are mandatory: d3-color
    // does not implement the CSS Color 4 space-separated form, so `color("rgb(42
    // 120 214)")` returns null and every interpolated treemap cell comes out
    // black — silently, with no error. Anything that is not a bare triple is
    // passed through untouched, so a token later written as hex still works.
    const triple = /^(\d+)\s+(\d+)\s+(\d+)$/.exec(v);
    if (triple) out[k] = `rgb(${triple[1]}, ${triple[2]}, ${triple[3]})`;
    else if (v) out[k] = v;
  });
  return out;
}

/**
 * WCAG relative luminance from any of the forms this app produces: a hex
 * token, an `rgb(r g b)` token, or the `rgb(r, g, b)` that d3's Lab
 * interpolator returns.
 */
function luminance(css: string): number {
  const s = css.trim();
  let rgb: [number, number, number] | null = null;

  const hex = /^#?([0-9a-f]{6})$/i.exec(s);
  if (hex?.[1]) {
    const int = parseInt(hex[1], 16);
    rgb = [(int >> 16) & 255, (int >> 8) & 255, int & 255];
  } else {
    const nums = s.match(/\d+(\.\d+)?/g);
    if (nums && nums.length >= 3) {
      rgb = [Number(nums[0]), Number(nums[1]), Number(nums[2])];
    }
  }
  if (!rgb) return 0;

  const ch = rgb.map((c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * (ch[0] ?? 0) + 0.7152 * (ch[1] ?? 0) + 0.0722 * (ch[2] ?? 0);
}

/**
 * Label ink for text sitting INSIDE a coloured fill (treemap cells). White or
 * near-black only — the two the palette allows — chosen by which actually wins
 * on contrast against that specific fill, because the diverging ramp runs from
 * a near-surface neutral to a saturated pole and the answer flips partway.
 */
export function inkOn(fill: string): string {
  const l = luminance(fill);
  const onWhite = 1.05 / (l + 0.05);
  // 0.0107 is the measured relative luminance of the deep navy ink (#001c36),
  // computed by the same function above rather than guessed.
  const onBlack = (l + 0.05) / (0.0107 + 0.05);
  return onWhite >= onBlack ? '#ffffff' : '#001c36';
}
