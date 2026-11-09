import { useApi } from '../api/client';
import type { Calibration, CalibrationBin, Simulation } from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { DataTable, type Column } from '../components/DataTable';
import { SufficiencyChip } from '../components/StateChip';
import { Meter, Panel, StatTile } from '../components/primitives';
import { TermInfo } from '../components/Tooltip';
import { Reliability } from '../charts/Reliability';
import { Histogram } from '../charts/Histogram';
import type { Mode } from '../lib/theme';
import { EM_DASH, EN_DASH, count, isNum, num, pctFrac, text } from '../lib/format';

/**
 * The refusal panel.
 *
 * This is the shape an honest "we don't know yet" takes: the claim that is NOT
 * being made, the shortfall in the server's own numbers, and a bar showing how
 * far off it is. No chart, because a chart of twelve observations reads as
 * evidence of calibration to anyone who glances at it, and a glance is what
 * dashboards get.
 */
function Refusal({
  heading,
  have,
  need,
  unit,
  unitTerm,
  note,
  secondary,
}: {
  heading: string;
  have: unknown;
  need: unknown;
  unit: string;
  /** Glossary code for the unit, so the refusal explains its own yardstick. */
  unitTerm?: string;
  note?: string;
  secondary?: { have: unknown; need: unknown; unit: string };
}) {
  // Meter takes a 0..1 fraction. The two raw counts are printed above it in
  // full, so the bar is only ever a shape for a number already on screen.
  const progress = isNum(have) && isNum(need) && need > 0 ? Math.min(1, have / need) : null;
  return (
    <div className="rounded-lg border border-warning/50 bg-warning/[0.14] p-4">
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="mt-0.5 font-semibold">
          !
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[14px] font-semibold text-ink">{heading}</h3>
          <p className="tnum mt-1.5 text-[22px] font-semibold tracking-[-0.01em] text-ink">
            {isNum(have) ? count(have) : EM_DASH} of {isNum(need) ? count(need) : EM_DASH} {unit}
            {unitTerm && <TermInfo code={unitTerm} className="ml-2 align-middle" />}
          </p>
          {secondary && (
            <p className="tnum mt-0.5 text-sm text-ink2">
              {isNum(secondary.have) ? count(secondary.have) : EM_DASH} of{' '}
              {isNum(secondary.need) ? count(secondary.need) : EM_DASH} {secondary.unit}
            </p>
          )}
          <div className="mt-3 max-w-sm">
            <Meter fraction={progress} tone="warning" label={`${unit} collected against the threshold`} />
          </div>
          {note && <p className="mt-3 text-sm text-ink2">{note}</p>}
        </div>
      </div>
    </div>
  );
}

const BIN_COLUMNS: Column<CalibrationBin>[] = [
  {
    key: 'p_stated',
    header: 'Stated',
    align: 'right',
    // Deliberately NOT sortable — see DataTable's forbidden-sort guard. These
    // are probability columns; the table is ordered as the server sent it.
    render: (b) => pctFrac(b.p_stated, 1),
  },
  { key: 'p_realised', header: 'Realised', align: 'right', render: (b) => pctFrac(b.p_realised, 1) },
  { key: 'n', header: 'n', align: 'right', render: (b) => count(b.n) },
];

