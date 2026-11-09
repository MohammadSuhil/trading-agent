import { useApi } from '../api/client';
import type { Industries, IndustryRow, Risk, RiskRule } from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { DataTable, type Column } from '../components/DataTable';
import { RuleStateChip } from '../components/StateChip';
import { Term } from '../components/Tooltip';
import { Kpi } from '../components/ui';
import { Meter, Panel } from '../components/primitives';
import { EM_DASH, count, inr, isNum, num, pctFrac, stamp, text } from '../lib/format';

/**
 * Meter colour for a rule. The state comes from the API; this only paints it.
 *
 * OK is deliberately NOT green. Colour on this page marks the exceptions, and
 * a table where every row is a saturated green bar has spent its whole colour
 * budget telling you that nothing happened — the ✓ OK chip already says that,
 * in a word, with a glyph.
 */
function toneOf(state: unknown): 'quiet' | 'warning' | 'critical' | 'neutral' {
  const s = typeof state === 'string' ? state.toUpperCase() : '';
  if (s === 'OK') return 'quiet';
  if (s === 'WARN') return 'warning';
  if (s === 'BREACH') return 'critical';
  return 'neutral';
}

/**
 * Value against limit.
 *
 * `unit` is an open vocabulary — the running backend sends `pct`, `pct_floor`,
 * `count`, `count_min` and `rho`. The only interpretation made here is that a
 * unit in the `pct*` family means the number is a FRACTION of one, which is
 * what the live data shows (portfolio heat `0.0` against a limit of `0.03`).
 * Printing that raw as "0.03 pct" would read as three hundredths of a percent;
 * printing it as "3.00%" is the same number spelled correctly. Any unit this
 * does not recognise is printed verbatim with its unit appended, never
 * rescaled on a guess.
 */
function isPctUnit(unit: string): boolean {
  return unit.toLowerCase().startsWith('pct');
}

function valueCell(r: RiskRule) {
  const unit = text(r.unit, '');
  const asPct = isPctUnit(unit);
  // `count` / `count_min` are whole things — positions, industries, losing
  // streaks. "7.00 count" invites the question of what seven hundredths of a
  // position would be.
  const asCount = unit.toLowerCase().startsWith('count');
  const fmt = (v: unknown) => (asPct ? pctFrac(v) : asCount ? count(v) : num(v));
  const suffix = asPct ? '' : unit;

  const hasValue = isNum(r.value);
  const v = hasValue ? fmt(r.value) : EM_DASH;
  const lim = isNum(r.limit) ? fmt(r.limit) : null;

  return (
    <span className="tnum">
      <span className={hasValue ? 'font-medium text-ink' : 'font-medium text-muted'}>{v}</span>
      {/* Unit only where there is a number to carry it — "— beta" reads as a
          beta that is blank rather than as no beta at all. */}
      {suffix && hasValue && <span className="ml-1 text-xs text-muted">{suffix}</span>}
      {lim !== null && (
        <span className="ml-1 text-xs text-muted">
          / {lim}
          {suffix ? ` ${suffix}` : ''}
        </span>
      )}
      {lim === null && r.state === 'DISPLAY_ONLY' && <span className="ml-1 text-xs text-muted">(no limit)</span>}
      {asPct && <span className="ml-1 text-[10px] text-muted">({unit})</span>}
    </span>
  );
}

/**
 * Rule label → glossary code.
 *
 * The glossary files two of these rules under the metric they measure rather
 * than under the rule's own name. The map only chooses WHICH server text to
 * show; it never supplies any. A label with no match simply has no tooltip.
 */
const RULE_TERM: Record<string, string> = {
  'portfolio heat': 'portfolio heat',
  'peak-to-trough drawdown': 'drawdown',
};

function ruleTerm(label: unknown): string | undefined {
  if (typeof label !== 'string') return undefined;
  return RULE_TERM[label.trim().toLowerCase()];
}

const RULE_COLUMNS: Column<RiskRule>[] = [
  {
    key: 'label',
    header: 'Rule',
    sortable: true,
    value: (r) => r.label ?? r.id,
    render: (r) => {
      const term = ruleTerm(r.label);
      const name = text(r.label, text(r.id));
      return (
        <div className="min-w-0">
          <div className="font-medium text-ink">
            {term ? <Term code={term}>{name}</Term> : name}
          </div>
          {r.id && <div className="font-mono text-[10.5px] text-muted">{r.id}</div>}
        </div>
      );
    },
  },
  {
    key: 'state',
    header: 'State',
    sortable: true,
    value: (r) => r.state,
    render: (r) => <RuleStateChip value={r.state} />,
  },
  { key: 'value', header: 'Value / limit', align: 'right', sortable: true, value: (r) => r.value, render: valueCell },
  {
    key: 'headroom_pct',
    header: 'Headroom',
    sortable: true,
    value: (r) => r.headroom_pct,
    className: 'w-56',
    render: (r) => (
      <div className="flex items-center gap-2">
        <div className="w-32">
          <Meter fraction={r.headroom_pct} tone={toneOf(r.state)} label={`Headroom for ${text(r.label, text(r.id))}`} />
        </div>
        <span className="tnum w-16 text-right text-xs text-ink2">{pctFrac(r.headroom_pct)}</span>
      </div>
    ),
  },
  {
    key: 'detail',
    header: 'Detail',
    render: (r) => <span className="text-ink2">{text(r.detail, '')}</span>,
  },
];

