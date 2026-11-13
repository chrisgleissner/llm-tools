// "Session" details of a mission (PRD-015): provider and model, the task being worked on, how long the current run has taken, the last line the
// provider printed, and the history of runs (turns). Everything comes from `facts.ts`; a value the worker does not report is left out, never guessed.
import { h } from "../../dom";
import { fields } from "./kit";
import { durationText, sessionFacts, turnLine, type Turn } from "./facts";

export interface SessionInfoModel {
  snapshot: Record<string, unknown> | null;
  /** Provider runs from the mission's events (`turnsFromEvents`). */
  turns: Turn[];
  /** The last line of the provider's retained log (`mission.activity_line`), "" when unknown. */
  lastLine: string;
  nowMs: number;
}

export interface SessionInfo {
  el: HTMLDetailsElement;
  update(m: SessionInfoModel): void;
}

const HISTORY_SHOWN = 10;

export function sessionInfo(): SessionInfo {
  const body = h("div", { class: "disclosure-body" }, h("p", { class: "muted" }, "Loading..."));
  // Closed, so the side column keeps its height; the summary says what matters at a glance (provider, model, task, elapsed time).
  const glance = h("span", { class: "muted small", "data-q": "session-glance" });
  const el = h("details", { class: "disclosure", id: "session-details" }, h("summary", {}, h("span", {}, "Session"), glance), body);
  let key = "";
  return {
    el,
    update(m) {
      const f = sessionFacts(m.snapshot, m.turns);
      const rows: Array<[string, Node | string]> = [];
      if (f.provider) rows.push(["Provider", f.provider[0]!.toUpperCase() + f.provider.slice(1)]);
      if (f.model) rows.push(["Model", f.model]);
      if (f.task) rows.push(["Task", `${f.task.ordinal}${f.task.title ? `, ${f.task.title}` : ""}`]);
      if (f.runningSinceMs) rows.push(["Running for", durationText(f.runningSinceMs, m.nowMs)]);
      if (m.lastLine) rows.push(["Last output", h("code", { class: "long-text", "data-q": "last-line" }, m.lastLine)]);
      glance.textContent = [[f.provider, f.model].filter(Boolean).join(" "), f.task ? `task ${f.task.ordinal}` : "", f.runningSinceMs ? durationText(f.runningSinceMs, m.nowMs) : ""].filter(Boolean).join(" · ");
      const turns = f.turns.slice(0, HISTORY_SHOWN);
      const next = JSON.stringify([rows.map(([k, v]) => [k, typeof v === "string" ? v : v.textContent]), turns.map((t) => turnLine(t, m.nowMs)), f.turns.length]);
      if (next === key) return;
      key = next;
      const nodes: Node[] = [];
      if (rows.length) nodes.push(fields("about-fields session-facts", rows));
      else nodes.push(h("p", { class: "muted" }, "The worker has not reported a provider run yet."));
      if (turns.length) {
        nodes.push(
          h("h3", { class: "session-turns-title" }, `Turn history (${f.turns.length})`),
          h("ol", { class: "session-turns", "aria-label": "Provider runs, newest first" }, ...turns.map((t) => h("li", { "data-outcome": t.outcome }, turnLine(t, m.nowMs)))),
        );
        if (f.turns.length > turns.length) nodes.push(h("p", { class: "muted small" }, `Showing the newest ${turns.length} of ${f.turns.length} runs.`));
      }
      body.replaceChildren(...nodes);
    },
  };
}
