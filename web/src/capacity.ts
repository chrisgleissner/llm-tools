// Capacity chips in the header (spec 7.1): the lowest-remaining scope of each provider, from `usage.snapshot` of the first online worker.
// A chip is neutral (text, a small meter) while its provider is ready; while the provider waits for capacity it takes the attention tone and says when it resets. A chip opens Usage. Below 30rem of available width only the limiting provider shows.
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
  /** The provider is not ready: it waits for capacity. */
  waiting: boolean;
  /** "14:25", the reset of the limiting scope; "" when unknown. */
  reset: string;
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
    const at = low.reset_epoch ? absTime(new Date(low.reset_epoch * 1000)).slice(11) : "";
    const reset = at ? `. Resets ${at} (${relFromMs((low.reset_epoch as number) * 1000, now)})` : "";
    out.push({ provider: label(p.provider), scope: low.scope, percent, title: `${label(p.provider)} ${low.scope}: ${percent}% left${reset}`, waiting: p.ready === false, reset: at });
  }
  return out.sort((a, b) => a.percent - b.percent);
}

export function capacityChips(): { el: HTMLElement; dispose: () => void } {
  const el = h("div", { class: "cap-chips", id: "capacity", role: "group", "aria-label": "Provider capacity" });
  let chips: Chip[] = [];
  let narrow = false;
  let worker = "";
  let timer = 0;
  let first_read = 0;
  let disposed = false;

  // When the chips do not fit they lose their scope and meter (class "tight"; the scope stays for screen readers).
  // The container clips on its start edge, where scrollWidth shows nothing, so the first chip's position tells.
  const paint = (): void => {
    el.classList.remove("tight");
    el.replaceChildren(
      ...(narrow ? chips.slice(0, 1) : chips).map((c) =>
        h("a", { class: `cap-chip${c.waiting ? " waiting" : ""}`, href: "/usage", title: c.title }, h("span", {}, c.provider, h("span", { class: "cap-scope" }, ` ${c.scope}`)), h("span", { class: "cap-meter", "aria-hidden": "true", style: `--v:${Math.max(0, Math.min(100, c.percent))}%` }, h("i")), h("span", {}, h("span", { class: "cap-pct" }, ` ${c.percent}% left${c.waiting && c.reset ? "," : ""}`), c.waiting && c.reset ? h("span", {}, ` resets ${c.reset}`) : null)),
      ),
    );
    el.classList.toggle("tight", (el.firstElementChild?.getBoundingClientRect().left ?? 0) < el.getBoundingClientRect().left - 0.5);
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
      // The read goes to a worker and takes a round trip: it starts after the page has been painted, not before (LCP, T37).
      window.clearTimeout(first_read);
      if (first) first_read = window.setTimeout(() => void load(), 400);
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
    narrow = (entry?.contentRect.width ?? 0) < NARROW_REM * rem;
    paint();
  });
  watch.observe(el);
  sync();
  return {
    el,
    dispose: () => {
      disposed = true;
      window.clearTimeout(first_read);
      unsubscribe();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
      watch.disconnect();
    },
  };
}
