/** @type {import('tailwindcss').Config} */

/**
 * Tokens are "R G B" triples in src/index.css. Wrapping them as
 * `rgb(var(--token) / <alpha-value>)` is what makes `bg-good/10` and
 * `border-warning/50` actually apply their alpha — a token written plainly as
 * `var(--token)` cannot be parsed by Tailwind, so the opacity modifier is
 * silently dropped and every tinted surface renders fully saturated.
 *
 * Note the `*Ink` colours. They are the same hue as the mark beside them,
 * re-stepped until they clear 4.5:1 as TEXT on the card surface. A fill and a
 * label are different jobs with different thresholds; giving them one token
 * forces a choice between an illegible label and a washed-out mark.
 */
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        page: token('page'),
        surface: token('surface-1'),
        sunken: token('surface-2'),
        raised: token('surface-3'),
        ink: token('ink'),
        ink2: token('ink-2'),
        muted: token('ink-muted'),
        grid: token('grid'),
        axis: token('axis'),
        line: token('line'),
        'line-strong': token('line-strong'),
        // Kept as an alias so every existing `border-hairline` keeps working;
        // it now resolves to a real ramp step rather than 10% black.
        hairline: token('line'),
        accent: token('accent'),
        'accent-ink': token('accent-ink'),
        'accent-soft': token('accent-soft'),
        rail: token('rail'),
        'rail-2': token('rail-2'),
        'rail-active': token('rail-active'),
        'rail-ink': token('rail-ink'),
        'rail-muted': token('rail-muted'),
        good: token('status-good'),
        'good-ink': token('good-ink'),
        'warning-ink': token('warning-ink'),
        warning: token('status-warning'),
        serious: token('status-serious'),
        critical: token('status-critical'),
        'critical-ink': token('critical-ink'),
        pos: token('pole-pos'),
        neg: token('pole-neg'),
        'pos-ink': token('pos-ink'),
        'neg-ink': token('neg-ink'),
        s1: token('series-1'),
        s2: token('series-2'),
        s3: token('series-3'),
        's2-ink': token('s2-ink'),
        's3-ink': token('s3-ink'),
        'buy-fill': token('buy-fill'),
        'sell-fill': token('sell-fill'),
      },
      fontFamily: {
        sans: ['system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      fontSize: {
        // The hero figure: >=48px, same sans as everything else.
        hero: ['3rem', { lineHeight: '1.05', letterSpacing: '-0.02em' }],
        // The KPI tile figure, per the reference: ~36px, tight.
        kpi: ['2.25rem', { lineHeight: '1.08', letterSpacing: '-0.025em' }],
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        raise: 'var(--shadow-raise)',
        pop: 'var(--shadow-pop)',
      },
    },
  },
  plugins: [],
};
