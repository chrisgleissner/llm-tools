// Two notifications besides "needs you" (PRD-018): a worker that has been offline for the configured time, and a rate limit that has lifted.
// Each shows a toast that stays until dismissed (with a button to the place to act) and, while the tab is hidden and permission is granted, a browser notification.
// A separate chunk, loaded once the shell is up, so the first load does not carry it (budget.json).
import * as api from "./api";
import { store, subscribe } from "./store";
import { announce, say } from "./ui";

export const OFFLINE_MINUTES_CHOICES = [1, 5, 15, 30, 60];
const OFFLINE_KEY = "bridge-notify-offline-min";
const NOTIFY_GAP_MS = 5 * 60_000;

/** How long a worker may be offline before the console says so: a per-viewer setting kept like the theme, 5 minutes by default. */
export function offlineAlertMinutes(): number {
  let n = NaN;
  try {
    n = Number(localStorage.getItem(OFFLINE_KEY));
  } catch {
    /* storage unavailable: the default */
  }
  return Number.isInteger(n) && n >= 1 && n <= 1440 ? n : 5;
}
export function cycleOfflineAlert(): number {
  const next = OFFLINE_MINUTES_CHOICES[(OFFLINE_MINUTES_CHOICES.indexOf(offlineAlertMinutes()) + 1) % OFFLINE_MINUTES_CHOICES.length] ?? 5;
  try {
    if (next === 5) localStorage.removeItem(OFFLINE_KEY);
    else localStorage.setItem(OFFLINE_KEY, String(next));
  } catch {
    /* the choice lasts for this page only */
  }
  say(`Worker offline alert after ${next} minutes`);
  return next;
}

/** Workers that have been offline for at least `minutes`, by last contact. A worker that never connected (no `last_seen`) is not listed: nothing says how long it has been gone. */
export function offlineLongerThan(workers: api.WorkerInfo[], minutes: number, now = Date.now()): Array<{ worker: api.WorkerInfo; minutes: number }> {
  const out: Array<{ worker: api.WorkerInfo; minutes: number }> = [];
  for (const w of workers) {
    if (w.state !== "offline" || !w.last_seen) continue;
    const gone = Math.floor((now - w.last_seen * 1000) / 60_000);
    if (gone >= minutes) out.push({ worker: w, minutes: gone });
  }
  return out;
}

/** Providers that waited for capacity at the previous read (`prev`, provider to waiting) and are ready now: their rate limit has lifted. Updates `prev`. */
export function liftedLimits(prev: Map<string, boolean>, providers: Array<{ provider: string; ready?: boolean; available?: boolean }>, target: string): string[] {
  const lifted: string[] = [];
  for (const p of providers) {
    if (p.available === false) continue;
    const waiting = p.ready === false;
    const key = `${target}/${p.provider}`;
    if (prev.get(key) === true && !waiting) lifted.push(p.provider);
    prev.set(key, waiting);
  }
  return lifted;
}

const COOLDOWN_MS = 30 * 60_000;
const CLAIM_PREFIX = "bridge-alert:";
const memoryClaims = new Map<string, number>();

/**
 * True when this tab may announce `key` now: nothing was announced for it in the last 30 minutes, in this tab or in another tab of this browser.
 * The claim is a `localStorage` entry (`time:token`) that is read back after it is written, so when two tabs claim at the same moment only the
 * one whose token stayed announces. Without storage the claim is kept in memory, which still gives the cooldown for this tab.
 */
export function claim(key: string, now = Date.now()): boolean {
  const mine = `${now}:${Math.random().toString(36).slice(2)}`;
  try {
    const seen = Number((localStorage.getItem(CLAIM_PREFIX + key) ?? "").split(":")[0]);
    if (Number.isFinite(seen) && seen > 0 && now - seen < COOLDOWN_MS) return false;
    localStorage.setItem(CLAIM_PREFIX + key, mine);
    return localStorage.getItem(CLAIM_PREFIX + key) === mine;
  } catch {
    const seen = memoryClaims.get(key) ?? 0;
    if (seen > 0 && now - seen < COOLDOWN_MS) return false;
    memoryClaims.set(key, now);
    return true;
  }
}

export function mountAlerts(go: (path: string) => void): () => void {
  const notified = new Map<string, number>();
  const alerted = new Set<string>();
  let baselined = false;
  const notice = (key: string, text: string, label: string, path: string): void => {
    if (!claim(key)) return;
    announce(text, "info", "", { action: { label, run: () => go(path) } });
    if ("Notification" in window && Notification.permission === "granted" && document.hidden && Date.now() - (notified.get(key) ?? 0) >= NOTIFY_GAP_MS) {
      notified.set(key, Date.now());
      try {
        const note = new Notification(text, { tag: key });
        note.onclick = () => (window.focus(), go(path), note.close());
      } catch {
        /* the browser refused: the toast still shows */
      }
    }
  };
  const check = (): void => {
    if (!store.loaded || store.conn !== "live") return;
    // Workers that are already offline when the console opens are not announced (the Workers page shows them): the alert is for one that drops out while the console is open.
    if (!baselined) {
      baselined = true;
      for (const w of store.workers) if (w.state === "offline") alerted.add(w.name);
      return;
    }
    // A worker that came back, or was removed, may be announced again the next time it stays offline.
    for (const n of [...alerted]) if (!store.workers.some((w) => w.name === n && w.state === "offline")) alerted.delete(n);
    for (const { worker: w, minutes } of offlineLongerThan(store.workers, offlineAlertMinutes())) {
      if (alerted.has(w.name)) continue;
      alerted.add(w.name);
      notice(`worker/${w.name}`, `Worker ${w.name} has been offline for ${minutes} min.`, "Open workers", "/workers");
    }
  };
  // The previous state is kept per worker and provider: a read from another worker (the chips read the first online one) is not a change of the first.
  const waiting = new Map<string, boolean>();
  const unUsage = api.onUsageSnapshot((snapshot, target) => {
    for (const p of liftedLimits(waiting, snapshot.providers ?? [], target)) notice(`limit/${p}`, `${p.charAt(0).toUpperCase()}${p.slice(1)} has capacity again. The rate limit has lifted.`, "Open usage", "/usage");
  });
  const unStore = subscribe(check);
  const timer = window.setInterval(check, 30_000);
  check();
  return () => {
    unUsage();
    unStore();
    window.clearInterval(timer);
  };
}
