// Evidence of one task (spec 8.1, level 1): the verifier line from `task.show`, the changed files parsed from `task.diff`
// text, and the last output lines from `task.output`. Anything the worker cannot supply is stated, never left blank.
// The mission page, the Inbox approval dialog and the Inbox approval rows all read it through this module, so they cannot disagree.
import * as api from "../../api";
import { disclosure, h } from "../../dom";
import type { Task } from "./types";

export interface DiffFile {
  path: string;
  added: number;
  removed: number;
}

export interface DiffSummary {
  files: DiffFile[];
  added: number;
  removed: number;
}

/** Changed files and +/- line counts from the text of a unified diff (`diff --git` headers, `+` and `-` lines). */
export function parseDiff(text: string): DiffSummary {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  for (const line of text.split("\n")) {
    const header = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (header) {
      current = { path: header[2] ?? header[1] ?? "", added: 0, removed: 0 };
      files.push(current);
    } else if (current && line.startsWith("+") && !line.startsWith("+++")) current.added += 1;
    else if (current && line.startsWith("-") && !line.startsWith("---")) current.removed += 1;
  }
  return { files, added: files.reduce((n, f) => n + f.added, 0), removed: files.reduce((n, f) => n + f.removed, 0) };
}

const asText = (v: unknown): string => (typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, 160) : "");
const rec = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

/**
 * The verifier result in words, from whichever shape the worker reports it (a structured result, or the `evidence` entries of the task
 * record, the last one from the verifier winning); "" when nothing was recorded.
 */
export function verifierText(show: Record<string, unknown> | null): string {
  const task = rec(show?.["task"] ?? show);
  const raw = task["verification"] ?? task["verifier"] ?? task["verifier_result"];
  if (typeof raw === "string") return asText(raw);
  if (raw && typeof raw === "object") {
    const v = rec(raw);
    const passed = typeof v["passed"] === "boolean" ? v["passed"] : typeof v["ok"] === "boolean" ? v["ok"] : typeof v["exit_code"] === "number" ? v["exit_code"] === 0 : null;
    const command = asText(v["command"]);
    return [passed === null ? "result recorded" : passed ? "passed" : "failed", command && (typeof v["exit_code"] === "number" ? `${command} (exit ${v["exit_code"]})` : command), asText(v["summary"] ?? v["output"])].filter(Boolean).join(" - ");
  }
  const entries = Array.isArray(task["evidence"]) ? (task["evidence"] as unknown[]).map(rec) : [];
  const last = [...entries].reverse().find((e) => e["source"] === "verifier");
  return asText(last?.["text"] ?? last?.["summary"]);
}

const unavailable = (what: string, e: api.TaskEvidence): string => `${what} unavailable: ${asText(e.message) || asText(e.reason) || "the worker has none for this task"}`;

export interface Evidence {
  title: string;
  verifier: string;
  /** "2 files, +5 -2", "No changed files", or why the diff is unavailable. */
  changes: string;
  diffOk: boolean;
  files: DiffFile[];
  diff: string;
  /** The last output lines, or "" (`outputNote` then says why, when the worker said). */
  tail: string;
  outputNote: string;
}

/** Read the evidence of one task: `task.show`, `task.diff` and, with `full`, `task.output`. A read that fails is reported in the text, never thrown. */
export async function readEvidence(workerName: string, missionId: string, ordinal: number, full: boolean): Promise<Evidence> {
  const [show, diff, out] = await Promise.allSettled([api.taskShow(workerName, missionId, ordinal), api.taskDiff(workerName, missionId, ordinal), full ? api.taskOutput(workerName, missionId, ordinal) : Promise.resolve(null)]);
  const shown = show.status === "fulfilled" ? show.value : null;
  const e: Evidence = { title: asText(rec(rec(shown)["task"] ?? shown)["title"]), verifier: verifierText(shown), changes: "Diff unavailable: the worker did not answer", diffOk: false, files: [], diff: "", tail: "", outputNote: "" };
  if (diff.status === "fulfilled" && diff.value.available !== false && typeof diff.value.text === "string") {
    const sum = parseDiff(diff.value.text);
    Object.assign(e, { diffOk: true, files: sum.files, diff: diff.value.text, changes: sum.files.length === 0 ? "No changed files" : `${sum.files.length} ${sum.files.length === 1 ? "file" : "files"}, +${sum.added}\u00a0-${sum.removed}` });
  } else if (diff.status === "fulfilled") e.changes = unavailable("Diff", diff.value);
  if (out.status === "fulfilled" && out.value) {
    if (out.value.available !== false && typeof out.value.text === "string") e.tail = out.value.text.split("\n").filter((l) => l.trim()).slice(-8).join("\n");
    else e.outputNote = unavailable("Output", out.value);
  }
  return e;
}

/** One line for a row: the size of the change first, then the verifier: "2 files, +5 -2 - verifier: cargo test exited 0" (or that no verifier result exists). */
export const evidenceLine = (e: Evidence): string => `${e.changes} - ${e.verifier ? `verifier: ${e.verifier}` : "no verifier result"}`;

export interface EvidenceView {
  /** Fills with the summary once the reads finish. */
  el: HTMLElement;
  done: Promise<Evidence>;
}

/**
 * The evidence of one task. With `full`, the changed-file list, the full diff and the output tail are included; with `title`, the task's own
 * title comes first (a dialog that does not already name it).
 */
export function evidenceBody(workerName: string, missionId: string, task: Pick<Task, "ordinal">, full = true, title = false): EvidenceView {
  const el = h("div", { class: "evidence-body", "aria-busy": "true" }, h("p", { class: "muted" }, "Loading evidence..."));
  const done = readEvidence(workerName, missionId, task.ordinal, full).then((e) => {
    const nodes: Array<Node | null> = [
      title ? h("p", {}, h("strong", {}, "Task: "), `${task.ordinal}${e.title ? ` - ${e.title}` : ""}`) : null,
      e.verifier ? h("p", { class: "evidence-verifier" }, `Verifier: ${e.verifier}`) : h("p", { class: "evidence-verifier notice", role: "status" }, "No verifier result is recorded for this task. Check the work yourself before approving it."),
      h("p", { class: e.diffOk ? "evidence-diff" : "evidence-diff muted" }, e.changes),
    ];
    if (full && e.files.length) {
      nodes.push(h("ul", { class: "evidence-files" }, ...e.files.slice(0, 12).map((f) => h("li", {}, h("code", { class: "long-text" }, f.path), h("span", { class: "num muted" }, ` +${f.added} -${f.removed}`)))));
      if (e.files.length > 12) nodes.push(h("p", { class: "muted small" }, `and ${e.files.length - 12} more`));
      nodes.push(disclosure("Full diff", h("pre", { class: "diff", tabindex: "0", "data-scroll-x": true }, e.diff.slice(0, 20000))));
    }
    if (e.tail) nodes.push(h("pre", { class: "evidence-out", tabindex: "0", "data-scroll-x": true, "aria-label": "Last output lines" }, e.tail));
    else if (e.outputNote) nodes.push(h("p", { class: "muted small" }, e.outputNote));
    el.removeAttribute("aria-busy");
    el.replaceChildren(...nodes.flatMap((n) => (n ? [n] : [])));
    return e;
  });
  return { el, done };
}
