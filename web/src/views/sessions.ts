import * as api from "../api";
import { disclosure, emptyState, h, idChip, pager, patch, statePill } from "../dom";
import { deriveState, type DerivedState } from "../state";
import { ago, calm, cap, el, facts, fleetRow, fleetView, plural, rowLink, type Answer, type FleetRender } from "./remote";

interface Session {
  operation_id: string;
  mission_id: string;
  mission_name: string | null;
  task_ordinal: number | null;
  provider: string;
  route_id: string | null;
  model: string | null;
  state: string;
  restoration_state: string;
  native_session_id: string | null;
  started_at: string;
  last_activity_at: string;
}

interface Row extends Session {
  worker: string;
}

const PAGE = 20;

/** Words for the restoration state instead of the stored `native_created` (spec 8.5). */
const RESTORATION: Record<string, string> = {
  NATIVE_CREATED: "Created natively by the provider",
  NATIVE_RESUMED: "Resumed natively",
  NATIVE_UNKNOWN: "Provider session not identified",
  FRESH: "Started fresh",
};

function restoration(state: string): string {
  return RESTORATION[state.toUpperCase()] ?? state.toLowerCase().replace(/_/g, " ");
}

/** The vocabulary state of each provider operation state (Running, Queued, Done, Stopped, Failed); any other one is Unavailable. */
const STATES: Record<string, string> = { RUNNING: "RUNNING", INTENT: "QUEUED", PREPARED: "QUEUED", EXITED: "COMPLETED", COMPLETED: "COMPLETED", STOPPED: "STOPPED", CANCELLED: "STOPPED", FAILED: "FAILED" };

function sessionState(state: string): DerivedState {
  const status = STATES[state.toUpperCase()];
  return status ? deriveState({ entity: "task", status }) : deriveState({ entity: "provider", status: "unavailable", reason: "" });
}

const isActive = (s: Row): boolean => ["running", "queued"].includes(sessionState(s.state).id);

function sessionRow(s: Row): HTMLElement {
  const native = s.native_session_id;
  return fleetRow(
    { "data-entity": `session:${s.operation_id}`, "data-l0": "sessions.session-row" },
    [statePill(sessionState(s.state)), el("strong", "nowrap", cap(s.route_id ?? s.provider)), s.model ? el("span", "muted nowrap", s.model) : null, el("span", "row-end muted small", ago(s.last_activity_at, "last activity"))],
    [rowLink(s.mission_name || "Mission", `/missions/${encodeURIComponent(s.worker)}/${encodeURIComponent(s.mission_id)}`), s.task_ordinal === null ? null : el("span", "muted nowrap", `Task ${s.task_ordinal}`), el("span", "muted nowrap", s.worker), el("span", "muted", ago(s.started_at, "started"))],
    "Details",
    facts([["Provider session", native ? idChip(native, "session ID") : "Not reported"], ["Restoration", restoration(s.restoration_state)]]),
  );
}

const EMPTY = "No provider sessions yet";
const list = (label: string, rows: Row[]): HTMLElement => h("ul", { class: "frows", "aria-label": label }, ...rows.map(sessionRow));

function render(ctx: FleetRender<Session[]>, state: { offset: number; open: boolean; redraw: () => void }): { answer: Answer; body: Node } {
  // A mission lives on one worker, so the same mission and operation from two workers is one session.
  const seen = new Set<string>();
  const rows = ctx.results.flatMap((r) => r.data.map((s): Row => ({ ...s, worker: r.worker }))).filter((s) => {
    const key = `${s.mission_id}\n${s.operation_id}`;
    return !seen.has(key) && seen.add(key);
  });
  if (rows.length === 0) {
    const names = ctx.results.map((r) => r.worker).join(", ");
    return {
      answer: calm(`${EMPTY}.`, "Run a mission to see its sessions here."),
      body: emptyState({ icon: "sessions", title: EMPTY, text: `${ctx.single ? names : "The online workers"} ${ctx.single || ctx.results.length === 1 ? "has" : "have"} not started a provider session. They are listed here once a mission runs a task.`, command: "llm-bridge mission create" }),
    };
  }
  const time = (v: string) => new Date(v).getTime() || 0;
  const active = rows.filter(isActive).sort((a, b) => time(b.last_activity_at) - time(a.last_activity_at));
  const history = rows.filter((s) => !isActive(s)).sort((a, b) => time(b.started_at) - time(a.started_at));
  const running = active.filter((s) => sessionState(s.state).id === "running").length;
  const historyBody: Node[] = [list("Earlier sessions", history.slice(state.offset, state.offset + PAGE))];
  if (history.length > PAGE) {
    historyBody.push(
      pager("Earlier sessions", state.offset, PAGE, history.length, (offset) => {
        state.offset = offset;
        state.redraw();
      }),
    );
  }
  return {
    answer: {
      what: active.length === 0 ? `No provider session is running. ${plural(history.length, "earlier session")} listed.` : `${plural(running, "provider session")} running${active.length > running ? `, ${active.length - running} starting` : ""}.`,
      needMe: "Nothing here needs you.",
      next: active.length === 0 ? "Start or continue a mission to begin one." : "Open a session's mission to watch it live.",
    },
    body: h("div", {}, h("div", { "data-section": "active" }, active.length ? list("Active sessions", active) : el("p", "muted", "No session is running right now.")), history.length ? h("div", { "data-section": "history" }, disclosure("History", historyBody, { count: history.length, id: "sessions-history", open: state.open, onToggle: (open) => (state.open = open) })) : null),
  };
}

export function sessions(root: HTMLElement, query: URLSearchParams): () => void {
  const paging = { offset: 0, open: false, redraw: () => {} };
  return fleetView<Session[]>(root, {
    title: "Sessions",
    query,
    load: async (workerName) => {
      const row = await api.command(workerName, "session.list", null, {});
      return (row.result?.result?.["sessions"] as Session[] | undefined) ?? [];
    },
    render: (ctx) => {
      const view = render(ctx, paging);
      paging.redraw = () => patch(document.getElementById("view-body") as HTMLElement, [render(ctx, paging).body]);
      return view;
    },
    icon: "sessions",
    empty: EMPTY,
    none: "Sessions come from workers, and none is enrolled. Add a worker, then run a mission on it.",
    offline: "its sessions can be listed",
    read: "provider sessions",
  });
}
