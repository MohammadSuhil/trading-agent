import { useCallback, useMemo, useState } from 'react';
import { postJson, useApi } from '../api/client';
import type {
  Context,
  Portfolio,
  Recommendations,
  RecommendationRow,
  Rejections,
  RejectionRow,
  Risk,
  RiskRule,
} from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { DataTable, type Column } from '../components/DataTable';
import {
  RecommendationCard,
  SizeBlock,
  CardDetails,
  ConfidencePill,
  entryBand,
  type Density,
} from '../components/RecommendationCard';
import { ActionBadge, ACTION_ORDER, ACTIONS, actionRail, actionSpec, type ActionCode } from '../components/action';
import { RegimeChip, RuleStateChip, TriSourceChip } from '../components/StateChip';
import { Kpi, PillTabs, SectionHead, type Tab } from '../components/ui';
import { TrendPairInline } from '../components/trend';
import { Chip, Delta, Empty, Meter, Panel, SegmentedBar, cn } from '../components/primitives';
import { Term, TermLabel, TermText } from '../components/Tooltip';
import { count, humanEnum, inr, isNum, isStr, pctFrac, price, signedPctFrac, stamp, text } from '../lib/format';

/* ------------------------------------------------------------- KPI row */
type RecommendationSort = 'probability' | 'expectancy' | 'composite' | 'rr' | 'risk' | 'cost';

const SORT_OPTIONS: { value: RecommendationSort; label: string }[] = [
  { value: 'probability', label: 'Target probability (lower bound)' },
  { value: 'expectancy', label: 'Expected value' },
  { value: 'composite', label: 'Composite score' },
  { value: 'rr', label: 'Reward:risk' },
  { value: 'risk', label: 'Potential risk (low first)' },
  { value: 'cost', label: 'Transaction cost (low first)' },
];

function sortValue(r: RecommendationRow, by: RecommendationSort): number | null {
  switch (by) {
    case 'probability': {
      const b = r.barrier;
      return b?.state === 'OK' && isNum(b.ci_lo) && isNum(b.ci_hi) && b.ci_hi >= b.ci_lo
        ? b.ci_lo
        : null;
    }
    case 'expectancy': return isNum(r.expectancy_atr) ? r.expectancy_atr : null;
    case 'composite': return isNum(r.composite) ? r.composite : null;
    case 'rr': return isNum(r.rr) ? r.rr : null;
    case 'risk': {
      const action = actionSpec(r.action)?.code;
      const advising = action === 'BUY' || action === 'TRIM' || action === 'SELL'
        || (isNum(r.qty) && r.qty > 0);
      const risk = advising ? r.risk_rupees : r.would_risk_rupees;
      return isNum(risk) ? risk : null;
    }
    case 'cost': return isNum(r.cost_bps) ? r.cost_bps : null;
  }
}

function sortRecommendations(rows: RecommendationRow[], by: RecommendationSort) {
  const ascending = by === 'risk' || by === 'cost';
  return [...rows].sort((a, b) => {
    const av = sortValue(a, by);
    const bv = sortValue(b, by);
    if (av === null) return bv === null ? 0 : 1;
    if (bv === null) return -1;
    if (av !== bv) return ascending ? av - bv : bv - av;

    const ac = isNum(a.composite) ? a.composite : 0;
    const bc = isNum(b.composite) ? b.composite : 0;
    return bc - ac || text(a.symbol, '').localeCompare(text(b.symbol, ''));
  });
}

/**
 * Six tiles, in the order a reader needs them: what to do, what to track, how
 * much risk is live, what is spendable, how far down we are, what kind of
 * market it is. Every description under a number is the glossary's own `short`
 * for that code — see components/ui.tsx.
 */
