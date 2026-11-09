# Trading Agent — dashboard

Vite + React 18 + TypeScript + Tailwind, with `lightweight-charts` for candles and
`d3` for the industry treemap. No Next.js, no component library — the components are
hand-rolled in `src/components`.

## Run it

```bash
cd apps/ui
npm install
npm run dev        # http://127.0.0.1:5173
```

The dev server binds **127.0.0.1:5173** and proxies `/api` → `http://127.0.0.1:8000`
(`server.proxy` in `vite.config.ts`). Start the FastAPI process separately; the UI
degrades to a visible "API unreachable" screen when it is not up, and recovers on
**Refresh** without a reload.

```bash
npm run build      # tsc --noEmit && vite build  ->  apps/ui/dist
npm run typecheck  # types only
npm run preview    # serve the built bundle
```

`npm run build` writes to **`apps/ui/dist`**, which FastAPI serves as StaticFiles.
Fetch URLs are relative (`/api/...`), so the same bundle works behind the Vite proxy
in dev and behind FastAPI in production — there is no origin to configure and no CORS.

## Views

| Route | What it shows |
|---|---|
| `#/today` | Market-context strip, recommendations as trade-plan cards, rejections drawer (collapsed) |
| `#/heatmap` | d3 treemap — area = turnover, colour = `ret_1d` — plus the industry table |
| `#/screener` | Sortable candidate table |
| `#/portfolio` | Equity / cash / heat / drawdown tiles and the positions table |
| `#/risk` | Rule table with state chips and headroom meters, plus industry diversification |
| `#/analysis` | Per-symbol candlesticks, trend states, relative strength |
| `#/honesty` | Calibration and simulation sufficiency panels |

The route lives in the URL hash, so a reload or a bookmark lands on the same view.

## The contract it expects

Every endpoint under `/api`, exactly as typed in `src/api/types.ts`:

`/health` · `/context` · `/heatmap` · `/screener?limit=N` · `/recommendations` ·
`/rejections` · `/portfolio` · `/risk` · `/industries` · `/trend/{symbol}` ·
`/calibration` · `/simulation`

### Units — read this before changing a formatter

The contract does not state units. Verified against the running backend:

| Shape | Convention | Helper |
|---|---|---|
| `ret_1d`, `ret_5d`, `ret_21d` | fraction of one (`-0.0048`) | `pctFrac` |
| **every `*_pct` field** | **fraction of one, NOT percent** | `pctFrac` |
| `p_up`, `ci_lo`, `ci_hi`, `p_lo`, `p_hi` | probability 0..1 | `pctFrac` |
| risk `value`/`limit` where `unit` starts with `pct` | fraction of one (`limit: 0.03` = 3%) | `pctFrac` |
| risk `value`/`limit` where `unit` starts with `count` | whole number | `count` |
| `turnover_cr`, `adtv_cr` | ₹ crore | `crore` |
| `notional`, `risk_rupees`, `equity`, `cash`, prices | ₹ | `inr` / `price` |
| `cost_bps` | basis points | `bps` |
| `rs_63`, `composite`, `er_63`, `adx14`, `slope_t_63`, `expectancy_atr`, `rr` | unitless, printed as sent | `num` |

The `_pct` suffix is a trap: the backend sends `pct_above_sma50: 0.235` and
`headroom_pct: 1.0`. Reading those as percentages prints `0.24%` for a day when
23.5% of the universe was above its 50-DMA — wrong by two orders of magnitude and
entirely plausible-looking. All money is formatted `en-IN`, so grouping is lakh/crore
(`₹1,00,062`).

### Nullability

Field names and enums in `src/api/types.ts` match the contract exactly, but every
scalar is widened to `| null` and every collection to `| null | undefined`. That is
deliberate: the pipeline degrades, and the live API already returns `benchmark.close:
null` and a screener row with `ret_1d`, `rvol_20` and `adtv_cr` all null. Widening the
types makes TypeScript force a fallback at each use site instead of letting one
`.toFixed()` blank the page. Missing values render as an em-dash (`—`), never as `0`.
An `ErrorBoundary` backstops each view.

## The honesty rules

These are product requirements, not styling. They live in
`src/components/BarrierBlock.tsx` and `src/views/Honesty.tsx`.

- **A bare probability point estimate is never rendered.**
- `barrier.state: "UNAVAILABLE"` renders an em-dash plus the server's own `reason`.
  So does `state: "OK"` with a null interval — there is no usable probability either way.
