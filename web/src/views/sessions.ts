import { h, isoTime, statusBadge } from "../dom";
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
    operation: "session.list",
    query,
    render: (result, workerName) => {
      const list = (result["sessions"] as Session[] | undefined) ?? [];
      if (list.length === 0) return h("p", { class: "muted" }, `No provider sessions recorded on ${workerName}.`);
      return table(
        "sessions-table",
        `Provider sessions on ${workerName}`,
        ["Provider", "Mission", "Task", "State", "Session", "Restoration", "Started", "Last activity"],
        list.map((s) => [
          h("span", {}, dash(s.route_id ?? s.provider)),
          h("a", { href: `/missions/${encodeURIComponent(workerName)}/${encodeURIComponent(s.mission_id)}` }, s.mission_name || s.mission_id),
          h("span", {}, dash(s.task_ordinal)),
          statusBadge(s.state),
          h("code", {}, dash(s.native_session_id)),
          h("span", {}, s.restoration_state.toLowerCase()),
          h("span", {}, isoTime(s.started_at)),
          h("span", {}, isoTime(s.last_activity_at)),
        ]),
      );
    },
  });
}
