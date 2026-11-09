import { useMemo, useState, type ReactNode } from 'react';
import { cn, Empty } from './primitives';
import { TermInfo } from './Tooltip';
import { isNum } from '../lib/format';

export type Column<T> = {
  /** Stable id; also the field name when it maps to one. */
  key: string;
  header: string;
  align?: 'left' | 'right';
  sortable?: boolean;
  /** Sort key. Omit on non-sortable columns. */
  value?: (row: T) => number | string | null | undefined;
  render: (row: T) => ReactNode;
  /** Tailwind width class, e.g. 'w-28'. */
  className?: string;
  title?: string;
  /**
   * Glossary code for this column's figure. Rendered as a separate ⓘ tab stop
   * beside the header — never inside the sort button, which is already a
   * <button> and cannot legally contain another one.
   */
  term?: string;
};

/**
 * Columns that must never become a sort control.
 *
 * Sorting a list by a probability point estimate is exactly the affordance this
 * product refuses to offer: it ranks names by a number whose uncertainty the
 * sort silently discards, and it invites the reader to treat the top row as
 * "most likely to work". The guard throws in dev so the rule cannot be broken
 * by someone adding a column in good faith six months from now.
 */
const FORBIDDEN_SORT_KEYS = /^(p_up|p_dn|p_vert|ci_lo|ci_hi|prob|probability|p_hit|win_rate)$/i;

type SortDir = 'asc' | 'desc';

function compare(a: unknown, b: unknown): number {
  const aMissing = a === null || a === undefined || (typeof a === 'number' && !Number.isFinite(a));
  const bMissing = b === null || b === undefined || (typeof b === 'number' && !Number.isFinite(b));
  // Missing values always sink, in both directions: a null is not "the
  // smallest value", it is the absence of one, and floating it to the top of a
  // descending sort would read as a real extreme.
  if (aMissing && bMissing) return 0;
  if (aMissing) return 1;
  if (bMissing) return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), 'en-IN', { numeric: true, sensitivity: 'base' });
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  initialSort,
  empty = 'No rows.',
  dense = false,
  caption,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T, i: number) => string;
  initialSort?: { key: string; dir: SortDir };
  empty?: ReactNode;
  dense?: boolean;
  /** Accessible table caption. Visually hidden. */
  caption?: string;
}) {
  if (import.meta.env.DEV) {
    const bad = columns.find((c) => c.sortable && FORBIDDEN_SORT_KEYS.test(c.key));
    if (bad) {
      throw new Error(
        `DataTable: column "${bad.key}" must not be sortable — sorting by a probability point estimate is forbidden.`,
      );
    }
  }

  const [sort, setSort] = useState<{ key: string; dir: SortDir } | null>(initialSort ?? null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.value) return rows;
    const get = col.value;
    const sign = sort.dir === 'asc' ? 1 : -1;
    // Decorate with the original index so equal keys keep input order —
    // Array.prototype.sort is stable in V8, but the index makes it explicit and
    // keeps the "missing sinks in both directions" rule from being reordered.
    return rows
      .map((row, i) => ({ row, i }))
      .sort((x, y) => {
        const c = compare(get(x.row), get(y.row));
        return c === 0 ? x.i - y.i : c * sign;
      })
      .map((d) => d.row);
  }, [rows, columns, sort]);

  const onSort = (key: string) => {
    setSort((prev) => {
      if (prev?.key !== key) return { key, dir: 'desc' };
      if (prev.dir === 'desc') return { key, dir: 'asc' };
      return null; // third click clears — back to the server's own order
    });
  };

  if (rows.length === 0) return <Empty>{empty}</Empty>;

  const pad = dense ? 'px-3 py-2' : 'px-3.5 py-2.5';

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[13px]">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-line bg-sunken">
            {columns.map((c) => {
              const active = sort?.key === c.key;
              const ariaSort = active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none';
              return (
                <th
                  key={c.key}
                  scope="col"
                  aria-sort={c.sortable ? ariaSort : undefined}
                  title={c.title}
                  className={cn(
                    'label whitespace-nowrap font-medium',
                    pad,
                    c.align === 'right' ? 'text-right' : 'text-left',
                    c.className,
                  )}
                >
                  <span
                    className={cn(
                      'inline-flex items-center gap-1',
                      c.align === 'right' && 'flex-row-reverse',
                    )}
                  >
                    {c.sortable && c.value ? (
                      <button
                        type="button"
                        onClick={() => onSort(c.key)}
                        className={cn(
                          'inline-flex items-center gap-1 rounded hover:text-ink',
                          active && 'text-ink',
                          c.align === 'right' && 'flex-row-reverse',
                        )}
                      >
                        <span>{c.header}</span>
                        {/* Glyph, not colour: the sorted column is identifiable
                            in greyscale and to a screen reader (aria-sort). */}
                        <span aria-hidden="true" className={cn('text-[9px]', !active && 'text-muted/50')}>
                          {active ? (sort.dir === 'asc' ? '▲' : '▼') : '⇅'}
                        </span>
                      </button>
                    ) : (
                      <span>{c.header}</span>
                    )}
                    {c.term && <TermInfo code={c.term} />}
                  </span>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, i) => (
            <tr key={rowKey(row, i)} className="border-b border-line last:border-0 hover:bg-sunken">
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={cn(
                    pad,
                    // Middle, not top: a single tall cell (a wrapped industry
                    // name, a stack of chips) used to drag every other cell in
                    // the row to the ceiling and leave a band of empty space.
                    'align-middle',
                    c.align === 'right' ? 'tnum text-right' : 'text-left',
                    c.className,
                  )}
                >
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Numeric cell helper that keeps the em-dash path in one place. */
export function numCell(v: unknown, fmt: (x: unknown) => string): ReactNode {
  return <span className={isNum(v) ? 'text-ink' : 'text-muted'}>{fmt(v)}</span>;
}
