// Window arithmetic of a virtualised list (PRD-014). Pure (no DOM): given how many entries there are, how tall one is and which part of the list is
// on screen, say which entries to build and how much space stands in for the others.
export interface Slice {
  start: number;
  end: number;
  /** Pixels that stand in for the entries before `start`. */
  before: number;
  /** Pixels that stand in for the entries from `end`. */
  after: number;
}

/**
 * `viewTop` and `viewHeight` are measured inside the list's full height. `overscan` extra entries are built on each side so a fast scroll does not show a gap.
 * A list of `count <= threshold` entries is built whole.
 */
export function windowSlice(count: number, rowHeight: number, viewTop: number, viewHeight: number, overscan = 8, threshold = 60): Slice {
  if (count <= threshold || rowHeight <= 0) return { start: 0, end: count, before: 0, after: 0 };
  const top = Math.max(0, viewTop);
  const first = Math.floor(top / rowHeight);
  const last = Math.ceil((top + Math.max(0, viewHeight)) / rowHeight);
  const start = Math.min(Math.max(0, first - overscan), count);
  const end = Math.min(count, Math.max(start, last + overscan));
  return { start, end, before: start * rowHeight, after: (count - end) * rowHeight };
}
