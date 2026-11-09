import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useGlossary } from '../lib/glossary';
import { cn } from '../lib/cn';
import { text } from '../lib/format';

/**
 * The explanation layer.
 *
 * Three rules this component exists to enforce:
 *
 * 1. **Not hover-only.** The trigger is a real <button>, so it is in the tab
 *    order, opens on focus, and answers Enter/Space. A definition reachable
 *    only by a mouse is a definition a keyboard reader does not have.
 * 2. **Touch-safe.** A tap toggles and *pins* the panel open; it does not close
 *    on the synthetic mouseleave a touch device fires straight afterwards.
 *    Outside-tap and Escape close it.
 * 3. **Portalled and fixed.** Cards clip their overflow and tables scroll
 *    horizontally; a popover that is a child of either gets cut in half. This
 *    one is positioned in viewport coordinates on document.body, flipped above
 *    the trigger when it would run off the bottom and clamped horizontally.
 *
 * Content is passed in, never written here — see lib/glossary.
 */

type Placement = { left: number; top: number; above: boolean };

function usePlacement(
  open: boolean,
  anchor: HTMLElement | null,
  panel: HTMLElement | null,
): Placement | null {
  const [place, setPlace] = useState<Placement | null>(null);

  useLayoutEffect(() => {
    if (!open || !anchor) {
      setPlace(null);
      return;
    }
    const measure = () => {
      const a = anchor.getBoundingClientRect();
      const w = panel?.offsetWidth ?? 300;
      const h = panel?.offsetHeight ?? 120;
      const gap = 8;
      const margin = 10;
      const above = a.bottom + gap + h > window.innerHeight && a.top - gap - h > margin;
      const rawLeft = a.left + a.width / 2 - w / 2;
      const left = Math.max(margin, Math.min(rawLeft, window.innerWidth - w - margin));
      const top = above ? a.top - gap - h : a.bottom + gap;
      setPlace({ left, top, above });
    };
    measure();
    // Scroll anywhere (capture: inner scrollers too) and resize both move the
    // anchor; re-measuring is cheaper and far less surprising than closing.
    window.addEventListener('scroll', measure, true);
    window.addEventListener('resize', measure);
    return () => {
      window.removeEventListener('scroll', measure, true);
      window.removeEventListener('resize', measure);
    };
  }, [open, anchor, panel]);

  return place;
}

export function InfoPop({
  children,
  panel,
  className,
  label,
}: {
  /** The visible trigger content. */
  children: ReactNode;
  /** The popover body. Rendered only while open. */
  panel: ReactNode;
  className?: string;
  /** Accessible name for the trigger when its text is a bare code. */
  label?: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const pinned = useRef(false);
  const anchorRef = useRef<HTMLButtonElement | null>(null);
  const [panelEl, setPanelEl] = useState<HTMLDivElement | null>(null);
  const place = usePlacement(open, anchorRef.current, panelEl);

  const close = useCallback(() => {
    pinned.current = false;
    setOpen(false);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        close();
        anchorRef.current?.focus();
      }
    };
    const onDown = (e: Event) => {
      const t = e.target as Node | null;
      if (t && (anchorRef.current?.contains(t) || panelEl?.contains(t))) return;
      close();
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onDown, true);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onDown, true);
    };
  }, [open, close, panelEl]);

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        aria-label={label}
        className={cn('text-left align-baseline', className)}
        onClick={() => {
          pinned.current = !open;
          setOpen((v) => !v);
        }}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => {
          if (!pinned.current) setOpen(false);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          if (!pinned.current) setOpen(false);
        }}
      >
        {children}
      </button>

      {open &&
        createPortal(
          <div
            id={id}
            ref={setPanelEl}
            role="tooltip"
            className="pointer-events-auto fixed z-50 w-[min(21rem,calc(100vw-1.25rem))] rounded-lg border border-line-strong bg-raised p-3 text-left shadow-pop"
            style={{
              left: place?.left ?? -9999,
              top: place?.top ?? -9999,
              visibility: place ? 'visible' : 'hidden',
            }}
            onMouseEnter={() => setOpen(true)}
            onMouseLeave={() => {
              if (!pinned.current) setOpen(false);
            }}
          >
            {panel}
          </div>,
          document.body,
        )}
    </>
  );
}

/* ------------------------------------------------------------- glossary */