const INDUSTRY_COLUMNS: Column<IndustryRow>[] = [
  {
    key: 'industry',
    header: 'Industry',
    sortable: true,
    value: (r) => r.industry,
    render: (r) => <span className="font-medium text-ink">{text(r.industry)}</span>,
  },
  {
    key: 'notional',
    header: 'Notional',
    align: 'right',
    sortable: true,
    value: (r) => r.notional,
    render: (r) => inr(r.notional),
  },
  {
    key: 'n_names',
    header: 'Names',
    align: 'right',
    sortable: true,
    value: (r) => r.n_names,
    render: (r) => count(r.n_names),
  },
  {
    key: 'pct_equity',
    header: '% equity',
    align: 'right',
    sortable: true,
    value: (r) => r.pct_equity,
    render: (r) => pctFrac(r.pct_equity),
  },
  {
    key: 'cap_pct',
    header: 'Cap',
    align: 'right',
    sortable: true,
    value: (r) => r.cap_pct,
    render: (r) => pctFrac(r.cap_pct),
  },
  {
    key: 'headroom',
    header: 'Headroom to cap',
    sortable: true,
    value: (r) => r.headroom,
    className: 'w-56',
    render: (r) => {
      // `headroom` is the one field here with no unit in the contract AND no
      // live example to check against (/api/industries returns [] today). Every
      // sibling field in this API is a fraction, so it is drawn as one — but
      // only when it actually lies in 0..1. Outside that range the bar is left
      // empty and the raw number printed, because a meter on a scale we are
      // guessing at is worse than no meter.
      const h = r.headroom;
      const meterable = isNum(h) && h >= 0 && h <= 1;
      return (
        <div className="flex items-center gap-2">
          <div className="w-32">
            {meterable ? (
              <Meter fraction={h} tone="neutral" label={`Headroom for ${text(r.industry)}`} />
            ) : (
              <div className="h-2 w-full rounded-full bg-sunken" />
            )}
          </div>
          <span className="tnum w-16 text-right text-xs text-ink2">{meterable ? pctFrac(h) : num(h)}</span>
        </div>
      );
    },
  },
];

export function RiskView({ version }: { version: number }) {
  const risk = useApi<Risk>('/api/risk', version);
  const industries = useApi<Industries>('/api/industries', version);

  const rules = risk.data?.rules ?? [];
  const inds = industries.data?.rows ?? [];
  const breaches = rules.filter((r) => r.state === 'BREACH').length;
  const warns = rules.filter((r) => r.state === 'WARN').length;

  const ok = rules.filter((r) => r.state === 'OK').length;
  const displayOnly = rules.filter((r) => r.state === 'DISPLAY_ONLY').length;

  return (
    <ApiGate states={[risk, industries]}>
      <div className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Kpi label="Passing" value={count(ok)} unit={`of ${count(rules.length)}`} accent="bg-good" />
          <Kpi
            label="Warning"
            value={count(warns)}
            accent="bg-warning"
            delta={
              warns > 0 ? (
                <span className="text-warning-ink">
                  <span aria-hidden="true">!</span> close to a limit
                </span>
              ) : null
            }
          />
          <Kpi
            label="Breached"
            value={count(breaches)}
            accent="bg-critical"
            delta={
              breaches > 0 ? (
                <span className="text-critical-ink">
                  <span aria-hidden="true">✕</span> a limit is exceeded
                </span>
              ) : null
            }
          />
          <Kpi label="Display only" term="DISPLAY_ONLY" value={count(displayOnly)} accent="bg-line-strong" />
        </div>

        <Panel
          title="Risk rules"
          subtitle={
            <>
              {count(rules.length)} rules · <span aria-hidden="true">✕</span> {count(breaches)} breach ·{' '}
              <span aria-hidden="true">!</span> {count(warns)} warn
            </>
          }
          right={<span className="text-xs text-muted">as of {stamp(risk.data?.as_of)}</span>}
          bodyClassName="p-0"
        >
          <DataTable
            rows={rules}
            columns={RULE_COLUMNS}
            rowKey={(r, i) => `${text(r.id, 'rule')}-${i}`}
            dense
            caption="Risk rules with state, value against limit, and headroom"
            empty="The risk endpoint returned no rules."
          />
        </Panel>

        <Panel
          title="Industry diversification"
          subtitle={`${count(inds.length)} industries · as of ${stamp(industries.data?.as_of)}`}
          bodyClassName="p-0"
        >
          <DataTable
            rows={inds}
            columns={INDUSTRY_COLUMNS}
            rowKey={(r, i) => `${text(r.industry, 'ind')}-${i}`}
            initialSort={{ key: 'pct_equity', dir: 'desc' }}
            dense
            caption="Exposure by industry against the per-industry cap"
            empty="No industry exposure reported."
          />
        </Panel>
      </div>
    </ApiGate>
  );
}
