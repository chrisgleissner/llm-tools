import * as api from "./api";
import type { MissionEntry, WorkerInfo } from "./api";

export type ConnState = "live" | "reconnecting" | "offline";

type Listener = () => void;

export const store = {
  workers: [] as WorkerInfo[],
  missions: [] as MissionEntry[],
  conn: "offline" as ConnState,
  loaded: false,
};

const listeners = new Set<Listener>();
const streamListeners = new Set<(msg: StreamMessage) => void>();

export interface StreamMessage {
  type: "worker" | "event" | "command" | "terminal";
  worker?: string;
  state?: string;
  cursor?: number;
  events?: Array<Record<string, unknown>>;
  task_missions?: string[];
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

/** A mission is stale when the coordinator says so or its worker is not online right now. */
export function isStale(m: MissionEntry): boolean {
  return store.conn !== "live" || m.stale || worker(m.worker)?.state !== "online";
}

/** Worker state as shown: while the stream is down the coordinator's view is unknown, so it reads as stale. */
export function workerState(w: WorkerInfo): "online" | "offline" | "stale" {
  if (store.conn !== "live" && w.state === "online") return "stale";
  return w.state;
}

export async function refresh(): Promise<void> {
  const [w, m] = await Promise.all([api.get<{ workers: WorkerInfo[] }>("/api/v1/workers"), api.get<{ missions: MissionEntry[] }>("/api/v1/missions")]);
  const changed = !store.loaded || JSON.stringify(w.workers) !== JSON.stringify(store.workers) || JSON.stringify(m.missions) !== JSON.stringify(store.missions);
  store.workers = w.workers;
  store.missions = m.missions;
  store.loaded = true;
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
    // Mark every worker stale while the coordinator cannot be reached: its view of them is unknown.
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
  store.workers = [];
  store.missions = [];
  emit();
}
