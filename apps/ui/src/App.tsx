import { useCallback, useEffect, useState } from 'react';
import { useApi } from './api/client';
import type { Health } from './api/types';
import { ErrorBoundary } from './components/ApiGate';
import { Nav, VIEWS, resolveViewId, type ViewId } from './components/Nav';
import { HealthChip, QualityChip, ServiceChip } from './components/StateChip';
import { cn } from './components/primitives';
import { GlossaryProvider } from './lib/glossary';
import { useTheme } from './lib/theme';
import { stamp, text } from './lib/format';

import { Today } from './views/Today';
import { HeatmapView } from './views/HeatmapView';
import { Screener } from './views/Screener';
import { PortfolioView } from './views/PortfolioView';
import { RiskView } from './views/RiskView';
import { Analysis } from './views/Analysis';
import { Honesty } from './views/Honesty';
import { GlossaryView } from './views/GlossaryView';

function readHash(): ViewId {
  const raw = window.location.hash.replace(/^#\/?/, '').trim();
  return resolveViewId(raw) ?? 'today';
}

export default function App() {
  const { mode, toggle } = useTheme();
  const [view, setView] = useState<ViewId>(readHash);
  // One counter refreshes every endpoint on the page at once. Per-panel refresh
  // buttons would let two tiles disagree about what "now" is.
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const onHash = () => setView(readHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const health = useApi<Health>('/api/health', version);
  const refresh = useCallback(() => setVersion((v) => v + 1), []);

  const svc = health.data?.services ?? null;
  const quality = health.data?.data_quality ?? null;
  const partial = typeof quality === 'string' && quality.toUpperCase() !== 'OK';
  const current = VIEWS.find((v) => v.id === view);

  return (
    // The glossary is fetched once here and read by every chip, label and
    // tooltip below. One source of truth for what a code means, and it is the
    // server's.
    <GlossaryProvider>
      <div className="flex min-h-screen bg-page text-ink">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2 focus:text-sm"
        >
          Skip to content
        </a>

        {/* The rail. Full height, no border — it is its own plane, and the
            canvas beside it does the separating. */}
        <aside className="rail sticky top-0 hidden h-screen w-60 shrink-0 flex-col bg-rail md:flex">
          <div className="px-5 pb-4 pt-5">
            <div className="flex items-center gap-2.5">
              <span
                aria-hidden="true"
                className="flex h-9 w-9 items-center justify-center rounded-[10px] bg-white/10 text-[15px] font-semibold text-white"
              >
                ₹
              </span>
              <div className="min-w-0">
                <div className="text-[14px] font-semibold leading-tight tracking-[-0.01em] text-white">
                  Trading Agent
                </div>
                <div className="text-[10.5px] leading-tight text-rail-muted">Indian equities · paper</div>
              </div>
            </div>
          </div>

          <Nav current={view} />

          <div className="mt-auto space-y-3 bg-rail-2 px-4 py-4">
            <div className="flex flex-wrap gap-1">
              <ServiceChip name="clickhouse" up={svc?.clickhouse} onRail />
              <ServiceChip name="mongo" up={svc?.mongo} onRail />
              <ServiceChip name="redis" up={svc?.redis} onRail />
            </div>
            <button
              type="button"
              onClick={toggle}
              className="w-full rounded-lg border border-white/15 px-2 py-2 text-[11.5px] font-medium text-rail-ink transition-colors hover:bg-white/10 hover:text-white"
              aria-label={`Switch to ${mode === 'dark' ? 'light' : 'dark'} mode`}
            >
              <span aria-hidden="true" className="mr-1.5">
                {mode === 'dark' ? '☀' : '☾'}
              </span>
              {mode === 'dark' ? 'Light mode' : 'Dark mode'}
            </button>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-20 border-b border-line bg-page/90 backdrop-blur">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-6 py-4">
              <div className="min-w-0">
                <h1 className="text-[19px] font-semibold leading-tight tracking-[-0.02em]">
                  {current?.label ?? 'Today'}
                </h1>
                <p className="mt-0.5 truncate text-[12px] text-muted">{current?.hint ?? ''}</p>
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                <HealthChip value={health.data?.status} />
                {quality !== null && <QualityChip value={quality} />}
              </div>
              <div className="ml-auto flex items-center gap-3">
                <span className="text-[12px] text-muted">
                  as of <span className="tnum text-ink2">{stamp(health.data?.as_of)}</span>
                </span>
                <button
                  type="button"
                  onClick={refresh}
                  className={cn(
                    'rounded-lg bg-rail-active px-3.5 py-2 text-[12px] font-semibold text-white shadow-raise transition-opacity hover:opacity-90',
                    health.loading && 'opacity-60',
                  )}
                >
                  <span aria-hidden="true" className="mr-1.5">
                    ↻
                  </span>
                  Refresh
                </button>
              </div>
            </div>

            {/* Nav for narrow windows. The rail is hidden below md. */}
            <div className="border-t border-line px-4 md:hidden">
              <ul className="flex gap-1.5 overflow-x-auto py-2">
                {VIEWS.map((v) => (
                  <li key={v.id}>
                    <a
                      href={`#/${v.id}`}
                      aria-current={v.id === view ? 'page' : undefined}
                      title={v.hint}
                      className={cn(
                        'flex items-center gap-1.5 whitespace-nowrap rounded-lg border px-2.5 py-1.5 text-[12px]',
                        v.id === view
                          ? 'border-transparent bg-rail-active font-semibold text-white'
                          : 'border-line bg-surface text-ink2',
                      )}
                    >
                      <span aria-hidden="true" className={cn('h-2 w-2 rounded-full', v.tint)} />
                      {v.label}
                    </a>
                  </li>
                ))}
              </ul>
            </div>

            {partial && (
              // Visible, not a tooltip: a PARTIAL day is a day whose numbers were
              // computed over an incomplete input set, and every figure below
              // inherits that caveat.
              <div className="flex items-start gap-2 border-t border-warning/50 bg-warning/[0.14] px-6 py-2.5 text-[13px]">
                <span aria-hidden="true" className="font-semibold text-warning-ink">
                  !
                </span>
                <p className="text-ink">
                  <span className="font-semibold">data_quality: {text(quality)}</span> — part of the input set was
                  unavailable for this session. Treat every number on this page as provisional.
                </p>
              </div>
            )}

            {health.error && !health.data && (
              <div className="flex items-start gap-2 border-t border-critical/50 bg-critical/10 px-6 py-2.5 text-[13px]">
                <span aria-hidden="true" className="font-semibold text-critical-ink">
                  ✕
                </span>
                <p className="text-ink">
                  <span className="font-semibold">API unreachable</span> — {health.error.detail}
                </p>
              </div>
            )}
          </header>

          <main id="main" className="mx-auto w-full max-w-[1800px] flex-1 px-6 py-6">
            {/* Keyed so a crashed view resets when the reader navigates away. */}
            <ErrorBoundary key={view} onReset={refresh}>
              {view === 'today' && <Today version={version} onRefresh={refresh} />}
              {view === 'heatmap' && <HeatmapView version={version} mode={mode} />}
              {view === 'screener' && <Screener version={version} />}
              {view === 'portfolio' && <PortfolioView version={version} mode={mode} onRefresh={refresh} />}
              {view === 'risk' && <RiskView version={version} />}
              {view === 'analysis' && <Analysis version={version} mode={mode} />}
              {view === 'calibration' && <Honesty version={version} mode={mode} onRefresh={refresh} />}
              {view === 'glossary' && <GlossaryView />}
            </ErrorBoundary>
          </main>

          <footer className="px-6 py-4 text-[11px] text-muted">
            Every figure on this dashboard is rendered exactly as the API sent it. The UI computes nothing except
            formatting; every definition comes from{' '}
            <a href="#/glossary" className="font-medium text-accent-ink hover:underline">
              /api/glossary
            </a>
            .
          </footer>
        </div>
      </div>
    </GlossaryProvider>
  );
}
