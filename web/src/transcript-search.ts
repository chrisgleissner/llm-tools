// Search in the scrollback transcript of the session pane (PRD-015). Pure (no DOM), so it can be tested without a browser.
export interface Range {
  start: number;
  end: number;
}

/** Matches kept at most: a one-letter search in a long buffer must not build thousands of highlight nodes. */
export const MAX_MATCHES = 500;

/** Lower case of one character. Folding the whole string instead could change its length ("İ" becomes two code units) and shift every later offset. */
const fold = (c: string): string => c.toLowerCase();

/** Case-insensitive, non-overlapping matches of `query` in `text`, in order. Offsets are positions in `text` itself. An empty query has none. */
export function findMatches(text: string, query: string): Range[] {
  if (!query) return [];
  const chars = Array.from(text);
  const offsets: number[] = [];
  let at = 0;
  for (const c of chars) {
    offsets.push(at);
    at += c.length;
  }
  offsets.push(at);
  const folded = chars.map(fold);
  const needle = Array.from(query).map(fold);
  const out: Range[] = [];
  for (let i = 0; i + needle.length <= chars.length && out.length < MAX_MATCHES; i += 1) {
    let j = 0;
    while (j < needle.length && folded[i + j] === needle[j]) j += 1;
    if (j === needle.length) {
      out.push({ start: offsets[i]!, end: offsets[i + needle.length]! });
      i += needle.length - 1;
    }
  }
  return out;
}

/** The index after stepping `by` (1 or -1) from `current` among `total` matches, wrapping at both ends; 0 when there is none. */
export const stepMatch = (current: number, by: 1 | -1, total: number): number => (total === 0 ? 0 : (current + by + total) % total);

/** "3 of 12", "No matches", or "" while nothing is searched for. */
export const matchLabel = (query: string, current: number, total: number): string => (query ? (total ? `${current + 1} of ${total}${total >= MAX_MATCHES ? "+" : ""}` : "No matches") : "");
