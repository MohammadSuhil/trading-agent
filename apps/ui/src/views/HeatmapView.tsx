import { useApi } from '../api/client';
import type { Heatmap, HeatmapRow } from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { DataTable, type Column } from '../components/DataTable';
import { Delta, Panel } from '../components/primitives';
import { Treemap } from '../charts/Treemap';
import type { Mode } from '../lib/theme';
import { count, crore, pctFrac, signedPctFrac, stamp, text } from '../lib/format';

const COLUMNS: Column<HeatmapRow>[] = [
  {
    key: 'industry',
    header: 'Industry',
    sortable: true,
    value: (r) => r.industry,
    render: (r) => <span className="font-medium text-ink">{text(r.industry)}</span>,
  },
  { key: 'n', header: 'Names', align: 'right', sortable: true, value: (r) => r.n, render: (r) => count(r.n) },
  {
    key: 'ret_1d',
    header: 'Ret 1d',
    align: 'right',
    sortable: true,
    value: (r) => r.ret_1d,
    render: (r) => <Delta value={r.ret_1d} format={(v) => signedPctFrac(v)} />,
  },
  {
    key: 'ret_5d',
    header: 'Ret 5d',
    align: 'right',
    sortable: true,
    value: (r) => r.ret_5d,
    render: (r) => <Delta value={r.ret_5d} format={(v) => signedPctFrac(v)} />,
  },
  {
    key: 'ret_21d',
    header: 'Ret 21d',
    align: 'right',
    sortable: true,
    value: (r) => r.ret_21d,
    render: (r) => <Delta value={r.ret_21d} format={(v) => signedPctFrac(v)} />,
  },
  {
    key: 'turnover_cr',
    header: 'Turnover',
    align: 'right',
    sortable: true,
    value: (r) => r.turnover_cr,
    render: (r) => crore(r.turnover_cr),
  },
  {
    key: 'breadth_pct',
    header: 'Breadth',
    align: 'right',
    sortable: true,
    value: (r) => r.breadth_pct,
    render: (r) => pctFrac(r.breadth_pct),
    term: 'breadth',
  },
];

export function HeatmapView({ version, mode }: { version: number; mode: Mode }) {
  const heat = useApi<Heatmap>('/api/heatmap', version);
  const rows = heat.data?.rows ?? [];

  return (
    <ApiGate states={[heat]}>
      <div className="space-y-6">
        <Panel
          title="Industries by turnover"
          subtitle="Area is turnover; colour is the 1-day return, diverging from a neutral zero."
          right={<span className="text-xs text-muted">as of {stamp(heat.data?.as_of)}</span>}
        >
          <Treemap rows={rows} mode={mode} />
        </Panel>

        {/* The table view twin. Every figure the map encodes is here as text,
            sortable, and reachable without a pointer. */}
        <Panel title="Industry table" subtitle={`${count(rows.length)} industries`}>
          <DataTable
            rows={rows}
            columns={COLUMNS}
            rowKey={(r, i) => `${text(r.industry, 'x')}-${i}`}
            initialSort={{ key: 'turnover_cr', dir: 'desc' }}
            caption="Industries with returns, turnover and breadth"
            empty="The heat map endpoint returned no industries."
          />
        </Panel>
      </div>
    </ApiGate>
  );
}
