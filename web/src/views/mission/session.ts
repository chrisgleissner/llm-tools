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
  // `openReadOnly` is idempotent, and a pane that was unavailable when it was asked to connect needs asking again once the worker is back.
  const watch = (): void => {
    if (running) pane.openReadOnly();
  };
  pane.setAvailable(opts.available);
  watch();
  return {
    element: pane.element,
    focusComposer: () => pane.focusComposer(),
    takeControl: () => pane.takeControl(),
    setAvailable: (a) => {
      pane.setAvailable(a);
      if (a) watch();
    },
    setRunning: (r) => {
      running = r;
      watch();
    },
    dispose: () => pane.dispose(),
  };
}
