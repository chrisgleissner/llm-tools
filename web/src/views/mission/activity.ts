// The Activity panel (spec 8.1): milestones by default, "All activity" adds detail events, plumbing is counted and hidden
// behind a Show button. Each row has a "Raw" disclosure that shows only the allowlisted event fields.
import type { BridgeEvent } from "../../api";
import { button, emptyState, h, keepFocus } from "../../dom";
import { allowedEventData, humanizeEvent } from "../../event-humanizer";
import { eventClass, type EventClass } from "../../event-class";
import { fields, more, note, tsEl } from "./kit";

const PAGE = 20;
const PLUMBING_PAGE = 50;

export interface ActivityData {
  events: BridgeEvent[];
  error: string;
  loaded: boolean;
  /** Older events exist that the page has not requested yet. */
  more: boolean;
}

export interface ActivityPanel {
  el: HTMLElement;
  update(data: ActivityData): void;
}

const VIEWS = { milestones: "Milestones", all: "All activity" } as const;
type View = keyof typeof VIEWS;

interface Classed {
  event: BridgeEvent;
  cls: EventClass;
}

const plural = (n: number, word: string, suffix = "s"): string => `${n} ${word}${n === 1 ? "" : suffix}`;

function plumbingBreakdown(rows: Classed[]): string {
  const count = (kind: string): number => rows.filter((r) => r.event.kind.startsWith(kind)).length;
  const checks = count("controller-heartbeat");
  const refreshes = count("capacity-refresh");
  const other = rows.length - checks - refreshes;
  return [checks && plural(checks, "controller check"), refreshes && plural(refreshes, "capacity refresh", "es"), other && `${other} other`].filter(Boolean).join(", ");
}

function rawBlock(event: BridgeEvent, opened: Set<string>): HTMLElement {
  const key = `${event.id}|${event.kind}|${event.created_at}`;
  const entries: Array<[string, unknown]> = [["kind", event.kind], ["event", event.id > 0 ? event.id : null], ["task", event.task_id], ["time", event.created_at], ...Object.entries(allowedEventData(event.data))];
  const d = h("details", { class: "raw", open: opened.has(key) }, h("summary", {}, "Raw"), fields("raw-fields", entries.filter(([, v]) => v != null).map(([k, v]) => [k, h("code", { class: "long-text" }, String(v))])));
  d.addEventListener("toggle", () => (d.open ? opened.add(key) : opened.delete(key)));
  return d;
}

function eventRow({ event, cls }: Classed, opened: Set<string>): HTMLElement {
  const ms = Date.parse(event.created_at);
  return h(
    "li",
    { class: `event-row ${cls}`, "data-event-class": cls, "data-event-kind": event.kind },
    Number.isNaN(ms) ? h("span", { class: "muted" }, "-") : tsEl(ms),
    h("span", { class: "ev-phrase" }, humanizeEvent(event)),
    event.task_id != null ? h("span", { class: "task-chip num" }, `Task ${event.task_id}`) : null,
    rawBlock(event, opened),
  );
}

export function activityPanel(onOlder: () => void): ActivityPanel {
  let data: ActivityData = { events: [], error: "", loaded: false, more: false };
  let view: View = "milestones";
  let shown = PAGE;
  let plumbingOpen = false;
  let plumbingShown = PLUMBING_PAGE;
  const opened = new Set<string>();

  const el = h("div", { class: "activity-panel stack" });

  function render(): void {
    const classed: Classed[] = [...data.events].reverse().map((event) => ({ event, cls: eventClass(event.kind) }));
    const wanted = classed.filter((r) => r.cls === "milestone" || (view === "all" && r.cls === "detail"));
    const plumbing = classed.filter((r) => r.cls === "plumbing");
    const list = (cls: string, label: string, rows: Classed[], limit: number, id?: string) => h("ol", { class: cls, id, "aria-label": label }, ...rows.slice(0, limit).map((r) => eventRow(r, opened)));
    const seg = h(
      "div",
      { class: "seg small", role: "group", "aria-label": "Activity detail" },
      ...(Object.keys(VIEWS) as View[]).map((v) =>
        h("button", { type: "button", class: "seg-btn", "aria-pressed": String(view === v), "data-view": v, onclick: () => ((view = v), (shown = PAGE), render()) }, h("span", {}, VIEWS[v])),
      ),
    );
    const nodes: Node[] = [h("div", { class: "panel-head" }, h("h2", { class: "panel-title" }, "Activity"), seg)];
    if (data.error) nodes.push(note("notice", data.error));
    if (!data.loaded && !data.error) nodes.push(note("muted", "Loading activity..."));
    else if (wanted.length === 0)
      nodes.push(emptyState({ icon: "inbox", title: view === "all" ? "No activity yet" : "No milestones yet", text: "Task changes, approvals, hand-offs and failures appear here as they happen." }));
    else nodes.push(list("events", VIEWS[view], wanted, shown, "events-list"));
    const actions: Node[] = [];
    if (wanted.length > shown) actions.push(more(PAGE, wanted.length - shown, () => ((shown += PAGE), render())));
    if (data.more) actions.push(button("Load older events", onOlder, { kind: "small" }));
    if (actions.length) nodes.push(h("div", { class: "actions" }, ...actions));
    if (plumbing.length) {
      const toggle = button(plumbingOpen ? "Hide" : "Show", () => ((plumbingOpen = !plumbingOpen), render()), { kind: "small", label: `${plumbingOpen ? "Hide" : "Show"} background events` });
      nodes.push(
        h(
          "div",
          { class: "plumbing-foot muted small", "data-plumbing-count": plumbing.length },
          h("span", {}, `${plural(plumbing.length, "background event")} ${plumbingOpen ? "shown" : "hidden"} (${plumbingBreakdown(plumbing)})`),
          toggle,
        ),
      );
      if (plumbingOpen) {
        nodes.push(list("events plumbing muted", "Background events", plumbing, plumbingShown));
        if (plumbing.length > plumbingShown) nodes.push(more(PLUMBING_PAGE, plumbing.length - plumbingShown, () => ((plumbingShown += PLUMBING_PAGE), render())));
      }
    }
    // Keep the focused button (a view button, Show or Hide) across the rebuild.
    const swap = (): void => el.replaceChildren(...nodes);
    if (document.activeElement instanceof HTMLButtonElement) keepFocus(el, swap);
    else swap();
  }
  render();
  return {
    el,
    update(next) {
      data = next;
      render();
    },
  };
}
