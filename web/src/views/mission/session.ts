// Adapter between the mission page and the session pane (spec 8.2). The page only knows this interface; the pane itself is built by
// the session package. Mounting through one function keeps the merge of that package a single-line change: delete `legacyPane`
// and return `mod.sessionPane(...)` unconditionally.
import { h } from "../../dom";

export interface SessionOpts {
  /** The mission is running, so the pane connects read-only on arrival. */
  running: boolean;
  /** The worker is online and the stream is live. */
  available: boolean;
}

export interface SessionHandle {
  element: HTMLElement;
  focusComposer(): void;
  takeControl(): void;
  setAvailable(available: boolean): void;
  /** Optional: tell the pane the mission started or stopped running. */
  setRunning?(running: boolean): void;
  dispose(): void;
}

interface LegacyPanel {
  element: HTMLElement;
  setAvailable(available: boolean): void;
  openReadOnly(): void;
  dispose(): void;
}

interface PaneModule {
  sessionPane?: (workerName: string, missionId: string, opts: SessionOpts) => SessionHandle;
  terminalPanel?: (workerName: string, missionId: string) => LegacyPanel;
}

/** The xterm.js code is the bulk of the bundle: it is loaded only on this page. */
export async function loadSession(workerName: string, missionId: string, opts: SessionOpts): Promise<SessionHandle> {
  const mod = (await import("../../terminal")) as unknown as PaneModule;
  if (mod.sessionPane) return mod.sessionPane(workerName, missionId, opts);
  return legacyPane(mod.terminalPanel as NonNullable<PaneModule["terminalPanel"]>, workerName, missionId, opts);
}

/** Stand-in used until the session pane lands: wraps the previous terminal panel in the contract. */
function legacyPane(make: NonNullable<PaneModule["terminalPanel"]>, workerName: string, missionId: string, opts: SessionOpts): SessionHandle {
  const panel = make(workerName, missionId);
  let running = opts.running;
  const element = h("div", { "data-pane": "session", class: "legacy-pane" }, panel.element);
  panel.setAvailable(opts.available);
  if (running) panel.openReadOnly();
  return {
    element,
    focusComposer: () => element.querySelector<HTMLElement>("#terminal-control")?.focus(),
    takeControl: () => element.querySelector<HTMLButtonElement>("#terminal-control")?.click(),
    setAvailable: (a) => {
      panel.setAvailable(a);
      if (a && running) panel.openReadOnly();
    },
    setRunning: (r) => {
      running = r;
      if (r) panel.openReadOnly();
    },
    dispose: () => panel.dispose(),
  };
}