function KpiRow({
  counts,
  pf,
  ctx,
}: {
  counts: Map<string, number>;
  pf: Portfolio | null;
  ctx: Context | null;
}) {
  const drawdown = pf?.drawdown_pct;
  const regime = ctx?.regime;

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6">
      <Kpi
        label="To buy"
        term="BUY"
        value={count(counts.get('BUY') ?? 0)}
        unit={(counts.get('BUY') ?? 0) === 1 ? 'name' : 'names'}
        accent={ACTIONS.BUY.tile}
        delta={
          (counts.get('BUY') ?? 0) === 0 ? (
            <span className="text-muted">
              <span aria-hidden="true">–</span> nothing cleared today
            </span>
          ) : null
        }
      />
      <Kpi
        label="Watchlist"
        term="WATCH"
        value={count(counts.get('WATCH') ?? 0)}
        unit={(counts.get('WATCH') ?? 0) === 1 ? 'name' : 'names'}
        accent={ACTIONS.WATCH.tile}
      />
      <Kpi
        label="Portfolio heat"
        term="portfolio heat"
        value={pctFrac(pf?.heat_pct)}
        accent="bg-[rgb(var(--nav-risk))]"
        missing={!isNum(pf?.heat_pct)}
        missingNote="not reported"
        delta={<Meter fraction={pf?.heat_pct} tone="warning" label="Portfolio heat" />}
        drill="#/risk"
      />
      <Kpi
        label="Cash"
        value={inr(pf?.cash)}
        description={
          <>
            of {inr(pf?.equity)} equity · {count(pf?.n_positions)} open
          </>
        }
        accent="bg-[rgb(var(--nav-portfolio))]"
        missing={!isNum(pf?.cash)}
        missingNote="portfolio not reported"
        drill="#/portfolio"
      />
      <Kpi
        label="Drawdown"
        term="drawdown"
        value={signedPctFrac(drawdown)}
        accent="bg-[rgb(var(--nav-calibration))]"
        missing={!isNum(drawdown)}
        missingNote="not reported"
        delta={
          <span className="text-muted">
            <span aria-hidden="true">{isNum(drawdown) && drawdown !== 0 ? '▼' : '–'}</span> from {inr(pf?.watermark)}{' '}
            high-water
          </span>
        }
        drill="#/portfolio"
      />
      <Kpi
        label="Regime"
        term="regime"
        value={<span className="text-[26px]">{isStr(regime) ? humanEnum(regime) : '—'}</span>}
        accent="bg-[rgb(var(--nav-heatmap))]"
        delta={<span className="text-ink2">{text(ctx?.regime_note, '')}</span>}
        drill="#/heatmap"
      />
    </div>
  );
}

/* ------------------------------------------------------------ list row */

/**
 * The list row.
 *
 * Reference anatomy: a status dot, one strong title line, then a line of small
 * colour-coded chips — action, confidence, sleeve — and a chevron. Five
 * answers, flat: what · what to do · the plan · confidence · why (behind the
 * chevron). 25 cards is four screens of scrolling; 25 rows is one.
 */
