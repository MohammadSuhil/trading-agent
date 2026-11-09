import { useMemo, useState } from 'react';
import { GROUP_LABEL, GROUP_ORDER, useGlossary } from '../lib/glossary';
import { ActionBadge } from '../components/action';
import { Empty, Panel } from '../components/primitives';
import { text } from '../lib/format';

/**
 * Every term the backend publishes, in one place.
 *
 * Tooltips answer "what is this one chip"; this page answers "what is all of
 * it", which is a different question and the one a reader has on their first
 * day. Every word on it comes from /api/glossary — this file contributes the
 * group headings and nothing else, so there is exactly one copy of each
 * definition in the system and it is the one the rules were written against.
 */
export function GlossaryView() {
  const g = useGlossary();
  const [query, setQuery] = useState('');

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const match = (t: { code: string; term?: string | null; short?: string | null; plain?: string | null }) =>
      !q ||
      [t.code, t.term, t.short, t.plain]
        .filter((v): v is string => typeof v === 'string')
        .some((v) => v.toLowerCase().includes(q));

    const seen = new Set(GROUP_ORDER as readonly string[]);
    const extra = Array.from(new Set(g.all.map((t) => text(t.group, 'other')))).filter((k) => !seen.has(k));
    const order = [...GROUP_ORDER, ...extra];

    return order
      .map((key) => ({
        key,
        label: GROUP_LABEL[key] ?? key,
        items: g.all.filter((t) => text(t.group, 'other') === key && match(t)),
      }))
      .filter((grp) => grp.items.length > 0);
  }, [g.all, query]);

  const total = g.all.length;
  const showing = groups.reduce((a, grp) => a + grp.items.length, 0);

  return (
    <div className="space-y-6">
      <Panel
        title="Glossary"
        subtitle={`${showing} of ${total} terms · served by /api/glossary`}
        right={
          <label className="flex items-center gap-1.5 text-xs text-muted">
            <span>Search</span>
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="code or wording"
              className="w-56 rounded-lg border border-line bg-surface px-2.5 py-1.5 text-xs text-ink placeholder:text-muted"
            />
          </label>
        }
      >
        {g.error && <p className="text-sm text-critical-ink">The glossary could not be loaded — {g.error}</p>}
        {!g.error && total === 0 && (
          <Empty>{g.loading ? 'Loading…' : 'The glossary endpoint returned no terms.'}</Empty>
        )}
        {!g.error && total > 0 && showing === 0 && <Empty>No term matches that search.</Empty>}

        <div className="space-y-8">
          {groups.map((grp) => (
            <section key={grp.key}>
              <div className="mb-3 flex items-baseline gap-3 border-b border-line pb-2">
                <h3 className="text-sm font-semibold text-ink">{grp.label}</h3>
                <span className="font-mono text-[10px] uppercase tracking-wide text-muted">
                  group {grp.key} · {grp.items.length}
                </span>
              </div>

              <dl className="grid gap-x-8 gap-y-5 lg:grid-cols-2">
                {grp.items.map((t) => (
                  <div key={t.code} className="min-w-0">
                    <dt className="flex flex-wrap items-baseline gap-2">
                      {grp.key === 'action' ? (
                        <ActionBadge value={t.code} size="sm" />
                      ) : (
                        <span className="rounded border border-line bg-sunken px-1.5 py-0.5 font-mono text-[10.5px] uppercase text-ink2">
                          {t.code}
                        </span>
                      )}
                      <span className="text-[13px] font-semibold text-ink">{text(t.short, '')}</span>
                    </dt>
                    <dd className="mt-1 text-[12.5px] leading-relaxed text-ink2">{text(t.plain, '')}</dd>
                    {text(t.why, '') && (
                      <dd className="mt-2 border-l-2 border-accent/40 pl-2.5">
                        <div className="label">Why it counts</div>
                        <p className="mt-0.5 text-[12px] leading-relaxed text-muted">{text(t.why, '')}</p>
                      </dd>
                    )}
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </Panel>
    </div>
  );
}
