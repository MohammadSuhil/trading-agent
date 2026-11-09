import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { useApi } from '../api/client';
import type { Glossary, GlossaryTerm } from '../api/types';

/**
 * The glossary, fetched once at app level and read everywhere.
 *
 * Deliberately NOT keyed to the refresh counter. These 47 definitions change
 * when the backend ships, not when the market does, and refetching them on
 * every ↻ would put a network round-trip behind a button whose job is to update
 * prices.
 *
 * The UI contributes no text of its own to any of this. A term the server has
 * not defined gets no tooltip rather than an invented one — a definition that
 * drifts from the rule that fired is worse than no definition, because it reads
 * just as authoritative.
 */

export type GlossaryIndex = {
  /** Look up one code. Case-insensitive; returns null when undefined. */
  get: (code: unknown) => (GlossaryTerm & { code: string }) | null;
  /** Every term, in the order the server sent them. */
  all: (GlossaryTerm & { code: string })[];
  loading: boolean;
  /** Present when the glossary endpoint itself failed. */
  error: string | null;
};

const EMPTY: GlossaryIndex = { get: () => null, all: [], loading: false, error: null };

const Ctx = createContext<GlossaryIndex>(EMPTY);

export function GlossaryProvider({ children }: { children: ReactNode }) {
  const state = useApi<Glossary>('/api/glossary');

  const value = useMemo<GlossaryIndex>(() => {
    const terms = state.data?.terms ?? null;
    const all = terms
      ? Object.entries(terms)
          .filter(([, v]) => v && typeof v === 'object')
          .map(([code, v]) => ({ ...v, code }))
      : [];
    // Upper-cased index, because the metric keys are mixed case on the wire
    // ("n_eff", "R:R", "P(up barrier first)") while the enum keys are not.
    const byUpper = new Map(all.map((t) => [t.code.toUpperCase(), t]));
    return {
      all,
      loading: state.loading && all.length === 0,
      error: state.error ? state.error.detail : null,
      get: (code: unknown) => {
        if (typeof code !== 'string' || code.length === 0) return null;
        return byUpper.get(code.toUpperCase()) ?? null;
      },
    };
  }, [state.data, state.loading, state.error]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useGlossary(): GlossaryIndex {
  return useContext(Ctx);
}

/** Order the Glossary view (and any grouped list) presents groups in. */
export const GROUP_ORDER = ['action', 'reason', 'sleeve', 'trend', 'metric'] as const;

/** Section headings. These name the GROUP, they do not define any term. */
export const GROUP_LABEL: Record<string, string> = {
  action: 'What you are being told to do',
  reason: 'Why a name was picked',
  sleeve: 'Strategy family',
  trend: 'Trend state',
  metric: 'Numbers on the screen',
};