/** The popover body for one glossary entry. Server text only. */
function TermPanel({ code }: { code: string }) {
  const g = useGlossary();
  const t = g.get(code);
  if (!t) {
    // A statement about the API, not a definition. If the backend adds a code
    // the glossary has not caught up with, say so rather than invent a gloss.
    return (
      <>
        <p className="font-mono text-[11px] text-muted">{code}</p>
        <p className="mt-1 text-sm text-ink2">
          {g.error ? 'The glossary could not be loaded.' : 'The server has not published a definition for this code.'}
        </p>
      </>
    );
  }
  const why = text(t.why, '');
  return (
    <>
      <p className="text-[13px] font-semibold leading-snug text-ink">{text(t.short, text(t.term, code))}</p>
      <p className="mt-1 text-[12px] leading-relaxed text-ink2">{text(t.plain, '')}</p>
      {why && (
        <div className="mt-2 border-t border-line pt-2">
          <div className="label">Why it counts</div>
          <p className="mt-0.5 text-[12px] leading-relaxed text-ink2">{why}</p>
        </div>
      )}
      <p className="mt-2 font-mono text-[10px] uppercase tracking-wide text-muted">
        {text(t.term, code)} · {text(t.group, 'term')} · from /api/glossary
      </p>
    </>
  );
}

/**
 * A label or inline word that carries a definition.
 *
 * Renders its children with a dotted underline — the conventional "there is a
 * definition here" mark, which survives greyscale and forced-colors — and
 * nothing at all beyond the plain text when the server has no entry, so an
 * undefined code never advertises help it cannot give.
 */
export function Term({
  code,
  children,
  className,
}: {
  code: unknown;
  children: ReactNode;
  className?: string;
}) {
  const g = useGlossary();
  const key = typeof code === 'string' ? code : '';
  const known = g.get(key) !== null;
  if (!key || !known) return <span className={className}>{children}</span>;
  return (
    <InfoPop className={cn('termable', className)} label={`${key} — what this means`} panel={<TermPanel code={key} />}>
      {children}
    </InfoPop>
  );
}

/**
 * A standalone ⓘ trigger.
 *
 * Exists for one structural reason: a sortable table header is already a
 * <button>, and a button inside a button is invalid HTML that browsers
 * un-nest — which silently destroys both controls. This sits BESIDE the sort
 * button as its own tab stop instead.
 */
export function TermInfo({ code, className }: { code: unknown; className?: string }) {
  const g = useGlossary();
  const key = typeof code === 'string' ? code : '';
  if (!key || !g.get(key)) return null;
  return (
    <InfoPop
      className={cn(
        'inline-flex h-[14px] w-[14px] shrink-0 items-center justify-center rounded-full border border-line-strong text-[9px] font-semibold leading-none text-muted hover:border-accent hover:text-accent-ink',
        className,
      )}
      label={`${key} — what this means`}
      panel={<TermPanel code={key} />}
    >
      <span aria-hidden="true">i</span>
    </InfoPop>
  );
}

/**
 * The server's own plain-English `short` for a code, as ordinary running text,
 * with the full entry behind the tooltip. Falls back to the raw code when the
 * glossary has no entry — never to a phrase this file made up.
 */
export function TermText({ code, className }: { code: unknown; className?: string }) {
  const g = useGlossary();
  const key = typeof code === 'string' ? code : '';
  const entry = g.get(key);
  if (!key) return null;
  if (!entry) return <span className={cn('font-mono text-[11px]', className)}>{key}</span>;
  return (
    <InfoPop className={cn('termable', className)} label={`${key} — what this means`} panel={<TermPanel code={key} />}>
      {text(entry.short, key)}
    </InfoPop>
  );
}

/**
 * A metric label (`R:R`, `n_eff`, `ATR`, `cost`…) rendered as the small caps
 * label used across the app, with its glossary entry attached.
 */
export function TermLabel({
  code,
  children,
  className,
}: {
  code: string;
  children?: ReactNode;
  className?: string;
}) {
  const g = useGlossary();
  const known = g.get(code) !== null;
  const body = children ?? code;
  if (!known) return <span className={cn('label', className)}>{body}</span>;
  return (
    <InfoPop
      // `max-w-full` plus normal wrapping: a two-word label like "ADVISED RISK"
      // in a narrow grid cell has to wrap rather than push its neighbour over.
      className={cn('label termable block max-w-full', className)}
      label={`${code} — what this means`}
      panel={<TermPanel code={code} />}
    >
      {body}
    </InfoPop>
  );
}
