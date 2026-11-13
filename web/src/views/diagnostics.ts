import * as api from "../api";
import { errorBlock, h, deferSkeleton, isoTime } from "../dom";
import { icon, type IconName } from "../icons";
import { apiMsg, facts } from "../kit-b";
import { store, subscribe } from "../store";
import { answerLine, plural } from "./remote";

/** "26 h 3 min", "4 min 10 s", "3 days 2 h": uptime in words instead of a raw second count. */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  if (s < 2 * 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} days ${Math.floor((s % 86400) / 3600)} h`;
}

function bytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.round(n / 1024)} KB`;
}

export type CheckStatus = "ok" | "warn" | "fail";
export interface CheckRow {
  name: string;
  value: string;
  status: CheckStatus;
  fix: string | null;
}
const STATUS_WORD: Record<CheckStatus, string> = { ok: "OK", warn: "Warning", fail: "Failing" };
const STATUS_ICON: Record<CheckStatus, IconName> = { ok: "checkCircle", warn: "alert", fail: "xOctagon" };

const asStatus = (v: unknown): CheckStatus => (v === "ok" || v === "warn" || v === "fail" ? v : "warn");

/** A check value as text: scalars as they are, anything else as compact JSON. */
export function valueText(v: unknown): string {
  if (v === null || v === undefined || v === "") return "-";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** The `checks` map as rows in the coordinator's order. A coordinator without `checks` gives no rows. */
export function checkRows(checks: unknown): CheckRow[] {
  if (!checks || typeof checks !== "object" || Array.isArray(checks)) return [];
  return Object.entries(checks as Record<string, unknown>).map(([name, raw]) => {
    const c = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    const status = asStatus(c["status"]);
    const fix = typeof c["fix"] === "string" && c["fix"] ? c["fix"] : null;
    return { name, value: valueText(c["value"]), status, fix: status === "ok" ? null : fix };
  });
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** "3 h 2 min ago" for a backup age in seconds. */
export function ageWords(seconds: number): string {
  return `${duration(seconds)} ago`;
}

/** The backup panel rows. `null` when the coordinator reports no `backup` object (an older coordinator). */
export function backupRows(backup: unknown, now = Date.now()): Array<[string, string | null]> | null {
  if (!backup || typeof backup !== "object") return null;
  const b = backup as Record<string, unknown>;
  if (b["enabled"] === false) return [["Automatic backups", "Off"]];
  const at = num(b["last_backup_at"]);
  const age = num(b["last_backup_age_seconds"]) ?? (at === null ? null : Math.max(0, Math.round(now / 1000 - at)));
  const size = num(b["last_backup_bytes"]);
  const interval = num(b["interval_seconds"]);
  const next = num(b["next_due_at"]);
  const result = typeof b["last_result"] === "string" ? b["last_result"] : null;
  const reason = typeof b["last_reason"] === "string" && b["last_reason"] ? b["last_reason"] : null;
  return [
    ["Automatic backups", interval === null ? "On" : `On, every ${duration(interval)}`],
    ["Last backup", age === null ? "None yet" : `${ageWords(age)} (${isoTime(at)})`],
    ["Size", size === null ? "-" : bytes(size)],
    ["Kept", `${num(b["count"]) ?? 0} of ${num(b["keep"]) ?? "-"}`],
    ["Next due", next === null ? "-" : isoTime(next)],
    ["Last attempt", result === null ? "None yet" : reason ? `${result}: ${reason}` : result],
    ["Directory", typeof b["dir"] === "string" ? b["dir"] : null],
  ];
}

const statusBadge = (status: CheckStatus): HTMLElement =>
  h("span", { class: "pill dense", "data-state": status === "ok" ? "done" : status === "warn" ? "waiting" : "failed", "data-tier": status === "fail" ? "alert" : null }, icon(STATUS_ICON[status], 16), STATUS_WORD[status]);

const section = (id: string, title: string, ...children: Array<Node | string>): HTMLElement => h("section", { class: "dsection", "aria-labelledby": id }, h("h2", { id }, title), ...children);

export function diagnostics(root: HTMLElement): () => void {
  const answers = answerLine({ what: "Reading coordinator diagnostics.", needMe: "Nothing needs you.", next: "This takes a moment." });
  const body = h("div", { id: "diag-body" });
  root.append(h("h1", {}, "Diagnostics"), answers.el, body);
  let data: Record<string, unknown> | null = null;
  let error = "";
  const done = deferSkeleton(body, 3);
  const grid = (label: string, rows: Array<[string, unknown]>) => facts("facts grid", label, rows);
  const render = () => {
    if (error) {
      answers.set({ what: "Coordinator diagnostics could not be loaded.", needMe: "Check that you are still signed in and the coordinator is reachable.", next: "Retry in a moment." });
      return body.replaceChildren(errorBlock({ message: error, retry: load, code: "diagnostics-failed" }));
    }
    if (!data) return;
    const d = data;
    const depths = (d["command_queue_depths"] ?? {}) as Record<string, Record<string, number>>;
    const counts = Object.values(depths).flatMap((states) => Object.values(states));
    const waiting = counts.reduce((sum, n) => sum + n, 0);
    const uptime = duration(Number(d["uptime_seconds"] ?? 0));
    answers.set({
      what: `Coordinator ${String(d["version"] ?? "")} has been up ${uptime} with ${plural(Number(d["connected_workers"] ?? 0), "worker")} connected.`,
      needMe: waiting === 0 ? "Nothing needs you." : `${plural(waiting, "command")} ${waiting === 1 ? "is" : "are"} still waiting for a worker.`,
      next: waiting === 0 ? "Nothing to do." : "Check that the workers named under Command queues are online.",
    });
    const rows = checkRows(d["checks"]);
    const overall = d["status"] === undefined ? null : asStatus(d["status"]);
    const backup = backupRows(d["backup"]);
    const backupOff = (d["backup"] as Record<string, unknown> | undefined)?.["enabled"] === false;
    body.replaceChildren(
      ...(overall ? [h("p", { id: "diag-overall" }, "Overall: ", statusBadge(overall))] : []),
      section(
        "dg-coord",
        "Coordinator",
        grid("Coordinator diagnostics", [
          ["Version", d["version"]],
          ["Protocol", d["protocol_version"]],
          ["Up for", uptime],
          ["Memory", d["rss_kib"] == null ? null : bytes(Number(d["rss_kib"]) * 1024)],
          ["Database", bytes(Number(d["db_bytes"] ?? 0))],
          ["SQLite", d["sqlite_version"]],
          ["Connected workers", d["connected_workers"]],
        ]),
      ),
      section(
        "dg-queue",
        "Command queues",
        counts.length > 0
          ? h("ul", { class: "plain" }, ...Object.entries(depths).map(([w, s]) => h("li", {}, h("strong", { class: "nowrap" }, w), ` ${Object.entries(s).map(([k, v]) => `${v} ${k}`).join(", ")}`)))
          : h("p", { class: "muted" }, "No unfinished commands."),
      ),
      ...(rows.length > 0
        ? [
            section(
              "dg-checks",
              "Checks",
              h(
                "ul",
                { class: "plain", id: "diag-checks" },
                ...rows.map((r) =>
                  h("li", { "data-check": r.name, "data-status": r.status }, h("strong", { class: "nowrap" }, r.name), " ", statusBadge(r.status), " ", h("span", {}, r.value), r.fix ? h("p", { class: "muted" }, `Fix: ${r.fix}`) : null),
                ),
              ),
            ),
          ]
        : []),
      ...(backup
        ? [
            section(
              "dg-backup",
              "Backups",
              grid("Backups", backup),
              ...(backupOff ? [h("p", { class: "muted" }, "Automatic backups are off. Set backup_interval_hours above 0 in the coordinator configuration to turn them on.")] : []),
              h("p", { class: "muted" }, "Restore is done on the coordinator host: bridge-coordinator restore --input FILE."),
            ),
          ]
        : []),
      section("dg-client", "This browser", grid("Browser", [["Live stream", store.conn], ["Workers known", store.workers.length], ["Missions known", store.missions.length]])),
    );
  };
  const load = () => {
    error = "";
    api
      .get<Record<string, unknown>>("/api/v1/diagnostics")
      .then((d) => {
        data = d;
      })
      .catch((e) => {
        error = apiMsg(e, "Cannot load diagnostics.");
      })
      .finally(() => {
        done();
        render();
      });
  };
  load();
  return subscribe(render);
}
