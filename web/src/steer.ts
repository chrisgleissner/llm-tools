// Steer dialogs of the session pane (loaded when the first Steer item is chosen, so they stay out of the terminal chunk).
import * as api from "./api";
import { h } from "./dom";
import { onEvents } from "./events-bus";
import { announce, confirmDialog, reportOutcome } from "./ui";

export interface SteerTarget {
  workerName: string;
  missionId: string;
  /** The `[data-queued-steer]` container the chips go into. */
  chips: HTMLElement;
  /** Registers the cleanup of a chip's stream listener with the composer. */
  own(stop: () => void): void;
}

/** The event that shows queued steering was applied (see bridge_runtime); review and replace are seen as a new task or a task change. */
const applied = (kind: api.SteerKind): RegExp =>
  new RegExp(kind === "review" ? "^task-added$" : kind === "replace" ? "^task-status$" : `^steering-${kind.replace("-after-iteration", "").replace("-after-task", "-task")}-(applied|consumed)$`);
const WHAT = {
  review: (n: number) => `review of task ${n}`,
  verify: (n: number) => `verify task ${n} when the run exits`,
  replace: (n: number) => `new prompt for task ${n}`,
  "stop-after-task": (n: number) => `stop after task ${n}`,
};
const HELP: Partial<Record<api.SteerKind, string>> = {
  review: "The provider reviews the task again.",
  verify: "The verifier runs when the run exits.",
  replace: "The task gets a new prompt.",
  "stop-after-task": "The mission stops after that task.",
  handoff: "The next run uses another provider.",
};
const OPEN = /^(RUNNING|VERIFYING|BLOCKED|READY|QUEUED|PAUSED|CREATED)$/;
const field = (id: string, label: string, control: HTMLElement): HTMLElement => h("div", { class: "field" }, h("label", { for: id }, label), control);

/** A chip for queued steering; it goes away when the event that applies it arrives. There is no retract operation, so it has no Undo. */
function chip(o: SteerTarget, kind: api.SteerKind, text: string): void {
  const done = applied(kind);
  const node = h("span", { class: "queued-chip" }, `Queued: ${text}`);
  o.chips.append(node);
  o.chips.hidden = false;
  const stop = onEvents((events) => {
    if (!events.some((e) => (e.mission_id ?? o.missionId) === o.missionId && done.test(String(e.kind)))) return;
    node.remove();
    o.chips.hidden = !o.chips.childElementCount;
    stop();
  });
  o.own(stop);
}

/** Ask for what the kind needs, send `mission.steer`, and show a chip while it is queued. */
export async function runSteer(o: SteerTarget, kind: api.SteerKind, label: string): Promise<void> {
  const request: api.SteerRequest = { kind };
  let what = "pause after this iteration";
  if (kind !== "pause-after-iteration") {
    const handoff = kind === "handoff";
    const withPrompt = kind === "review" || kind === "replace";
    const pick = h("select", { id: "steer-target", disabled: true }, h("option", {}, "Loading..."));
    const text = h("textarea", { id: "steer-prompt", rows: 3 });
    const route = h("input", { id: "steer-route", type: "text", list: "steer-routes" });
    const names = h("datalist", { id: "steer-routes" });
    const body = handoff ? [field("steer-route", "Provider or route", route), names] : [field("steer-target", "Task", pick)];
    if (withPrompt) body.push(field("steer-prompt", kind === "review" ? "Instructions (optional)" : "New prompt", text));
    // The dialog opens at once; its choices arrive when the worker answers.
    const loaded = handoff
      ? api.missionStatus(o.workerName, o.missionId).then((s) => {
          const c = (s["station_config"] ?? {}) as Record<string, unknown>;
          const list = [c["routes"], c["providers"]].flatMap((v) => (Array.isArray(v) ? v.map(String) : []));
          names.append(...[...new Set(list)].map((n) => h("option", { value: n })));
          route.value = list[0] ?? "";
        })
      : api.missionTasks(o.workerName, o.missionId).then((r) => {
          const list = r.tasks.filter((t) => kind === "review" || OPEN.test(String(t["status"])));
          pick.replaceChildren(...list.map((t) => h("option", { value: String(t["ordinal"]) }, `${t["ordinal"]}. ${t["title"]}`)));
          // The default is the task the operator most likely means: the one awaiting approval, else the one running, else the first.
          pick.value = String((["VERIFYING", "RUNNING"].map((st) => list.find((t) => t["status"] === st)).find(Boolean) ?? list[0])?.["ordinal"] ?? "");
          pick.disabled = !list.length;
        });
    const answer = confirmDialog({ title: label, message: `${HELP[kind]} Applied at the next safe point.`, confirmLabel: handoff ? "Hand off" : `Queue ${{ review: "review", verify: "verify", replace: "new prompt" }[kind as string] ?? "stop"}`, details: h("div", {}, ...body) });
    (withPrompt ? text : handoff ? route : pick).focus();
    if ((await answer) === null) return;
    await loaded.catch(() => {});
    const bad = handoff ? !(request.route = route.value.trim()) : !(request.task = Number(pick.value)) || (kind === "replace" && !text.value.trim());
    if (bad) return announce(handoff ? "Name the provider or route to hand off to." : kind === "replace" ? "Replace needs a task and the new prompt." : "This mission has no task to steer.", "rejected");
    if (withPrompt) request.prompt = text.value.trim();
    what = handoff ? `hand off to ${request.route} at the next safe point` : WHAT[kind as keyof typeof WHAT](request.task!);
  }
  if (reportOutcome(await api.steer(o.workerName, o.missionId, request), `Queued: ${what}`, `queue ${what}`)) chip(o, kind, what);
}
