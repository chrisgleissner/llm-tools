import { emptyState, h, idChip, statusBadge, timeEl } from "../dom";
import { table, workerReadView } from "./remote";

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

const dash = (v: string | number | null | undefined) => (v === null || v === undefined || v === "" ? "-" : String(v));

export function sessions(root: HTMLElement, query: URLSearchParams): () => void {
  return workerReadView(root, {
    title: "Sessions",
    resource: "sessions",
    query,
    render: (result, workerName) => {
      const list = (result["sessions"] as Session[] | undefined) ?? [];
      if (list.length === 0) return emptyState({ icon: "sessions", title: "No provider sessions yet", text: `${workerName} has not started a provider session. They are listed here once a mission runs a task.` });
      return table(
        "sessions-table",
        `Provider sessions on ${workerName}`,
        ["Provider", "Mission", "Task", "State", "Session", "Restoration", "Started", "Last activity"],
        list.map((s) => [
          h("span", {}, dash(s.route_id ?? s.provider)),
          h("a", { href: `/missions/${encodeURIComponent(workerName)}/${encodeURIComponent(s.mission_id)}` }, s.mission_name || s.mission_id),
          h("span", {}, dash(s.task_ordinal)),
          statusBadge(s.state),
          s.native_session_id ? idChip(s.native_session_id, "session ID") : h("span", {}, "-"),
          h("span", {}, s.restoration_state.toLowerCase()),
          timeEl(s.started_at),
          timeEl(s.last_activity_at),
        ]),
      );
    },
  });
}