- `state: "OK"` renders the **interval** (`41–57%`) with `n_eff` beside it.
- When the interval is too wide, the range is **all** that renders; the midpoint is
  withheld and the card says so. The verdict is the server's `barrier.wide`, so the
  threshold lives in one place and cannot drift. The documented 15pp test survives only
  as the fallback for a response that predates the flag — a server that says "wide"
  always wins.
- Where a midpoint is shown at all (tight interval only) it is in strictly smaller
  type than the interval — `text-[11px]` against `text-base`.
- `/api/calibration` with `state: "INSUFFICIENT"` renders the refusal and the
  shortfall ("12 of 30 effective observations"). **No reliability chart is drawn.**
- `/api/simulation` with `status: "INSUFFICIENT"` renders "61 of 250 sessions" and
  **never** `p_lo`/`p_hi`, even when the server sends them.
- Recommendations have a dedicated **Sort by** control, defaulting to the lower bound
  of the target-probability interval. Unavailable estimates sort last; other choices
  include expected value, composite score, reward:risk, potential risk and transaction cost.
  `DataTable` still throws in dev if a column whose key looks like a probability is marked sortable.
- `data_quality` other than `OK` raises a visible banner across every view.

## Colour and accessibility

Two layers, and they are kept apart on purpose.

**The product surface** is a deep navy rail (`#0B2545`) against a soft blue-grey canvas
(`#EEF2F7`) with white cards. Depth comes from the canvas/card step, not from shadows.
The neutrals are one cool slate ladder generated in OKLCH at hue 250 with chroma
tapering 0.004 → 0.064, so every border, label and surface is a relative of the rail
rather than an unrelated grey. **Light is the primary theme**; dark is a selected
variant re-stepped against its own surfaces (`#0a1420` canvas, `#111d2b` card), never an
inversion.

**The data colours** are the validated reference instance from the `dataviz` skill, held
unchanged. Re-run the validator against the real surfaces after touching any value:

```bash
node scripts/validate_palette.js "#2a78d6,#eb6834,#1baf7a" \
  --mode light --surface "#ffffff"            # all checks pass
node scripts/validate_palette.js "#3987e5,#d95926,#199e70" \
  --mode dark  --surface "#111d2b"            # all checks pass
# both also pass under --pairs all
```

The one WARN is light-mode aqua at 2.82:1 as a *fill*; the relief rule is satisfied —
aqua only appears behind a chip that also carries its word and glyph, and aqua **text**
is a separate, darker token.

### Fills and labels are different tokens

A mark needs 3:1 against the surface; small text needs 4.5:1. Where the documented mark
step is too light to set type in there is a same-hue `*-ink` token beside it, re-stepped
until it cleared 4.5:1 and listed with its measured ratio in `src/index.css`
(`--pos-ink`, `--neg-ink`, `--good-ink`, `--s2-ink`, `--s3-ink`, `--accent-ink`,
`--critical-ink`, `--warning-ink`). Giving a hue one token for both jobs is how chips end
up with 3:1 text in them.

### The six action tokens

`BUY WATCH HOLD TRIM SELL AVOID` are separated on three channels, any one of which is
sufficient on its own — see `src/components/action.tsx`:

| channel | what it carries |
|---|---|
| **form** | solid fill = transact today (BUY, SELL). Everything else is an outline. This is the channel that survives greyscale and a one-second glance. |
| **glyph** | ▲ ◇ = ½ ▼ ✕ — distinct shapes, not six triangles. |
| **word** | always spelled out, never an icon alone. |

Hue is the fourth and least load-bearing channel. WATCH is deliberately the quietest
thing on screen: it is the *absence* of an instruction and must not look like one.

### Wayfinding hues

Each nav entry has its own icon-tile hue (`--nav-*`), all checked ≥ 5.2:1 against the
rail. They are identity, not measurement: they never appear on a chart, and each is
accompanied by a glyph, a label and a fixed position, so hue is the third channel there
too.

- **Up is blue, down is red — not green/red.** Green-vs-red is the pair that collapses
  for red-green colour blindness, and this dashboard exists to show which way numbers
  went. Every up/down colour is additionally paired with a glyph (▲ ▼) and the signed value.
- No state is carried by colour alone: every chip ships a glyph and a word.
- Tokens are `rgb(var(--token) / <alpha-value>)` in `tailwind.config.js`. A token
  written plainly as `var(--x)` silently drops Tailwind's opacity modifier, so
  `bg-good/10` would render fully saturated.
- `src/lib/theme.ts` hands charts `rgb(r, g, b)` **with commas** — d3-color does not
  parse the space-separated CSS Color 4 form and returns `null`, which paints every
  interpolated treemap cell black.
- Light by default (a pre-paint script in `index.html` prevents a flash), dark
  supported, toggled via a `dark` class on `<html>` with `darkMode: 'class'`.
