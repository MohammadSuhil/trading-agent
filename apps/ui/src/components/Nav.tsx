import { cn } from '../lib/cn';

/**
 * The rail.
 *
 * Each entry carries FOUR identifying channels, because the owner asked to be
 * able to find a tab without reading it: a fixed position in the list, a glyph,
 * its own hue on the icon tile, and the word. Hue is therefore never doing the
 * work alone — remove colour entirely and the rail still navigates.
 *
 * `hint` is the title, which is also what the ⓘ-less rail uses as its tooltip.
 */
export const VIEWS = [
  {
    id: 'today',
    label: 'Today',
    glyph: '◆',
    tint: 'bg-[rgb(var(--nav-today))]',
    hint: "Today: market context, what to do now, and what was rejected",
  },
  {
    id: 'heatmap',
    label: 'Heat map',
    glyph: '▦',
    tint: 'bg-[rgb(var(--nav-heatmap))]',
    hint: 'Heat map: industries sized by turnover, coloured by 1-day return',
  },
  {
    id: 'screener',
    label: 'Screener',
    glyph: '≡',
    tint: 'bg-[rgb(var(--nav-screener))]',
    hint: 'Screener: the ranked candidate universe with every column',
  },
  {
    id: 'portfolio',
    label: 'Portfolio',
    glyph: '◧',
    tint: 'bg-[rgb(var(--nav-portfolio))]',
    hint: 'Portfolio: equity, cash, and the positions you actually hold',
  },
  {
    id: 'risk',
    label: 'Risk',
    glyph: '⚑',
    tint: 'bg-[rgb(var(--nav-risk))]',
    hint: 'Risk: every rule, its state, and how much headroom is left',
  },
  {
    id: 'analysis',
    label: 'Analysis',
    glyph: '◢',
    tint: 'bg-[rgb(var(--nav-analysis))]',
    hint: 'Analysis: per-symbol candles, trend states and relative strength',
  },
  {
    id: 'calibration',
    label: 'Calibration',
    glyph: '◎',
    tint: 'bg-[rgb(var(--nav-calibration))]',
    hint: 'Calibration: whether stated probabilities have earned any trust yet',
  },
  {
    id: 'glossary',
    label: 'Glossary',
    glyph: '✦',
    tint: 'bg-[rgb(var(--nav-glossary))]',
    hint: 'Glossary: every code and metric on this dashboard, in plain English',
  },
] as const;

export type ViewId = (typeof VIEWS)[number]['id'];

/**
 * Old hashes that must keep resolving. `#/honesty` was this build's name for
 * the calibration view and is certainly in somebody's bookmarks.
 */
const ALIASES: Record<string, ViewId> = { honesty: 'calibration' };

export function resolveViewId(raw: string): ViewId | null {
  const v = raw.trim().toLowerCase();
  if (VIEWS.some((x) => x.id === v)) return v as ViewId;
  return ALIASES[v] ?? null;
}

export function isViewId(v: string): v is ViewId {
  return VIEWS.some((x) => x.id === v);
}

/**
 * Anchors, not buttons, and the hash is the route.
 *
 * Using real links buys the whole keyboard and assistive-tech story for free —
 * Tab order, Enter to activate, focus ring, "open in new tab", browser back —
 * and it means a reload or a bookmarked URL lands on the same view instead of
 * dumping the reader back on Today.
 */
export function Nav({ current }: { current: ViewId }) {
  return (
    <nav aria-label="Views" className="px-3 py-2">
      <ul className="space-y-0.5">
        {VIEWS.map((v) => {
          const active = v.id === current;
          return (
            <li key={v.id}>
              <a
                href={`#/${v.id}`}
                aria-current={active ? 'page' : undefined}
                title={v.hint}
                className={cn(
                  'group relative flex items-center gap-3 rounded-lg px-2.5 py-2 text-[13px] transition-colors',
                  active ? 'bg-rail-active font-semibold text-white' : 'text-rail-ink hover:bg-white/[0.07] hover:text-white',
                )}
              >
                {/* The icon tile: the hue lives here and nowhere else, so the
                    rail stays calm while each destination stays identifiable. */}
                <span
                  aria-hidden="true"
                  className={cn(
                    'flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold text-rail transition-opacity',
                    v.tint,
                    active ? 'opacity-100' : 'opacity-75 group-hover:opacity-100',
                  )}
                >
                  {v.glyph}
                </span>
                <span className="truncate">{v.label}</span>
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
