import { Component, type ErrorInfo, type ReactNode } from 'react';
import type { ApiError } from '../api/client';
import { cn } from './primitives';

export type GateState = {
  data: unknown;
  error: ApiError | null;
  loading: boolean;
  stale: boolean;
  reload: () => void;
};

function Retry({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink hover:bg-sunken"
    >
      Retry
    </button>
  );
}

/**
 * The "API unreachable" state.
 *
 * The API is a separate local process that is frequently not running yet, so
 * this is a normal screen rather than an exception path: it names the endpoint
 * and the port, and it is reached whenever we have no data to show. The one
 * thing it must never do is render blank.
 */
export function ApiDown({ errors, onRetry }: { errors: ApiError[]; onRetry: () => void }) {
  const unreachable = errors.some((e) => e.unreachable);
  return (
    <div className="panel p-8 text-center">
      <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full border border-critical/50 bg-critical/10 text-critical-ink">
        <span aria-hidden="true">✕</span>
      </div>
      <h2 className="mt-3 text-base font-semibold text-ink">
        {unreachable ? 'API unreachable' : 'API returned an error'}
      </h2>
      <p className="mx-auto mt-2 max-w-xl text-sm text-ink2">
        {unreachable
          ? 'The dashboard could not reach the backend. Nothing on this page is live.'
          : 'The backend answered, but not with data this page can use.'}
      </p>
      <ul className="mx-auto mt-3 max-w-xl space-y-1 text-left text-xs text-muted">
        {errors.map((e, i) => (
          <li key={i} className="break-words">
            <span className="font-medium text-ink2">{e.title}:</span> {e.detail}
          </li>
        ))}
      </ul>
      {unreachable && (
        <pre className="mx-auto mt-4 w-fit rounded bg-sunken px-3 py-2 text-left text-xs text-ink2">
          uv run uvicorn ta_api.main:app --host 127.0.0.1 --port 8000
        </pre>
      )}
      <div className="mt-4">
        <Retry onClick={onRetry} />
      </div>
    </div>
  );
}

function Loading() {
  return (
    <div className="panel p-8 text-center text-sm text-muted" role="status">
      Loading…
    </div>
  );
}

/**
 * Wraps a view's content against its endpoints.
 *
 * On refetch the previous render is held at reduced opacity instead of being
 * replaced by a skeleton — no layout jump, and the numbers stay on screen while
 * the new ones arrive. On a refetch that FAILS, the old render also stays, but
 * behind a visible "stale" strip, because silently showing yesterday's prices
 * as if they were live is the one failure mode worth shouting about.
 */
export function ApiGate({ states, children }: { states: GateState[]; children: ReactNode }) {
  const errors = states.map((s) => s.error).filter((e): e is ApiError => e !== null);
  const anyData = states.some((s) => s.data !== null);
  const allLoadingFirstTime = states.every((s) => s.loading) && !anyData;

  if (errors.length > 0 && !anyData) {
    return <ApiDown errors={errors} onRetry={() => states.forEach((s) => s.reload())} />;
  }
  if (allLoadingFirstTime) return <Loading />;

  const refetching = states.some((s) => s.stale);

  return (
    <>
      {errors.length > 0 && (
        <div className="mb-4 flex items-start justify-between gap-4 rounded-lg border border-warning/50 bg-warning/10 px-4 py-3">
          <div className="text-sm text-ink">
            <span aria-hidden="true" className="mr-2">
              !
            </span>
            {/* Deliberately not "showing the last good response": when one of
                several endpoints fails on first load there IS no previous
                response for it, and claiming otherwise would tell the reader
                that a panel which is actually empty is merely stale. */}
            <span className="font-medium">
              Part of this view did not load. Anything still on screen is from an earlier response.
            </span>{' '}
            <span className="text-ink2">{errors[0]?.detail}</span>
          </div>
          <Retry onClick={() => states.forEach((s) => s.reload())} />
        </div>
      )}
      <div className={cn(refetching && 'opacity-60 transition-opacity duration-150')}>{children}</div>
    </>
  );
}

/* ------------------------------------------------------------- boundary */

type BoundaryState = { error: Error | null };

/**
 * Last line of defence. The contract says `ret_1d: number`; reality sends null,
 * and one `.toFixed()` on it would otherwise unmount the whole app and leave a
 * white page. Every view is individually null-safe, but a blank screen is such
 * a bad outcome that it gets a backstop too.
 */
export class ErrorBoundary extends Component<{ children: ReactNode; onReset?: () => void }, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep the component stack: "cannot read properties of null" is useless
    // without knowing which card threw it.
    console.error('[trading-agent/ui] render failed', error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="panel p-8 text-center">
        <h2 className="text-base font-semibold text-critical-ink">This view failed to render</h2>
        <p className="mx-auto mt-2 max-w-xl text-sm text-ink2">
          A field was not the shape the UI expected. The rest of the dashboard is unaffected.
        </p>
        <pre className="mx-auto mt-3 w-fit max-w-full overflow-auto rounded bg-sunken px-3 py-2 text-left text-xs text-ink2">
          {error.message}
        </pre>
        <button
          type="button"
          className="mt-4 rounded-lg border border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink hover:bg-sunken"
          onClick={() => {
            this.setState({ error: null });
            this.props.onReset?.();
          }}
        >
          Try again
        </button>
      </div>
    );
  }
}
