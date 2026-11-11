import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Why relative URLs: in dev Vite proxies /api -> 127.0.0.1:8000 (vite.config.ts),
 * and in production FastAPI serves this bundle itself. A relative path is
 * correct under both, so nothing in the app has to know which one is serving it
 * — and there is no CORS surface at all.
 *
 * Empty on purpose: every call site already passes a full `/api/...` path, so
 * a non-empty base here would produce `/api/api/...`. That 404s against the
 * SPA catch-all rather than against a route, which surfaces as "API
 * unreachable" and sends you debugging the wrong process.
 */
export const API_BASE = '';

export type ApiError = {
  /** Short line for the banner. */
  title: string;
  /** The gory bit, for the person who has to go restart uvicorn. */
  detail: string;
  /** True when we never reached the server at all (vs. it answering 500). */
  unreachable: boolean;
};

export type ApiState<T> = {
  data: T | null;
  error: ApiError | null;
  /** True on first load AND on refetch — see `stale` for which. */
  loading: boolean;
  /** True when we are refetching but still holding a previous good render. */
  stale: boolean;
  reload: () => void;
};

/**
 * One request at a time, app-wide.
 *
 * The backend 500s under concurrency. Measured, not guessed: seven endpoints
 * fired sequentially all answer 200; the same seven fired in parallel return
 * two or three `500 Internal Server Error` per round, and a different two or
 * three each time. That is a server-side shared-handle problem, not something
 * this UI can fix — but a dashboard whose landing view legitimately needs five
 * endpoints must not be the thing that triggers it.
 *
 * So every fetch goes through a FIFO of width one. It costs a few hundred
 * milliseconds on first paint and it is invisible after that, because each view
 * holds its previous render while refetching. It changes no data and no
 * contract: this is transport, not presentation.
 *
 * `.then(job, job)` rather than `.then(job)` is deliberate — a rejected
 * predecessor must not stall every request behind it.
 */
let queue: Promise<unknown> = Promise.resolve();

function enqueue<T>(job: () => Promise<T>): Promise<T> {
  const run = queue.then(job, job);
  queue = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

function describe(e: unknown, path: string): ApiError {
  // A bare TypeError from fetch() is the "nothing is listening on :8000" case.
  // It is by far the most likely failure here, so it gets the plain-English
  // message rather than the browser's "Failed to fetch".
  if (e instanceof TypeError) {
    return {
      title: 'API unreachable',
      detail: `No response from ${API_BASE}${path}. Is the FastAPI process up on http://127.0.0.1:8000?`,
      unreachable: true,
    };
  }
  if (e instanceof Error) {
    return { title: 'API error', detail: e.message, unreachable: false };
  }
  return { title: 'API error', detail: String(e), unreachable: false };
}

/**
 * Fetch one endpoint.
 *
 * Pass `path: null` to hold off (the Analysis view does this until a symbol is
 * chosen). `version` is a counter the shell bumps to refresh everything at once.
 */
export function useApi<T>(path: string | null, version = 0): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState<boolean>(path !== null);
  const [nonce, setNonce] = useState(0);
  // Kept in a ref so `stale` can distinguish "first paint" from "refetch", which
  // is what lets the views hold the old render instead of flashing a skeleton.
  const hasData = useRef(false);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (path === null) {
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    let live = true;
    setLoading(true);

    const once = async (): Promise<T> => {
      const res = await fetch(`${API_BASE}${path}`, {
        signal: ac.signal,
        headers: { accept: 'application/json' },
      });
      if (!res.ok) {
        let body = '';
        try {
          body = (await res.text()).slice(0, 300);
        } catch {
          /* body already consumed or not text — the status is enough */
        }
        const detail = body ? ` — ${body}` : '';
        const err = new Error(`HTTP ${res.status} ${res.statusText} from ${path}${detail}`);
        // Tagged so the retry below can tell "the server broke" from "the
        // server said no". A 4xx is an answer and is never retried.
        (err as Error & { serverFault?: boolean }).serverFault = res.status >= 500;
        throw err;
      }
      return (await res.json()) as T;
    };

    (async () => {
      try {
        // One retry, and only on a 5xx. The queue above removes the usual
        // cause; this covers the race where a manual Refresh lands on top of a
        // request already in flight. A second failure is reported, not hidden.
        const json = await enqueue(async () => {
          try {
            return await once();
          } catch (e) {
            if (ac.signal.aborted) throw e;
            if (!(e instanceof Error) || !(e as Error & { serverFault?: boolean }).serverFault) throw e;
            await new Promise((r) => setTimeout(r, 250));
            return once();
          }
        });
        if (!live) return;
        hasData.current = true;
        setData(json);
        setError(null);
      } catch (e) {
        if (!live || ac.signal.aborted) return;
        // Keep `data` as-is. A transient blip should dim the numbers, not wipe
        // them: a trader staring at a stale-but-labelled screen is better off
        // than one staring at an empty one.
        setError(describe(e, path));
      } finally {
        if (live) setLoading(false);
      }
    })();

    return () => {
      live = false;
      ac.abort();
    };
  }, [path, nonce, version]);

  return { data, error, loading, stale: loading && hasData.current, reload };
}

/**
 * Send a JSON POST request.
 *
 * Throws an Error with the server's detail message on failure so calling views
 * can catch and surface exact validation or rejection feedback.
 */
export async function postJson<T = unknown>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      accept: 'application/json',
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = '';
    try {
      const txt = await res.text();
      try {
        const json = JSON.parse(txt);
        detail = json.detail || json.message || txt.slice(0, 300);
      } catch {
        detail = txt.slice(0, 300);
      }
    } catch {
      /* ignore */
    }
    const msg = detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return (await res.json()) as T;
}
