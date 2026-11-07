import * as api from "./api";
import type { MissionEntry, WorkerInfo } from "./api";
import { deriveState, type ConnState, type DerivedState } from "./state";

export type { ConnState };

type Listener = () => void;

export const store = {
  workers: [] as WorkerInfo[],
  missions: [] as MissionEntry[],
  conn: "offline" as ConnState,
  loaded: false,
  /** Epoch milliseconds of the last data from the coordinator (a refresh or a stream message); 0 before the first. Drives "as of 14:02 (3m ago)". */
  lastSync: 0,
};

const listeners = new Set<Listener>();
const streamListeners = new Set<(msg: StreamMessage) => void>();

export interface StreamMessage {
  type: "worker" | "event" | "command" | "terminal";
  worker?: string;
  state?: string;
  cursor?: number;
  events?: Array<Record<string, unknown>>;
  command?: api.CommandRow;
  mission_id?: string;
  mode?: string;
  reason?: string;
}

export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
export function onStream(fn: (msg: StreamMessage) => void): () => void {
  streamListeners.add(fn);
  return () => streamListeners.delete(fn);
}
export function publishStreamMessage(msg: StreamMessage): void {
  for (const listener of [...streamListeners]) listener(msg);
}
function emit(): void {
  for (const l of [...listeners]) l();
}

export function worker(name: string): WorkerInfo | undefined {
  return store.workers.find((w) => w.name === name);
}

// The last state shown for each mission and worker while the stream was live. While the stream is down the page keeps these:
// it does not recompute Offline from data it can no longer refresh (spec 5.1).
const lastMissionState = new Map<string, DerivedState>();
const lastWorkerState = new Map<string, DerivedState>();

/**
 * The single state of a mission. `extra` carries what the status projection adds beyond the mission list (`needs_you` with
 * its task numbers, `task_counts`). The coordinator's replication flag (`stale`) is ignored.
 */
export function missionState(m: MissionEntry, extra: { needsYou?: api.NeedsYou | null; taskCounts?: Record<string, number> | null } = {}): DerivedState {
  const key = `${m.worker}/${m.mission.id}`;
  const w = worker(m.worker);
  const state = deriveState({
    entity: "mission",
    status: m.mission.status,
    needsYou: extra.needsYou ?? m.mission.needs_you ?? null,
    taskCounts: extra.taskCounts ?? null,
    worker: w ? { state: w.state, lastSeen: w.last_seen } : null,
    conn: store.conn,
  });
  if (store.conn === "live") {
    lastMissionState.set(key, state);
    return state;
  }
  return lastMissionState.get(key) ?? state;
}

/** Worker liveness as a state (Online or Offline). While the stream is down it is the last state shown. */
export function workerDerived(w: WorkerInfo): DerivedState {
  const state = deriveState({ entity: "worker", status: w.state, worker: { state: w.state, lastSeen: w.last_seen }, conn: store.conn });
  if (store.conn === "live") {
    lastWorkerState.set(w.name, state);
    return state;
  }
  return lastWorkerState.get(w.name) ?? state;
}

/** Worker state as reported by the coordinator: `online` or `offline`. */
export function workerState(w: WorkerInfo): "online" | "offline" {
  return w.state;
}

export async function refresh(): Promise<void> {
  const [w, m] = await Promise.all([api.get<{ workers: WorkerInfo[] }>("/api/v1/workers"), api.get<{ missions: MissionEntry[] }>("/api/v1/missions")]);
  const changed = !store.loaded || JSON.stringify(w.workers) !== JSON.stringify(store.workers) || JSON.stringify(m.missions) !== JSON.stringify(store.missions);
  store.workers = w.workers;
  store.missions = m.missions;
  store.loaded = true;
  store.lastSync = Date.now();
  if (changed) emit();
}

let ws: WebSocket | null = null;
let timer: number | undefined;
let poll: number | undefined;
let attempts = 0;
let wanted = false;
let refreshTimer: number | undefined;

function setConn(c: ConnState): void {
  if (store.conn === c) return;
  store.conn = c;
  emit();
}

function scheduleRefresh(): void {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => void refresh().catch(() => {}), 400);
}

function connect(): void {
  if (!wanted) return;
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/stream`;
  const sock = new WebSocket(url);
  ws = sock;
  sock.onopen = () => {
    attempts = 0;
    setConn("live");
    void refresh().catch(() => {});
  };
  sock.onmessage = (ev) => {
    let msg: StreamMessage;
    try {
      msg = JSON.parse(String(ev.data)) as StreamMessage;
    } catch {
      return;
    }
    store.lastSync = Date.now();
    if (msg.type === "worker" && msg.worker) {
      const w = worker(msg.worker);
      if (w && (msg.state === "online" || msg.state === "offline")) {
        w.state = msg.state;
        emit();
      }
      scheduleRefresh();
    } else if (msg.type === "event") {
      scheduleRefresh();
    }
    publishStreamMessage(msg);
  };
  sock.onclose = () => {
    if (ws !== sock) return;
    ws = null;
    if (!wanted) return;
    attempts += 1;
    setConn(attempts > 4 || navigator.onLine === false ? "offline" : "reconnecting");
    timer = window.setTimeout(connect, Math.min(15000, 500 * 2 ** attempts));
    void api.authState().then((s) => !s.authenticated && api.onUnauthorizedNow()).catch(() => {});
  };
}

export function startLive(): void {
  if (wanted) return;
  wanted = true;
  connect();
  poll = window.setInterval(() => void refresh().catch(() => {}), 30000);
  window.addEventListener("online", reconnectNow);
}

function reconnectNow(): void {
  if (!wanted || ws) return;
  window.clearTimeout(timer);
  connect();
}

export function stopLive(): void {
  wanted = false;
  window.clearTimeout(timer);
  window.clearTimeout(refreshTimer);
  window.clearInterval(poll);
  window.removeEventListener("online", reconnectNow);
  ws?.close();
  ws = null;
  attempts = 0;
  store.conn = "offline";
  store.loaded = false;
  store.lastSync = 0;
  lastMissionState.clear();
  lastWorkerState.clear();
  store.workers = [];
  store.missions = [];
  emit();
}
