/**
 * Formatting only. Nothing in this file computes a quantity — every number on
 * screen is the number the API sent, re-spelled. That is a product rule, not a
 * style preference: if the UI ever derives a figure, the screen and the ledger
 * can disagree and the audit trail is worthless.
 */

/** Rendered wherever the API had nothing to say. Never "0", never "N/A". */
export const EM_DASH = '—'; // —  : "no value"
export const EN_DASH = '–'; // –  : ranges ("41–57%")

export function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

export function isStr(v: unknown): v is string {
  return typeof v === 'string' && v.length > 0;
}

const fmtCache = new Map<string, Intl.NumberFormat>();
function grouped(locale: string, dp: number): Intl.NumberFormat {
  const key = `${locale}:${dp}`;
  let f = fmtCache.get(key);
  if (!f) {
    f = new Intl.NumberFormat(locale, { minimumFractionDigits: dp, maximumFractionDigits: dp });
    fmtCache.set(key, f);
  }
  return f;
}

/**
 * Indian money. `en-IN` grouping is the lakh/crore one, so 100000 -> "1,00,000"
 * and 12300000 -> "1,23,00,000" come straight from Intl rather than from a
 * hand-rolled regex that gets the first group wrong on negatives.
 *
 * The sign goes before the symbol ("-₹1,200", not "₹-1,200").
 */
export function inr(v: unknown, dp = 0): string {
  if (!isNum(v)) return EM_DASH;
  const sign = v < 0 ? '-' : '';
  return `${sign}₹${grouped('en-IN', dp).format(Math.abs(v))}`;
}

/** A price: rupees, always 2 dp. */
export function price(v: unknown): string {
  return inr(v, 2);
}

/** A ₹-crore field (`turnover_cr`, `adtv_cr`). */
export function crore(v: unknown, dp = 2): string {
  if (!isNum(v)) return EM_DASH;
  return `${inr(v, dp)} cr`;
}

/** A count. */
export function count(v: unknown): string {
  if (!isNum(v)) return EM_DASH;
  return grouped('en-IN', 0).format(v);
}

/** A plain unitless number — scores, ratios, t-stats. */
export function num(v: unknown, dp = 2): string {
  if (!isNum(v)) return EM_DASH;
  return grouped('en-IN', dp).format(v);
}

/**
 * A field that is ALREADY in percent units. 4.2 -> "4.20%".
 *
 * Nothing in the current contract uses this — it is kept because `unit` on a
 * risk rule is an open vocabulary and a future rule may well report "4.2 pct".
 */
export function pctUnit(v: unknown, dp = 2): string {
  if (!isNum(v)) return EM_DASH;
  return `${grouped('en-IN', dp).format(v)}%`;
}

/**
 * A field that is a FRACTION of one. 0.0123 -> "1.23%".
 *
 * This is the one every percentage-ish field in this API wants. The contract
 * states no units, and the `_pct` suffix is a trap: the running backend sends
 * `pct_above_sma50: 0.235`, `risk_pct: 0.0079`, `headroom_pct: 1.0`, and a
 * portfolio-heat limit of `0.03` carrying `unit: "pct"`. Every one of those is
 * a fraction. Reading `_pct` as "already a percentage" would have printed
 * "0.24% above SMA50" for a day when 23.5% of the universe was above it —
 * wrong by two orders of magnitude and perfectly plausible-looking.
 */
export function pctFrac(v: unknown, dp = 2): string {
  if (!isNum(v)) return EM_DASH;
  return `${grouped('en-IN', dp).format(v * 100)}%`;
}

/** Signed fraction-as-percent, for returns and P&L. "+1.23%" / "-0.40%". */
export function signedPctFrac(v: unknown, dp = 2): string {
  if (!isNum(v)) return EM_DASH;
  const s = pctFrac(Math.abs(v), dp);
  return `${v > 0 ? '+' : v < 0 ? '-' : ''}${s}`;
}

/** Signed already-percent. */
export function signedPctUnit(v: unknown, dp = 2): string {
  if (!isNum(v)) return EM_DASH;
  const s = pctUnit(Math.abs(v), dp);
  return `${v > 0 ? '+' : v < 0 ? '-' : ''}${s}`;
}

/** Signed rupees, for P&L columns. */
export function signedInr(v: unknown, dp = 0): string {
  if (!isNum(v)) return EM_DASH;
  return `${v > 0 ? '+' : v < 0 ? '-' : ''}${inr(Math.abs(v), dp)}`;
}

/** Basis points, as sent. */
export function bps(v: unknown, dp = 1): string {
  if (!isNum(v)) return EM_DASH;
  return `${grouped('en-IN', dp).format(v)} bps`;
}

/**
 * The glyph that carries direction when colour cannot — CVD, greyscale print,
 * forced-colors. Every coloured up/down number in the app is paired with this.
 */
export function dirGlyph(v: unknown): string {
  if (!isNum(v) || v === 0) return '–'; // – : flat / unknown
  return v > 0 ? '▲' : '▼'; // ▲ / ▼
}

/** 'up' | 'down' | 'flat' — drives the colour token, paired with dirGlyph. */
export function dirOf(v: unknown): 'up' | 'down' | 'flat' {
  if (!isNum(v) || v === 0) return 'flat';
  return v > 0 ? 'up' : 'down';
}

/**
 * An `as_of` / `opened` stamp. The contract says `string` without a shape, so
 * this tries ISO and falls back to echoing the raw string — a date we cannot
 * parse is still information, and silently printing "Invalid Date" is worse
 * than printing whatever the server actually said.
 */
export function stamp(v: unknown): string {
  if (!isStr(v)) return EM_DASH;
  if (v.startsWith('1970-01-01') || v.startsWith('1970-01-02')) return EM_DASH;
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  const datePart = d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
  // Date-only strings ('2026-10-31') parse to midnight UTC; printing "00:00"
  // for them would invent a precision the server never claimed.
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(v.trim());
  if (dateOnly) return datePart;
  const timePart = d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
  return `${datePart}, ${timePart}`;
}

/** A free-text enum from the server, shown readably: TREND_UP -> "Trend up". */
export function humanEnum(v: unknown): string {
  if (!isStr(v)) return EM_DASH;
  const s = v.replace(/_/g, ' ').toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** A string that might be missing. */
export function text(v: unknown, fallback = EM_DASH): string {
  return isStr(v) ? v : fallback;
}
