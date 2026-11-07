// Evidence of one task (spec 8.1, level 1): the verifier line from `task.show`, the changed files parsed from `task.diff`
// text, and the last output lines from `task.output`. Anything the worker cannot supply is stated, never left blank.
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

const asText = (v: unknown): string => (typeof v === "string" ? v.replace(/[\r\n\t]+/g, " ").trim().slice(0, 160) : "");

/** The verifier result from the fields `task.show` reports, in words; "No verifier result recorded" when there is none. */
export function verifierLine(show: Record<string, unknown> | null): string {
  const raw = show?.["verification"] ?? show?.["verifier"] ?? show?.["verifier_result"];
  if (!raw || typeof raw !== "object") return typeof raw === "string" && raw.trim() ? `Verifier: ${asText(raw)}` : "No verifier result recorded";
  const v = raw as Record<string, unknown>;
  const passed = typeof v["passed"] === "boolean" ? v["passed"] : typeof v["ok"] === "boolean" ? v["ok"] : typeof v["exit_code"] === "number" ? v["exit_code"] === 0 : null;
  const parts = [passed === null ? "result recorded" : passed ? "passed" : "failed"];
  const command = asText(v["command"]);
  if (command) parts.push(typeof v["exit_code"] === "number" ? `${command} (exit ${v["exit_code"]})` : command);
  const summary = asText(v["summary"] ?? v["output"]);
  if (summary) parts.push(summary);
  return `Verifier: ${parts.join(" - ")}`;
}

const unavailable = (what: string, e: api.TaskEvidence): string => `${what} unavailable: ${asText(e.message) || asText(e.reason) || "the worker has none for this task"}`;

export interface EvidenceView {
  /** Fills with the summary once the three reads finish. */
  el: HTMLElement;
  done: Promise<void>;
}

/** The evidence summary of one task. With `full`, the changed-file list, the full diff and the output tail are included. */
export function evidenceBody(workerName: string, missionId: string, task: Pick<Task, "ordinal">, full = true): EvidenceView {
  const el = h("div", { class: "evidence-body", "aria-busy": "true" }, h("p", { class: "muted" }, "Loading evidence..."));
  const done = (async () => {
    const [show, diff, out] = await Promise.allSettled([
      api.taskShow(workerName, missionId, task.ordinal),
      api.taskDiff(workerName, missionId, task.ordinal),
      full ? api.taskOutput(workerName, missionId, task.ordinal) : Promise.resolve(null),
    ]);
    const nodes: Array<Node | null> = [];
    nodes.push(h("p", { class: "evidence-verifier" }, show.status === "fulfilled" ? verifierLine(show.value) : "Verifier result unavailable: the worker did not answer"));
    if (diff.status === "fulfilled" && diff.value.available !== false && typeof diff.value.text === "string") {
      const sum = parseDiff(diff.value.text);
      nodes.push(h("p", { class: "evidence-diff" }, sum.files.length === 0 ? "No changed files" : `${sum.files.length} changed ${sum.files.length === 1 ? "file" : "files"}, +${sum.added} -${sum.removed}`));
      if (full && sum.files.length) {
        nodes.push(h("ul", { class: "evidence-files" }, ...sum.files.slice(0, 12).map((f) => h("li", {}, h("code", { class: "long-text" }, f.path), h("span", { class: "num muted" }, ` +${f.added} -${f.removed}`)))));
        if (sum.files.length > 12) nodes.push(h("p", { class: "muted small" }, `and ${sum.files.length - 12} more`));
        nodes.push(disclosure("Full diff", h("pre", { class: "diff", tabindex: "0", "data-scroll-x": true }, diff.value.text.slice(0, 20000))));
      }
    } else nodes.push(h("p", { class: "evidence-diff muted" }, diff.status === "fulfilled" ? unavailable("Diff", diff.value) : "Diff unavailable: the worker did not answer"));
    if (full) {
      if (out.status === "fulfilled" && out.value && out.value.available !== false && typeof out.value.text === "string") {
        const tail = out.value.text.split("\n").filter((l) => l.trim()).slice(-8).join("\n");
        if (tail) nodes.push(h("pre", { class: "evidence-out", tabindex: "0", "data-scroll-x": true, "aria-label": "Last output lines" }, tail));
      } else if (out.status === "fulfilled" && out.value) nodes.push(h("p", { class: "muted small" }, unavailable("Output", out.value)));
    }
    el.removeAttribute("aria-busy");
    el.replaceChildren(...nodes.flatMap((n) => (n ? [n] : [])));
  })();
  return { el, done };
}
