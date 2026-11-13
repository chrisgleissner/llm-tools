// The grouped, windowed active list of the Missions page (PRD-014). It is a lazy chunk: a short list is drawn by the page itself, and this module is
// loaded the first time the list has enough missions to need headings or a window.
import type { h as hType } from "./dom";
import { groupOf, withHeadings, type Flat } from "./grouping";
import type { Item, reconcile as reconcileType, RowSpec } from "./rows";
import { windowByHeights } from "./virtual";

export interface MissionWindow {
  /** Show these missions (in display order); `headings` adds a heading before each group. */
  set(active: Item[], headings: boolean): void;
  dispose(): void;
}

export interface WindowEnv {
  list: HTMLElement;
  specs: (items: Item[]) => RowSpec[];
  threshold: number;
  stopped: () => boolean;
  /** `h` and `reconcile` come from the page: importing them here would make esbuild split the shared modules into two chunks of the first load (about 560 bytes gzip more). */
  h: typeof hType;
  reconcile: typeof reconcileType;
}

const ROW_GUESS = 48;
const HEAD_HEIGHT = 24;

/** `threshold`: a list of at most this many entries is built whole; a longer one is a window of rows with spacers for the rest, so the list holds a bounded number of nodes. */
export function missionWindow({ list, specs, threshold, stopped, h, reconcile }: WindowEnv): MissionWindow {
  let flat: Flat<Item>[] = [];
  let painted = "";
  let frame = 0;
  /** Measured height (gap included) of each row by its key; a row that was never drawn is estimated from the average of those that were. */
  const heights = new Map<string, number>();
  const keyOf = (e: Flat<Item>): string => (e.kind === "row" ? e.item.key : `~${e.group}`);

  function paint(force = false): void {
    const gap = parseFloat(getComputedStyle(list).rowGap) || 0;
    const known = [...heights.values()];
    const guess = known.length ? known.reduce((a, b) => a + b, 0) / known.length : ROW_GUESS;
    const all = flat.map((e) => (e.kind === "head" ? HEAD_HEIGHT + gap : (heights.get(e.item.key) ?? guess)));
    // The row that has the focus stays in the DOM wherever it is, so scrolling never drops it.
    const focus = document.activeElement as HTMLElement | null;
    const focused = focus && list.contains(focus) ? focus.closest<HTMLElement>("[data-row-key]")?.dataset["rowKey"] : undefined;
    const pinned = focused ? flat.findIndex((e) => keyOf(e) === focused) : -1;
    const slice = windowByHeights(all, -list.getBoundingClientRect().top, window.innerHeight, window.innerHeight / 2, threshold);
    const mark = `${slice.start}-${slice.end}-${pinned}-${flat.length}-${all.join(",")}`;
    if (!force && mark === painted) return;
    painted = mark;
    const rowNo = new Map<string, number>();
    for (const e of flat) if (e.kind === "row") rowNo.set(e.item.key, rowNo.size + 1);
    const space = (where: string, px: number): RowSpec => ({
      key: `~${where}`,
      sig: String(px),
      build: () => h("li", { class: "vspace", "aria-hidden": "true", style: `block-size:${Math.max(0, px - gap)}px` }),
    });
    const sum = (a: number, b: number): number => all.slice(a, b).reduce((x, y) => x + y, 0);
    const out: RowSpec[] = [];
    const run = (a: number, b: number): void => {
      for (let i = a; i < b; i++) {
        const e = flat[i]!;
        if (e.kind === "row") out.push(...specs([e.item]));
        else
          out.push({
            key: `~${e.group}`,
            sig: `${e.title}|${e.count}`,
            build: () => h("li", { class: "mgroup", "data-group": e.group, style: `display:flex;align-items:flex-end;padding-inline:var(--sp-2);block-size:${HEAD_HEIGHT}px` }, h("h2", { style: "margin:0;font-size:var(--fs-sm);font-weight:600;color:var(--text-muted)" }, e.title, h("span", { style: "font-weight:400" }, ` (${e.count})`))),
          });
      }
    };
    if (pinned >= 0 && pinned < slice.start) {
      if (pinned) out.push(space("before", sum(0, pinned)));
      run(pinned, pinned + 1);
      out.push(space("gap", sum(pinned + 1, slice.start)));
      run(slice.start, slice.end);
      if (slice.after) out.push(space("after", slice.after));
    } else if (pinned >= slice.end) {
      if (slice.before) out.push(space("before", slice.before));
      run(slice.start, slice.end);
      out.push(space("gap", sum(slice.end, pinned)));
      run(pinned, pinned + 1);
      if (pinned + 1 < flat.length) out.push(space("after", sum(pinned + 1, flat.length)));
    } else {
      if (slice.before) out.push(space("before", slice.before));
      run(slice.start, slice.end);
      if (slice.after) out.push(space("after", slice.after));
    }
    reconcile(list, out);
    // Each row is announced as one of the whole list although most are not in the DOM, and its height is kept by key.
    let changed = false;
    for (const li of list.querySelectorAll<HTMLElement>(".mrow[data-row-key]")) {
      const key = li.dataset["rowKey"]!;
      li.setAttribute("aria-setsize", String(rowNo.size));
      li.setAttribute("aria-posinset", String(rowNo.get(key) ?? 0));
      const measured = li.offsetHeight + gap;
      if (li.offsetHeight > 0 && Math.abs((heights.get(key) ?? 0) - measured) >= 2) {
        heights.set(key, measured);
        changed = true;
      }
    }
    if (changed && !force) paint(true);
  }

  const onScroll = (): void => {
    if (frame || flat.length <= threshold) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!stopped()) paint();
    });
  };
  document.addEventListener("scroll", onScroll, { capture: true, passive: true });
  window.addEventListener("resize", onScroll);

  return {
    set(active, headings) {
      flat = headings ? withHeadings(active, (i) => groupOf(i.state)) : active.map((item) => ({ kind: "row" as const, item }));
      paint(true);
    },
    dispose() {
      cancelAnimationFrame(frame);
      document.removeEventListener("scroll", onScroll, { capture: true });
      window.removeEventListener("resize", onScroll);
    },
  };
}