function ListRow({
  r,
  open,
  onToggle,
  isOwned = false,
  onPaperBuy,
  buying = false,
}: {
  r: RecommendationRow;
  open: boolean;
  onToggle: (next: boolean) => void;
  isOwned?: boolean;
  onPaperBuy?: (r: RecommendationRow) => void;
  buying?: boolean;
}) {
  const canBuy = ((isNum(r.qty) && r.qty > 0) || (isNum(r.would_qty) && r.would_qty > 0)) && !isOwned;

  return (
    <li className={cn('relative border-b border-line last:border-0', open && 'bg-sunken/60')}>
      <div
        aria-hidden="true"
        className={cn(
          'absolute inset-y-0 left-0 w-[3px]',
          isOwned && actionSpec(r.action)?.code === 'BUY' ? 'bg-good' : actionRail(r.action),
        )}
      />
      {/* A grid, not a wrapping flex row: the four columns have to line up down
          the whole list, otherwise 25 rows read as 25 different layouts. */}
      <div className="grid grid-cols-1 items-center gap-x-5 gap-y-2 py-3 pl-5 pr-4 transition-colors hover:bg-sunken/70 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1.25fr)_minmax(0,1fr)_auto]">
        <div className="min-w-0">
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="shrink-0 text-[14px] font-semibold leading-tight tracking-[-0.01em] text-ink">
              {text(r.symbol)}
            </span>
            <span className="truncate text-[12px] leading-tight text-ink2">{text(r.name, '')}</span>
            <span className="hidden truncate text-[11px] leading-tight text-muted xl:inline">
              {text(r.industry, '')}
            </span>
          </div>
          <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <ActionBadge value={r.action} size="sm" />
            <ConfidencePill r={r} compact />
            {/* The stock's trend against its industry's, at the primary
                horizon. The contrast between the two is the signal. */}
            <TrendPairInline stock={r.trend_63} industry={r.ind_trend_63} />
          </div>
        </div>

        {/* Two deliberate lines rather than a wrap: the band is the decision,
            the two levels are its bounds. The levels are tinted to match the
            rail — stop in the down pole, target in the up pole — each with its
            glyph, so the polarity is never hue-only. */}
        <div className="tnum min-w-0 text-[12px] text-ink2">
          <div className="flex min-w-0 items-baseline gap-1">
            <TermLabel code="entry" className="shrink-0 normal-case tracking-normal">
              buy
            </TermLabel>
            <span className="min-w-0 text-[13px] font-semibold text-ink">{entryBand(r)}</span>
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-3">
            <span className="whitespace-nowrap text-neg-ink">
              <TermLabel code="stop" className="mr-1 normal-case tracking-normal">
                stop
              </TermLabel>
              <span aria-hidden="true" className="mr-0.5 text-[0.75em]">
                ▼
              </span>
              {price(r.stop)}
            </span>
            <span className="whitespace-nowrap text-pos-ink">
              <TermLabel code="target" className="mr-1 normal-case tracking-normal">
                target
              </TermLabel>
              <span aria-hidden="true" className="mr-0.5 text-[0.75em]">
                ▲
              </span>
              {price(r.target)}
            </span>
          </div>
        </div>

        <div className="min-w-0">
          <SizeBlock r={r} />
        </div>

        <div className="flex items-center gap-2 justify-self-start lg:justify-self-end">
          {isOwned ? (
            <Chip tone="good" glyph="✓">In portfolio</Chip>
          ) : canBuy ? (
            <button
              type="button"
              disabled={buying}
              onClick={(e) => {
                e.stopPropagation();
                onPaperBuy?.(r);
              }}
              className="rounded-lg border border-accent bg-accent/10 px-2.5 py-1 text-[11.5px] font-semibold text-accent-ink hover:bg-accent/20 disabled:opacity-50"
            >
              {buying ? 'Buying…' : 'Paper buy'}
            </button>
          ) : null}

          <button
            type="button"
            onClick={() => onToggle(!open)}
            aria-expanded={open}
            className="rounded-lg border border-line bg-surface px-2.5 py-1.5 text-[11.5px] font-medium text-ink2 hover:border-line-strong hover:text-ink"
          >
            <span
              aria-hidden="true"
              className={cn('mr-1 inline-block text-[9px] transition-transform', open && 'rotate-90')}
            >
              ▶
            </span>
            Why
          </button>
        </div>
      </div>

      {open && (
        <div className="border-t border-line bg-sunken px-5 py-4">
          <CardDetails r={r} />
        </div>
      )}
    </li>
  );
}

/* --------------------------------------------------------- rejections */

const REJECTION_COLUMNS: Column<RejectionRow>[] = [
  {
    key: 'symbol',
    header: 'Symbol',
    sortable: true,
    value: (r) => r.symbol,
    render: (r) => <span className="font-medium text-ink">{text(r.symbol)}</span>,
  },
  {
    key: 'rule_id',
    header: 'Rule',
    sortable: true,
    value: (r) => r.rule_id,
    render: (r) => (
      <Term code={r.rule_id}>
        <Chip tone="neutral">{text(r.rule_id)}</Chip>
      </Term>
    ),
  },
  {
    key: 'reason',
    header: 'Reason',
    sortable: true,
    value: (r) => r.reason,
    render: (r) => <TermText code={r.reason} />,
  },
  {
    key: 'detail',
    header: 'Detail',
    render: (r) => <span className="text-ink2">{text(r.detail, '')}</span>,
  },
];

/* --------------------------------------------------------- right rail */

