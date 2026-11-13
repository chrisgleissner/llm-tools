// The grouped, windowed active list of the Missions page (PRD-014). It is a lazy chunk: a short list is drawn by the page itself, and this module is
// loaded the first time the list has enough missions to need headings or a window.
import type { h as hType } from "./dom";
import { groupOf, withHeadings, type Flat } from "./grouping";
import type { Item, reconcile as reconcileType, RowSpec } from "./rows";
import { windowSlice } from "./virtual";

export interface MissionWindow {
  /** Show these missions (in display order); `headings` adds a heading before each group. */
  set(active: Item[], headings: boolean): void;
  dispose(): void;
}

const ROW_GUESS = 48;

/** `threshold`: a list of at most this many entries is built whole; a longer one is a window of rows with spacers for the rest, so the list holds a bounded number of nodes. */
export interface WindowEnv {
  list: HTMLElement;
  specs: (items: Item[]) => RowSpec[];
  threshold: number;
  stopped: () => boolean;
  /** `h` and `reconcile` come from the page: importing them here would make esbuild split the shared modules into two chunks of the first load (about 560 bytes gzip more). */
  h: typeof hType;
  reconcile: typeof reconcileType;
}

export function missionWindow({ list, specs, threshold, stopped, h, reconcile }: WindowEnv): MissionWindow {
  let flat: Flat<Item>[] = [];
  let rowH = 0;
  let painted = "";
  let frame = 0;

  function paint(force = false): void {
    const step = rowH || ROW_GUESS;
    const gap = parseFloat(getComputedStyle(list).rowGap) || 0;
    const slice = windowSlice(flat.length, step, -list.getBoundingClientRect().top, window.innerHeight, 8, threshold);
    const mark = `${slice.start}-${slice.end}-${step}-${flat.length}`;
    if (!force && mark === painted) return;
    painted = mark;
    const space = (where: string, px: number): RowSpec => ({
      key: `~${where}`,
      sig: String(px),
      build: () => h("li", { class: "vspace", "aria-hidden": "true", style: `block-size:${Math.max(0, px - gap)}px` }),
    });
    const out: RowSpec[] = [];
    if (slice.before) out.push(space("before", slice.before));
    for (const e of flat.slice(slice.start, slice.end)) {
      if (e.kind === "row") out.push(...specs([e.item]));
      else
        out.push({
          key: `~${e.group}`,
          sig: `${e.title}|${e.count}|${step}`,
          build: () => h("li", { class: "mgroup", "data-group": e.group, style: `display:flex;align-items:flex-end;padding-inline:var(--sp-2);block-size:${Math.max(0, step - gap)}px` }, h("h2", { style: "margin:0;font-size:var(--fs-sm);font-weight:600;color:var(--text-muted)" }, e.title, h("span", { style: "font-weight:400" }, ` (${e.count})`))),
        });
    }
    if (slice.after) out.push(space("after", slice.after));
    reconcile(list, out);
    // The window is sized from the measured height of a row; a different height than the guess redraws once.
    const rows = list.querySelectorAll<HTMLElement>(".mrow");
    if (!rows.length) return;
    const measured = Math.round(Array.from(rows).reduce((n, r) => n + r.offsetHeight, 0) / rows.length + gap);
    if (measured > 0 && Math.abs(measured - step) >= 2 && flat.length > threshold) {
      rowH = measured;
      paint(true);
    } else if (measured > 0) rowH ||= measured;
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
