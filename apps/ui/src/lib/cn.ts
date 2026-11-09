/**
 * Class-name join.
 *
 * It lives in its own module rather than in components/primitives because
 * primitives now renders a glossary tooltip and Tooltip needs `cn` — importing
 * it from primitives would make the two files a cycle. One four-line module is
 * cheaper than reasoning about ESM hoisting order every time either is edited.
 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}