/**
 * What is closest to stopping the next trade.
 *
 * Ordering by `headroom_pct` is selection, not arithmetic — the figure, the
 * state and the detail are all the server's. A DISPLAY_ONLY rule keeps its
 * em-dash: it is reported, not enforced, and a meter on it would imply a limit
 * that does not exist.
 */
function RiskRail({ rules, ctx }: { rules: RiskRule[]; ctx: Context | null }) {
  const binding = useMemo(() => {
    const enforced = rules.filter((r) => r.state !== 'DISPLAY_ONLY' && isNum(r.headroom_pct));
    return [...enforced].sort((a, b) => (a.headroom_pct ?? 1) - (b.headroom_pct ?? 1)).slice(0, 4);
  }, [rules]);

  const breaches = rules.filter((r) => r.state === 'BREACH').length;
  const warns = rules.filter((r) => r.state === 'WARN').length;

  return (
    <aside className="panel flex flex-col gap-4 p-4">
      <SectionHead
        title="Closest to binding"
        subtitle={`${count(rules.length)} rules · ${count(breaches)} breach · ${count(warns)} warn`}
        right={
          <a href="#/risk" className="text-[11.5px] font-medium text-accent-ink hover:underline">
            All rules <span aria-hidden="true">→</span>
          </a>
        }
      />

      {binding.length === 0 ? (
        <Empty>No enforced rule reported headroom.</Empty>
      ) : (
        <ul className="space-y-2.5">
          {binding.map((r, i) => (
            <li key={`${text(r.id, 'rule')}-${i}`} className="rounded-lg border border-line bg-sunken px-3 py-2.5">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-[12.5px] font-medium text-ink">
                    <Term code={r.id}>{text(r.label, text(r.id))}</Term>
                  </div>
                  <div className="font-mono text-[10px] text-muted">{text(r.id, '')}</div>
                </div>
                <RuleStateChip value={r.state} />
              </div>
              <div className="mt-2 flex items-center gap-2">
                <div className="flex-1">
                  <Meter
                    fraction={r.headroom_pct}
                    tone={r.state === 'BREACH' ? 'critical' : r.state === 'WARN' ? 'warning' : 'quiet'}
                    label={`Headroom for ${text(r.label, text(r.id))}`}
                  />
                </div>
                <span className="tnum w-14 text-right text-[11px] text-ink2">{pctFrac(r.headroom_pct)}</span>
              </div>
              {r.detail && <p className="mt-1.5 text-[11px] leading-snug text-muted">{r.detail}</p>}
            </li>
          ))}
        </ul>
      )}

      <div className="border-t border-line pt-3">
        <div className="flex items-center gap-2">
          <TermLabel code="breadth">Breadth</TermLabel>
          <span className="text-[11px] text-muted">{text(ctx?.breadth?.universe, '')}</span>
        </div>
        <div className="mt-2">
          <SegmentedBar
            label="Advancers versus decliners"
            segments={[
              {
                name: 'advancers',
                value: isNum(ctx?.breadth?.advancers) ? ctx.breadth.advancers : 0,
                className: 'bg-pos',
              },
              {
                name: 'decliners',
                value: isNum(ctx?.breadth?.decliners) ? ctx.breadth.decliners : 0,
                className: 'bg-neg',
              },
            ]}
          />
        </div>
        <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[12px]">
          <span className="tnum text-pos-ink">
            <span aria-hidden="true">▲</span> {count(ctx?.breadth?.advancers)} adv
          </span>
          <span className="tnum text-neg-ink">
            <span aria-hidden="true">▼</span> {count(ctx?.breadth?.decliners)} dec
          </span>
          <span className="tnum text-ink2">{pctFrac(ctx?.breadth?.pct_above_sma50)} above SMA50</span>
        </div>
      </div>

      <div className="border-t border-line pt-3">
        <div className="label">Benchmark</div>
        <div className="mt-1.5 flex flex-wrap items-baseline gap-2">
          <span className="text-[13px] font-medium text-ink">{text(ctx?.benchmark?.name, 'Benchmark')}</span>
          {isNum(ctx?.benchmark?.close) ? (
            <span className="tnum text-[15px] font-semibold text-ink">{price(ctx?.benchmark?.close)}</span>
          ) : (
            <span className="text-[12px] text-muted">
              <span aria-hidden="true">—</span> no close published for this session
            </span>
          )}
          <Delta value={ctx?.benchmark?.ret_1d} format={(v) => signedPctFrac(v)} className="text-[12px]" />
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-muted">
          <span>
            21d <Delta value={ctx?.benchmark?.ret_21d} format={(v) => signedPctFrac(v)} />
          </span>
          <TriSourceChip value={ctx?.benchmark?.tri_source} />
        </div>
      </div>
    </aside>
  );
}

