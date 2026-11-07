// Capacity chips in the header (spec 7.1): the lowest-remaining scope of each provider, from `usage.snapshot` of the first online worker.
// Chips are always neutral (text, a small meter, no attention colour) and open Usage. Below 30rem of available width only the limiting provider shows.
import * as api from "./api";
import { absTime, h, relFromMs } from "./dom";
import { store, subscribe } from "./store";

const REFRESH_MS = 60_000;
const NARROW_REM = 30;

interface Chip {
  provider: string;
  scope: string;
  percent: number;
  title: string;
}

const label = (id: string): string => id.charAt(0).toUpperCase() + id.slice(1);

/** One chip per available provider, the one with the least capacity left first. Exported for tests of the selection rule. */
export function chipsFrom(snapshot: api.UsageSnapshot | null, now = Date.now()): Chip[] {
  const out: Chip[] = [];
  for (const p of snapshot?.providers ?? []) {
    if (!p.available) continue;
    const scopes = p.scopes.filter((s) => s.remaining_percent !== null && s.remaining_percent !== undefined);
    const low = scopes.reduce<api.UsageScope | null>((a, s) => (a === null || (s.remaining_percent as number) < (a.remaining_percent as number) ? s : a), null);
    if (!low) continue;
    const percent = Math.round(low.remaining_percent as number);
    const reset = low.reset_epoch ? `. Resets ${absTime(new Date(low.reset_epoch * 1000)).slice(11)} (${relFromMs(low.reset_epoch * 1000, now)})` : "";
    out.push({ provider: label(p.provider), scope: low.scope, percent, title: `${label(p.provider)} ${low.scope}: ${percent}% left${reset}` });
  }
  return out.sort((a, b) => a.percent - b.percent);
}

export function capacityChips(): { el: HTMLElement; dispose: () => void } {
  const el = h("div", { class: "cap-chips", id: "capacity", role: "group", "aria-label": "Provider capacity" });
  let chips: Chip[] = [];
  let narrow = false;
  let worker = "";
  let timer = 0;
  let disposed = false;

  const paint = (): void => {
    const shown = narrow ? chips.slice(0, 1) : chips;
    el.replaceChildren(
      ...shown.map((c) =>
        h("a", { class: "cap-chip", href: "/usage", title: c.title }, c.provider, " ", c.scope, " ", h("span", { class: "meter", "aria-hidden": "true", style: `--v:${Math.max(0, Math.min(100, c.percent))}%` }, h("i")), " ", `${c.percent}%`, h("span", { class: "sr-only" }, " left")),
      ),
    );
  };

  const load = async (): Promise<void> => {
    const name = worker;
    if (!name) return;
    try {
      const snapshot = await api.usageSnapshot(name);
      if (disposed || name !== worker) return;
      chips = chipsFrom(snapshot);
      paint();
    } catch {
      /* keep the last chips: a failed read is not news */
    }
  };

  const sync = (): void => {
    const first = store.workers.find((w) => w.state === "online")?.name ?? "";
    if (first !== worker) {
      worker = first;
      if (first) void load();
    }
  };
  const tick = (): void => {
    if (document.visibilityState === "visible" && store.conn === "live") void load();
  };
  const unsubscribe = subscribe(sync);
  timer = window.setInterval(tick, REFRESH_MS);
  document.addEventListener("visibilitychange", tick);
  const watch = new ResizeObserver(([entry]) => {
    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    const next = (entry?.contentRect.width ?? 0) < NARROW_REM * rem;
    if (next !== narrow) {
      narrow = next;
      paint();
    }
  });
  watch.observe(el);
  sync();
  return {
    el,
    dispose: () => {
      disposed = true;
      unsubscribe();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
      watch.disconnect();
    },
  };
}
