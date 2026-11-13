// Compatibility re-export: the session pane moved to `session.ts` (PRD-037 WP-2). `terminalPanel` is the former name.
export { sessionPane, sessionPane as terminalPanel } from "./session";
export type { SessionPane, SessionPane as TerminalPanel, SessionPaneOpts } from "./session";