/* --------------------------------------------------------------- view */

type Layout = 'list' | 'cards';
const REJECTED = '__rejected__';

export function Today({ version, onRefresh }: { version: number; onRefresh?: () => void }) {
  const ctx = useApi<Context>('/api/context', version);
  const recs = useApi<Recommendations>('/api/recommendations', version);
  const rejections = useApi<Rejections>('/api/rejections', version);
  const portfolio = useApi<Portfolio>('/api/portfolio', version);
  const risk = useApi<Risk>('/api/risk', version);

  const [layout, setLayout] = useState<Layout>('list');
  const [density, setDensity] = useState<Density>('comfortable');
  const [sortBy, setSortBy] = useState<RecommendationSort>('probability');
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [buyingSymbols, setBuyingSymbols] = useState<Set<string>>(new Set());

  const ownedSymbols = useMemo(
    () => new Set((portfolio.data?.positions ?? []).map((p) => p.symbol).filter((s): s is string => Boolean(s))),
    [portfolio.data],
  );

  const handlePaperBuy = useCallback(
    async (r: RecommendationRow) => {
      if (!r.symbol) return;
      const qty = r.qty && r.qty > 0 ? r.qty : r.would_qty;
      if (!qty || qty <= 0) return;
      setBuyingSymbols((prev) => new Set(prev).add(r.symbol!));
      try {
        await postJson('/api/paper/buy', { symbol: r.symbol, qty });
        onRefresh?.();
      } catch (err) {
        alert(`Paper buy failed for ${r.symbol}: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        setBuyingSymbols((prev) => {
          const next = new Set(prev);
          next.delete(r.symbol!);
          return next;
        });
      }
    },
    [onRefresh],
  );

  const rows = useMemo(() => recs.data?.rows ?? [], [recs.data]);
  const rejRows = useMemo(() => rejections.data?.rows ?? [], [rejections.data]);

  // Counts over the WHOLE response. The tabs must not renumber themselves when
  // the reader clicks one of them.
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    rows.forEach((r) => {
      const k = actionSpec(r.action)?.code ?? 'unknown';
      m.set(k, (m.get(k) ?? 0) + 1);
    });
    return m;
  }, [rows]);

  // BUY and WATCH are ALWAYS on screen, even at zero. "Actionable (0)" is the
  // single most important thing this page can say on a day like today, and a
  // tab that disappears when it empties cannot say it.
  const present = useMemo<ActionCode[]>(() => {
    const always: ActionCode[] = ['BUY', 'WATCH'];
    return ACTION_ORDER.filter((a) => always.includes(a) || (counts.get(a) ?? 0) > 0);
  }, [counts]);

  const tabs = useMemo<Tab[]>(() => {
    const list: Tab[] = present.map((code) => ({
      id: code,
      label: code === 'BUY' ? 'Actionable' : code.charAt(0) + code.slice(1).toLowerCase(),
      count: counts.get(code) ?? 0,
      activeClass: ACTIONS[code].tab,
      dotClass: ACTIONS[code].tile,
      glyph: ACTIONS[code].glyph,
      title: `${code} — ${counts.get(code) ?? 0} of ${rows.length}`,
    }));
    if ((counts.get('unknown') ?? 0) > 0) {
      list.push({ id: 'unknown', label: 'No action', count: counts.get('unknown') ?? 0, dotClass: 'bg-line-strong' });
    }
    list.push({
      id: REJECTED,
      label: 'Rejected',
      count: rejRows.length,
      activeClass: 'bg-rail-active text-white',
      dotClass: 'bg-line-strong',
      glyph: '✕',
      title: 'Candidates a rule threw out this session',
    });
    return list;
  }, [present, counts, rows.length, rejRows.length]);

  // null = "the reader has not chosen yet", which lands on the first group that
  // actually has rows so the page is not empty on arrival. Once they DO choose,
  // the choice sticks even when it is empty — clicking "Actionable (0)" and
  // being bounced to Watch would hide the answer they asked for.
  const [tab, setTab] = useState<string | null>(null);
  const firstWithRows = tabs.find((t) => (t.count ?? 0) > 0)?.id ?? 'BUY';
  const activeTab = tab && tabs.some((t) => t.id === tab) ? tab : firstWithRows;

  const shown = useMemo(() => {
    if (activeTab === REJECTED || activeTab === 'unknown') {
      return activeTab === 'unknown' ? rows.filter((r) => !actionSpec(r.action)) : [];
    }
    return rows.filter((r) => actionSpec(r.action)?.code === activeTab);
  }, [rows, activeTab]);
  const sortedShown = useMemo(() => sortRecommendations(shown, sortBy), [shown, sortBy]);

  // One stable key per row, computed once off the full response: it has to
  // survive tab changes, otherwise every expanded row would close on a click.
  const keys = useMemo(() => {
    const m = new Map<RecommendationRow, string>();
    rows.forEach((r, i) => m.set(r, text(r.id, '') || `${text(r.symbol, 'row')}-${i}`));
    return m;
  }, [rows]);
  const keyOf = useCallback((r: RecommendationRow) => keys.get(r) ?? '', [keys]);

  const toggleOne = useCallback((key: string, next: boolean) => {
    setExpanded((prev) => {
      const s = new Set(prev);
      if (next) s.add(key);
      else s.delete(key);
      return s;
    });
  }, []);

  const allKeys = useMemo(() => sortedShown.map((r) => keyOf(r)), [sortedShown, keyOf]);
  const allOpen = allKeys.length > 0 && allKeys.every((k) => expanded.has(k));

  const tabLabel = tabs.find((t) => t.id === activeTab)?.label ?? 'Recommendations';

  return (
    <ApiGate states={[ctx, recs]}>
      <div className="space-y-5">
        <KpiRow counts={counts} pf={portfolio.data} ctx={ctx.data} />

        <PillTabs tabs={tabs} value={activeTab} onChange={setTab} label="Recommendation groups" />

        <div className="grid gap-5 2xl:grid-cols-[minmax(0,1fr)_22rem]">
          <section className="panel min-w-0 overflow-hidden">
            <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3 border-b border-line px-4 py-3.5">
              <SectionHead
                title={
                  <>
                    {tabLabel}
                    <span className="ml-2 font-normal text-muted">
                      {activeTab === REJECTED ? count(rejRows.length) : count(shown.length)}
                    </span>
                  </>
                }
                subtitle={
                  <>
                    as of {stamp(recs.data?.as_of)} · sorted by{' '}
                    {SORT_OPTIONS.find((option) => option.value === sortBy)?.label.toLowerCase()}
                  </>
                }
              />
              {activeTab !== REJECTED && shown.length > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                  <label className="flex items-center gap-1.5 text-xs text-muted">
                    <span>Sort by</span>
                    <select
                      aria-label="Sort recommendations by"
                      value={sortBy}
                      onChange={(e) => setSortBy(e.target.value as RecommendationSort)}
                      className="rounded-lg border border-line bg-surface px-2 py-1.5 text-xs text-ink"
                    >
                      {SORT_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  </label>
                  <div
                    role="group"
                    aria-label="Layout"
                    className="inline-flex rounded-lg border border-line bg-sunken p-0.5"
                  >
                    {(
                      [
                        { id: 'list', label: 'List', glyph: '≡' },
                        { id: 'cards', label: 'Cards', glyph: '▦' },
                      ] as const
                    ).map((o) => (
                      <button
                        key={o.id}
                        type="button"
                        onClick={() => setLayout(o.id)}
                        aria-pressed={layout === o.id}
                        className={cn(
                          'rounded-[6px] px-2.5 py-1 text-[11.5px] font-medium transition-colors',
                          layout === o.id ? 'bg-surface text-ink shadow-card' : 'text-ink2 hover:text-ink',
                        )}
                      >
                        <span aria-hidden="true" className="mr-1 text-[10px]">
                          {o.glyph}
                        </span>
                        {o.label}
                      </button>
                    ))}
                  </div>
                  {layout === 'cards' && (
                    <div
                      role="group"
                      aria-label="Density"
                      className="inline-flex rounded-lg border border-line bg-sunken p-0.5"
                    >
                      {(
                        [
                          { id: 'comfortable', label: 'Comfortable' },
                          { id: 'compact', label: 'Compact' },
                        ] as const
                      ).map((o) => (
                        <button
                          key={o.id}
                          type="button"
                          onClick={() => setDensity(o.id)}
                          aria-pressed={density === o.id}
                          className={cn(
                            'rounded-[6px] px-2.5 py-1 text-[11.5px] font-medium transition-colors',
                            density === o.id ? 'bg-surface text-ink shadow-card' : 'text-ink2 hover:text-ink',
                          )}
                        >
                          {o.label}
                        </button>
                      ))}
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => setExpanded(allOpen ? new Set() : new Set(allKeys))}
                    className="rounded-lg border border-line bg-surface px-2.5 py-1.5 text-[11.5px] font-medium text-ink2 hover:border-line-strong hover:text-ink"
                  >
                    {allOpen ? 'Collapse all' : 'Expand all'}
                  </button>
                </div>
              )}
            </div>

            {activeTab === REJECTED ? (
              rejections.error && !rejections.data ? (
                <p className="p-4 text-sm text-muted">Could not load rejections — {rejections.error.detail}</p>
              ) : (
                <DataTable
                  rows={rejRows}
                  columns={REJECTION_COLUMNS}
                  rowKey={(r, i) => `${text(r.symbol, 'x')}-${text(r.rule_id, 'r')}-${i}`}
                  dense
                  caption="Candidates rejected this session, with the rule that rejected them"
                  empty="Nothing was rejected this session."
                />
              )
            ) : shown.length === 0 ? (
              <Empty>
                {rows.length === 0
                  ? 'No recommendations for this session. That is a valid outcome — the Rejected tab says why.'
                  : `Nothing in this group for this session.`}
              </Empty>
            ) : layout === 'list' ? (
              <ul>
                {sortedShown.map((r) => {
                  const k = keyOf(r);
                  const sym = r.symbol ?? '';
                  return (
                    <ListRow
                      key={k}
                      r={r}
                      open={expanded.has(k)}
                      onToggle={(next) => toggleOne(k, next)}
                      isOwned={sym ? ownedSymbols.has(sym) : false}
                      onPaperBuy={handlePaperBuy}
                      buying={sym ? buyingSymbols.has(sym) : false}
                    />
                  );
                })}
              </ul>
            ) : (
              <div className="grid grid-cols-1 gap-4 p-4 2xl:grid-cols-2">
                {sortedShown.map((r) => {
                  const k = keyOf(r);
                  const sym = r.symbol ?? '';
                  return (
                    <RecommendationCard
                      key={k}
                      r={r}
                      density={density}
                      open={expanded.has(k)}
                      onToggle={(next) => toggleOne(k, next)}
                      isOwned={sym ? ownedSymbols.has(sym) : false}
                      onPaperBuy={handlePaperBuy}
                      buying={sym ? buyingSymbols.has(sym) : false}
                    />
                  );
                })}
              </div>
            )}
          </section>

          <RiskRail rules={risk.data?.rules ?? []} ctx={ctx.data} />
        </div>

        {ctx.data?.data_quality && ctx.data.data_quality !== 'OK' && (
          <Panel>
            <div className="flex items-center gap-2 text-sm">
              <RegimeChip value={ctx.data?.regime} />
              <Chip tone="warning" glyph="!">
                data {text(ctx.data.data_quality)}
              </Chip>
            </div>
          </Panel>
        )}
      </div>
    </ApiGate>
  );
}
