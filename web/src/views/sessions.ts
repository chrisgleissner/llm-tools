import * as api from "../api";
import { copyButton, disclosure, emptyState, h, pager, statePill } from "../dom";
import { shortId, deriveState, type DerivedState } from "../state";
import { ago, fleetView, plural, rowLink, toggleDetails, type Answer, type FleetRender } from "./remote";

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

/** One state for a provider operation: it reuses the shared vocabulary (Running, Queued, Done, Stopped, Failed, Unavailable). */
function sessionState(state: string): DerivedState {
  switch (state.toUpperCase()) {
    case "RUNNING":
      return deriveState({ entity: "task", status: "RUNNING" });
    case "INTENT":
    case "PREPARED":
      return deriveState({ entity: "task", status: "QUEUED" });
    case "EXITED":
    case "COMPLETED":
      return deriveState({ entity: "task", status: "COMPLETED" });
    case "STOPPED":
    case "CANCELLED":
      return deriveState({ entity: "task", status: "STOPPED" });
    case "FAILED":
      return deriveState({ entity: "task", status: "FAILED" });
    default:
      return deriveState({ entity: "provider", status: "unavailable", reason: "" });
  }
}

const isActive = (s: DerivedState): boolean => s.id === "running" || s.id === "queued";

function sessionRow(s: Row): HTMLElement {
  const state = sessionState(s.state);
  const provider = s.route_id ?? s.provider;
  const mission = s.mission_name || "Mission";
  const details = toggleDetails("Details", h("dl", { class: "facts" }, h("dt", {}, "Provider session"), h("dd", {}, s.native_session_id ? h("span", { class: "idchip" }, h("code", { title: s.native_session_id }, shortId(s.native_session_id)), copyButton(s.native_session_id, "session ID")) : "Not reported"), h("dt", {}, "Restoration"), h("dd", {}, restoration(s.restoration_state))));
  return h(
    "li",
    { class: "frow", "data-entity": `session:${s.operation_id}`, "data-l0": "sessions.session-row" },
    h("div", { class: "row-main" }, statePill(state), h("strong", { class: "nowrap" }, provider), s.model ? h("span", { class: "muted nowrap" }, s.model) : null, h("span", { class: "row-end muted small" }, ago(s.last_activity_at, "last activity"))),
    h(
      "div",
      { class: "row-sub small" },
      h("span", { class: "row-facts" }, rowLink(mission, `/missions/${encodeURIComponent(s.worker)}/${encodeURIComponent(s.mission_id)}`), s.task_ordinal === null ? null : h("span", { class: "muted nowrap" }, `Task ${s.task_ordinal}`), h("span", { class: "muted nowrap" }, s.worker), h("span", { class: "muted" }, ago(s.started_at, "started"))),
      h("span", { class: "row-end" }, details.button),
    ),
    details.panel,
  );
}

function answerFor(active: Row[], history: Row[]): Answer {
  const running = active.filter((s) => sessionState(s.state).id === "running").length;
  return {
    what: active.length === 0 ? `No provider session is running. ${plural(history.length, "earlier session")} listed.` : `${plural(running, "provider session")} running${active.length > running ? `, ${active.length - running} starting` : ""}.`,
    needMe: "Nothing here needs you.",
    next: active.length === 0 ? "Start or continue a mission to begin one." : "Open a session's mission to watch it live.",
  };
}

function render(ctx: FleetRender<Session[]>, state: { offset: number; open: boolean; redraw: () => void }): { answer: Answer; body: Node } {
  // A mission lives on one worker, so the same mission and operation from two workers is one session.
  const seen = new Set<string>();
  const rows: Row[] = [];
  for (const r of ctx.results) {
    for (const s of r.data) {
      const key = `${s.mission_id}\n${s.operation_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({ ...s, worker: r.worker });
    }
  }
  const time = (v: string) => new Date(v).getTime() || 0;
  const active = rows.filter((s) => isActive(sessionState(s.state))).sort((a, b) => time(b.last_activity_at) - time(a.last_activity_at));
  const history = rows.filter((s) => !isActive(sessionState(s.state))).sort((a, b) => time(b.started_at) - time(a.started_at));
  const answer = answerFor(active, history);
  if (rows.length === 0) {
    const names = ctx.results.map((r) => r.worker).join(", ");
    return {
      answer: { what: "No provider sessions yet.", needMe: "Nothing needs you.", next: "Run a mission to see its sessions here." },
      body: emptyState({ icon: "sessions", title: "No provider sessions yet", text: `${ctx.single ? names : "The online workers"} ${ctx.single || ctx.results.length === 1 ? "has" : "have"} not started a provider session. They are listed here once a mission runs a task.`, command: "llm-bridge mission create" }),
    };
  }
  const page = history.slice(state.offset, state.offset + PAGE);
  const activeSection = active.length > 0 ? h("ul", { class: "frows", "aria-label": "Active sessions" }, ...active.map(sessionRow)) : h("p", { class: "muted" }, "No session is running right now.");
  const historyBody: Node[] = [h("ul", { class: "frows", "aria-label": "Earlier sessions" }, ...page.map(sessionRow))];
  if (history.length > PAGE) {
    historyBody.push(
      pager("Earlier sessions", state.offset, PAGE, history.length, (offset) => {
        state.offset = offset;
        state.redraw();
      }),
    );
  }
  const body = h("div", {}, h("div", { "data-section": "active" }, activeSection), history.length > 0 ? h("div", { "data-section": "history" }, disclosure("History", historyBody, { count: history.length, id: "sessions-history", open: state.open, onToggle: (open) => (state.open = open) })) : null);
  return { answer, body };
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
      paging.redraw = () => document.getElementById("view-body")?.replaceChildren(render(ctx, paging).body);
      return view;
    },
    noWorkers: () => emptyState({ icon: "sessions", title: "No provider sessions yet", text: "Sessions come from workers, and none is enrolled. Add a worker, then run a mission on it.", action: h("div", { class: "actions" }, h("a", { class: "btn primary", href: "/workers" }, "Go to Workers")) }),
    allOffline: (names) => emptyState({ icon: "workers", title: "No worker is online", text: `${names.join(", ")} must reconnect before its sessions can be listed.`, action: h("div", { class: "actions" }, h("a", { class: "btn", href: "/workers" }, "Check Workers")) }),
    idle: (reason) => ({
      what: reason === "loading" ? "Reading provider sessions from the workers." : reason === "offline" ? "No worker is online, so sessions cannot be read." : "No worker is enrolled yet.",
      needMe: reason === "loading" ? "Nothing needs you." : "A worker has to be online first.",
      next: reason === "loading" ? "This takes a moment." : "Open Workers to add or check one.",
    }),
  });
}