function CalibrationPanel({ data, mode }: { data: Calibration | null; mode: Mode }) {
  const insufficient = data?.state !== 'OK';
  const bins = data?.bins ?? [];

  return (
    <Panel
      title="Calibration"
      subtitle="Do stated probabilities match realised frequencies?"
      right={<SufficiencyChip value={data?.state} />}
    >
      {insufficient ? (
        <Refusal
          heading="Calibration is not established. No calibration figure is being claimed."
          have={data?.n_eff}
          need={data?.needed}
          unit="effective observations (n_eff)"
          unitTerm="n_eff"
          note={text(
            data?.note,
            'The backend has not seen enough resolved outcomes to say whether its probabilities are honest.',
          )}
        />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <StatTile
              label="n_eff"
              value={count(data?.n_eff)}
              footnote={
                <>
                  threshold {count(data?.needed)} <TermInfo code="n_eff" className="ml-1 align-middle" />
                </>
              }
            />
            <StatTile label="Resolution" value={num(data?.resolution)} footnote="as sent by the API" />
            <StatTile label="Brier skill score" value={num(data?.bss)} footnote="as sent by the API" />
          </div>
          <Reliability bins={bins} mode={mode} />
          {/* The chart's table twin. */}
          <DataTable
            rows={bins}
            columns={BIN_COLUMNS}
            rowKey={(_b, i) => `bin-${i}`}
            dense
            caption="Calibration bins: stated probability, realised frequency and bin count"
            empty="state is OK but no bins were returned."
          />
          {data?.note && <p className="text-xs text-muted">{data.note}</p>}
        </div>
      )}
    </Panel>
  );
}

/**
 * `p_lo` / `p_hi` carry no unit in the contract. They are rendered as percents
 * when they look like probabilities and as plain numbers otherwise, rather than
 * being multiplied by 100 on faith.
 */
function interval(lo: unknown, hi: unknown): string {
  if (!isNum(lo) || !isNum(hi)) return EM_DASH;
  const probable = lo >= 0 && hi <= 1;
  return probable ? `${pctFrac(lo, 1)} ${EN_DASH} ${pctFrac(hi, 1)}` : `${num(lo)} ${EN_DASH} ${num(hi)}`;
}

function SimulationPanel({ data, mode }: { data: Simulation | null; mode: Mode }) {
  const insufficient = data?.status !== 'OK';
  const buckets = data?.histogram ?? [];

  return (
    <Panel
      title="Simulation"
      subtitle="Block-bootstrap of realised history"
      right={<SufficiencyChip value={data?.status} />}
    >
      {insufficient ? (
        // No p_lo / p_hi here on purpose, even if the server sent them: an
        // interval computed from too little history is still a claim about the
        // future, and the whole point of this panel is that the claim is not
        // being made yet.
        <Refusal
          heading="Insufficient history. No simulated outcome is being reported."
          have={data?.n_days}
          need={data?.needed_days}
          unit="sessions"
          secondary={{ have: data?.n_trades, need: data?.needed_trades, unit: 'trades' }}
          note={text(data?.note, 'The bootstrap needs more sessions and more resolved trades before it means anything.')}
        />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-4">
            <StatTile
              label="Outcome interval"
              value={<span className="tnum">{interval(data?.p_lo, data?.p_hi)}</span>}
              footnote="interval only — no midpoint"
            />
            <StatTile label="Sessions" value={count(data?.n_days)} footnote={`threshold ${count(data?.needed_days)}`} />
            <StatTile label="Trades" value={count(data?.n_trades)} footnote={`threshold ${count(data?.needed_trades)}`} />
            <StatTile label="Block length" value={count(data?.block_len)} footnote="bootstrap block" />
          </div>
          <Histogram buckets={buckets} mode={mode} />
          {data?.note && <p className="text-xs text-muted">{data.note}</p>}
        </div>
      )}
    </Panel>
  );
}

export function Honesty({ version, mode }: { version: number; mode: Mode }) {
  const calibration = useApi<Calibration>('/api/calibration', version);
  const simulation = useApi<Simulation>('/api/simulation', version);

  return (
    <ApiGate states={[calibration, simulation]}>
      <div className="space-y-6">
        <p className="max-w-3xl text-sm text-ink2">
          These two panels exist to say what the system does <em>not</em> know. Until each has enough history, it
          reports the shortfall instead of a number — a probability the data cannot support is worse than no
          probability, because it gets acted on.
        </p>
        <CalibrationPanel data={calibration.data} mode={mode} />
        <SimulationPanel data={simulation.data} mode={mode} />
      </div>
    </ApiGate>
  );
}