- Charts are keyboard-reachable: treemap cells, scatter points and histogram bars are
  focusable and announce their values. Every chart has a table-view twin.

## Explanations come from the server

`GET /api/glossary` is fetched once at app level (`src/lib/glossary.tsx`) and read by
every chip, metric label and tooltip. **This UI writes no definition of its own.** A
tooltip that said something the backend did not could drift away from the rule that
actually fired, and it would read just as authoritative.

- `src/components/Tooltip.tsx` is the explanation layer. The trigger is a real
  `<button>`: in the tab order, opens on focus, answers Enter/Space, Escape closes. A tap
  pins it open rather than dismissing on the synthetic `mouseleave` touch devices fire.
  The panel is portalled to `document.body` and positioned in viewport coordinates,
  because cards clip overflow and tables scroll horizontally.
- `TermInfo` (the ⓘ) exists for one structural reason: a sortable table header is
  already a `<button>`, and a button inside a button is invalid HTML that browsers
  un-nest — breaking the outer control. It sits beside as its own tab stop.
- Reason codes render the glossary's plain wording with the raw code beside them, so a
  non-specialist can read the row and a screenshot can still be grepped against the
  ledger.
- A term the server has not defined gets no tooltip, never an invented one.
- `#/glossary` lists all 47 terms grouped by `group`, with search.

## Advice versus affordability

`/api/recommendations` carries two different size answers and the UI must never conflate
them:

- `qty` / `notional` / `risk_rupees` — what you are **advised** to buy now. Zero on a
  WATCH. On a BUY this leads, at the largest size on the card after the symbol.
- `would_qty` / `would_notional` / `would_risk_rupees` + `afford_note` — **affordability**:
  the most this name could take today given risk appetite, cash and industry headroom.
  Rendered in subordinate type as "You could buy …", with the server's own note beneath.

A WATCH therefore never shows a promoted zero and never shows a figure that could be
mistaken for "buy this many". On Portfolio, quantity means shares actually held and
`would_qty` is not shown at all. "Advised risk" is omitted from a WATCH expansion
entirely — it is structurally ₹0 there and reads as a measurement rather than as an
absent instruction.

## The trend pair

`trend_20/63/252` and `ind_trend_20/63/252` come from the same classifier at the same
lookbacks, so the stock and its industry are directly comparable — and **the comparison
is the information**. They are therefore always rendered as a labelled pair, stock above
industry, never as two unrelated chips: sideways inside a strongly trending industry is
a different trade from sideways inside a decaying one. The 63-session pair is on the
collapsed card and list row; all three lookbacks are in the expansion
(`src/components/trend.tsx`).

Colour there says only what it can defend — clearly up, clearly down, or neither. The
three weak states (`UP_DECAYING`, `RANGE`, `DOWN_DECAYING`) share the neutral ink on
purpose; tinting a decaying uptrend green would assert what the state name withdraws.
`UNDEFINED` gets muted ink and `?`, never the `◇` that means a measured range — it is
"not enough history to say", which is what the glossary entry states.

## Plan levels are tinted

Stop wears the down pole, target wears the up pole, the entry band stays neutral because
it is neither — so the three numbers agree with the stop→entry→target rail drawn above
them. Each carries its ▼/▲, so the polarity is never hue-only.

The levels sit in a grid of `minmax(0, 1fr)` tracks with `min-w-0` items, and the entry
band spans the full row. That is a fix, not a style: a grid item defaults to
`min-width: auto`, so a five-figure entry range (two ₹ amounts plus an en-dash, roughly
twice the width of its siblings) refused to shrink and overran the stop beside it,
printing two prices as one unreadable number.

## Known gaps

- `industries[].headroom` has no unit in the contract and the live endpoint returns
  `[]`, so it is drawn as a fraction only when it falls in `0..1`; otherwise the meter
  is left empty and the raw number is printed.
- `simulation.histogram[].bucket` has no stated unit; the axis says so rather than
  guessing.
- `/api/screener` sends `action`, `adx14`, `rr`, `expectancy_atr` and `composite` beyond
  the original contract. They are now typed and rendered; `composite` is the sort key and
  sits early in the column order so the ranking never scrolls out of view.
- Rule labels are free text, so two Risk rows are matched to a glossary entry by name
  (`Portfolio heat` → `portfolio heat`, `Peak-to-trough drawdown` → `drawdown`) in
  `RULE_TERM`. That map only chooses *which* server text to show; a label with no match
  simply has no tooltip.
- `/api/recommendations` occasionally answers `500` under load. The gate handles it:
  a visible strip naming the endpoint, a Retry, and whatever did load stays on screen.
