// Adapter between the mission page and the session pane (spec 8.2). The pane's xterm.js code is a lazy chunk, so the page reserves
// the pane's box with `sessionReserve()` (src/session-box.ts) until `loadSession` resolves; both boxes have the same size.
export interface SessionOpts {
  /** The mission is running, so the pane connects read-only on arrival. */
  running: boolean;
  /** The worker is online and the stream is live. */
  available: boolean;
  /** Provider name for the composer label ("Message Codex"); omitted when unknown. */
  provider?: string;
}

export interface SessionHandle {
  element: HTMLElement;
  focusComposer(): void;
  takeControl(): void;
  setAvailable(available: boolean): void;
  /** Tell the pane the mission is (or stopped) running: a running mission is watched read-only without a click. */
  setRunning(running: boolean): void;
  dispose(): void;
}

export async function loadSession(workerName: string, missionId: string, opts: SessionOpts): Promise<SessionHandle> {
  const { sessionPane } = await import("../../session");
  const pane = sessionPane(workerName, missionId, opts.provider ? { provider: opts.provider } : {});
  let running = opts.running;
  let available = opts.available;
  // `openReadOnly` is idempotent. It is asked for on arrival and again only when the mission starts running or the worker comes back,
  // never on every refresh: an operator who chose Disconnect stays disconnected.
  const watch = (): void => {
    if (running) pane.openReadOnly();
  };
  pane.setAvailable(available);
  watch();
  return {
    element: pane.element,
    focusComposer: () => pane.focusComposer(),
    takeControl: () => pane.takeControl(),
    setAvailable: (a) => {
      const back = a && !available;
      available = a;
      pane.setAvailable(a);
      if (back) watch();
    },
    setRunning: (r) => {
      const started = r && !running;
      running = r;
      if (started) watch();
    },
    dispose: () => pane.dispose(),
  };
}
