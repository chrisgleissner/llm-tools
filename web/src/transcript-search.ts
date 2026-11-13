// Search in the scrollback transcript of the session pane (PRD-015). Pure (no DOM), so it can be tested without a browser.
export interface Range {
  start: number;
  end: number;
}

/** Matches kept at most: a one-letter search in a long buffer must not build thousands of highlight nodes. */
export const MAX_MATCHES = 500;

/** Case-insensitive, non-overlapping matches of `query` in `text`, in order. An empty query has none. */
export function findMatches(text: string, query: string): Range[] {
  if (!query) return [];
  const hay = text.toLowerCase();
  const needle = query.toLowerCase();
  const out: Range[] = [];
  for (let at = hay.indexOf(needle); at >= 0 && out.length < MAX_MATCHES; at = hay.indexOf(needle, at + needle.length)) out.push({ start: at, end: at + needle.length });
  return out;
}

/** The index after stepping `by` (1 or -1) from `current` among `total` matches, wrapping at both ends; 0 when there is none. */
export const stepMatch = (current: number, by: 1 | -1, total: number): number => (total === 0 ? 0 : (current + by + total) % total);

/** "3 of 12", "No matches", or "" while nothing is searched for. */
export const matchLabel = (query: string, current: number, total: number): string => (query ? (total ? `${current + 1} of ${total}${total >= MAX_MATCHES ? "+" : ""}` : "No matches") : "");
