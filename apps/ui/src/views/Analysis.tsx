import { useEffect, useMemo, useState } from 'react';
import { useApi } from '../api/client';
import type { Recommendations, Trend } from '../api/types';
import { ApiGate } from '../components/ApiGate';
import { TagChip } from '../components/StateChip';
import { Chip, Field, Panel, StatTile } from '../components/primitives';
import { TermInfo, TermText } from '../components/Tooltip';
import { Candles } from '../charts/Candles';
import type { Mode } from '../lib/theme';
import { count, isStr, num, text } from '../lib/format';

function SymbolPicker({
  value,
  onChange,
  suggestions,
}: {
  value: string;
  onChange: (s: string) => void;
  suggestions: string[];
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  return (
    <div className="flex flex-wrap items-center gap-3">
      <form
        className="flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const s = draft.trim().toUpperCase();
          if (s) onChange(s);
        }}
      >
        <label className="flex items-center gap-1.5 text-xs text-muted">
          <span>Symbol</span>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            list="ta-symbols"
            placeholder="e.g. RELIANCE"
            className="w-44 rounded-lg border border-line bg-surface px-2 py-1 text-xs uppercase text-ink placeholder:normal-case placeholder:text-muted"
          />
        </label>
        <datalist id="ta-symbols">
          {suggestions.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
        <button
          type="submit"
          className="rounded-lg border border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink hover:bg-sunken"
        >
          Load
        </button>
      </form>

      {suggestions.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-xs text-muted">from today:</span>
          {suggestions.slice(0, 8).map((s) => (
            <button key={s} type="button" onClick={() => onChange(s)} className="rounded">
              <Chip tone={s === value ? 'info' : 'neutral'}>{s}</Chip>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Analysis({ version, mode }: { version: number; mode: Mode }) {
  const [symbol, setSymbol] = useState('');

  const recs = useApi<Recommendations>('/api/recommendations', version);
  const suggestions = useMemo(
    () => (recs.data?.rows ?? []).map((r) => r.symbol).filter((s): s is string => isStr(s)),
    [recs.data],
  );

  // Seed from the first recommendation once, so the view is not empty on
  // arrival — but never overwrite a symbol the reader typed.
  useEffect(() => {
    if (!symbol && suggestions.length > 0 && suggestions[0]) setSymbol(suggestions[0]);
  }, [suggestions, symbol]);

  const trend = useApi<Trend>(symbol ? `/api/trend/${encodeURIComponent(symbol)}` : null, version);
  const t = trend.data;
  const states = t?.states ?? null;
  const bars = t?.bars ?? [];

  return (
    <div className="space-y-6">
      <Panel title="Symbol" bodyClassName="p-4">
        <SymbolPicker value={symbol} onChange={setSymbol} suggestions={suggestions} />
        {!symbol && (
          <p className="mt-3 text-sm text-muted">
            Enter a symbol to load its candles and trend state. Nothing is fetched until you do.
          </p>
        )}
      </Panel>

      {symbol && (
        <ApiGate states={[trend]}>
          <div className="space-y-6">
            <Panel
              title={`${text(t?.symbol, symbol)} — daily candles`}
              subtitle="Blue candles are up days, red are down; the OHLC readout above the plot carries the same values as text."
            >
              <Candles bars={bars} symbol={text(t?.symbol, symbol)} mode={mode} />
            </Panel>

            <div className="grid gap-4 lg:grid-cols-2">
              <Panel title="Trend state" subtitle="20, 63 and 252 sessions — hover any state for the server's wording">
                <div className="grid grid-cols-3 gap-4">
                  <Field label="t20" term="t20">
                    <TagChip value={states?.t20} plain />
                  </Field>
                  <Field label="t63" term="t63">
                    <TagChip value={states?.t63} plain />
                  </Field>
                  <Field label="t252" term="t252">
                    <TagChip value={states?.t252} plain />
                  </Field>
                </div>
                <p className="mt-3 text-xs text-muted">
                  Three lookbacks, as classified by the backend. They are shown side by side rather than reduced to one
                  label: a name that is up on 20 sessions and down on 252 is a different trade from one that is up on
                  all three, and collapsing that distinction here would hide it.
                </p>
              </Panel>

              <Panel title="Trend quality and relative strength">
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  <StatTile
                    label="ER 63"
                    value={num(t?.er_63)}
                    footnote={
                      <>
                        efficiency ratio <TermInfo code="er_63" className="ml-1 align-middle" />
                      </>
                    }
                  />
                  <StatTile
                    label="ADX 14"
                    value={num(t?.adx14)}
                    footnote={<TermText code="ADX_TRENDING" className="text-muted" />}
                  />
                  <StatTile
                    label="Slope t 63"
                    value={num(t?.slope_t_63)}
                    footnote={
                      <>
                        t-stat of the 63d slope <TermInfo code="slope_t_63" className="ml-1 align-middle" />
                      </>
                    }
                  />
                  <StatTile
                    label="Flips 63d"
                    value={count(t?.flips_63d)}
                    footnote={
                      <>
                        state changes <TermInfo code="flips_63d" className="ml-1 align-middle" />
                      </>
                    }
                  />
                  <StatTile
                    label="RS vs bench 63"
                    value={num(t?.rs_vs_bench_63)}
                    footnote={<TermText code="RS_VS_NIFTY" className="text-muted" />}
                  />
                  <StatTile label="Sessions" value={count(bars.length)} footnote="bars returned" />
                </div>
                <p className="mt-3 text-xs text-muted">
                  Units are whatever /api/trend sends; the UI does not rescale them.
                </p>
              </Panel>
            </div>
          </div>
        </ApiGate>
      )}
    </div>
  );
}
