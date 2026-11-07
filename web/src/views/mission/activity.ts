// The Activity panel (spec 8.1): milestones by default, "All activity" adds detail events, plumbing is counted and hidden
// behind a Show button. Each row has a "Raw" disclosure that shows only the allowlisted event fields.
import type { BridgeEvent } from "../../api";
import { absTime, button, emptyState, h, relFromMs } from "../../dom";
import { allowedEventData, humanizeEvent } from "../../event-humanizer";
import { eventClass, type EventClass } from "../../event-class";

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

type View = "milestones" | "all";

interface Classed {
  event: BridgeEvent;
  cls: EventClass;
}

function plumbingBreakdown(rows: Classed[]): string {
  const checks = rows.filter((r) => r.event.kind.startsWith("controller-heartbeat")).length;
  const refreshes = rows.filter((r) => r.event.kind.startsWith("capacity-refresh")).length;
  const other = rows.length - checks - refreshes;
  const parts: string[] = [];
  if (checks) parts.push(`${checks} controller ${checks === 1 ? "check" : "checks"}`);
  if (refreshes) parts.push(`${refreshes} capacity ${refreshes === 1 ? "refresh" : "refreshes"}`);
  if (other) parts.push(`${other} other`);
  return parts.join(", ");
}

function rawBlock(event: BridgeEvent, opened: Set<string>): HTMLElement {
  const key = `${event.id}|${event.kind}|${event.created_at}`;
  const fields = allowedEventData(event.data);
  const entries: Array<[string, string]> = [["kind", event.kind]];
  if (event.id > 0) entries.push(["event", String(event.id)]);
  if (event.task_id != null) entries.push(["task", String(event.task_id)]);
  entries.push(["time", event.created_at]);
  for (const [k, v] of Object.entries(fields)) entries.push([k, String(v)]);
  const d = h("details", { class: "raw", open: opened.has(key) }, h("summary", {}, "Raw"), h("dl", { class: "raw-fields" }, ...entries.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, h("code", { class: "long-text" }, v))])));
  d.addEventListener("toggle", () => (d.open ? opened.add(key) : opened.delete(key)));
  return d;
}

function eventRow({ event, cls }: Classed, opened: Set<string>): HTMLElement {
  const ms = Date.parse(event.created_at);
  return h(
    "li",
    { class: `event-row ${cls}`, "data-event-class": cls, "data-event-kind": event.kind },
    Number.isNaN(ms)
      ? h("span", { class: "muted" }, "-")
      : h("time", { class: "ts", datetime: new Date(ms).toISOString(), "data-ms": ms, title: absTime(new Date(ms)) }, h("span", { class: "rel" }, relFromMs(ms))),
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

  function setView(next: View): void {
    view = next;
    shown = PAGE;
    render();
  }

  function render(): void {
    const classed: Classed[] = [...data.events].reverse().map((event) => ({ event, cls: eventClass(event.kind) }));
    const wanted = classed.filter((r) => r.cls === "milestone" || (view === "all" && r.cls === "detail"));
    const plumbing = classed.filter((r) => r.cls === "plumbing");
    const seg = h(
      "div",
      { class: "seg small", role: "group", "aria-label": "Activity detail" },
      ...(["milestones", "all"] as const).map((v) => {
        const b = h("button", { type: "button", class: "seg-btn", "aria-pressed": String(view === v), "data-view": v }, h("span", {}, v === "milestones" ? "Milestones" : "All activity"));
        b.addEventListener("click", () => setView(v));
        return b;
      }),
    );
    const nodes: Node[] = [h("div", { class: "panel-head" }, h("h2", { class: "panel-title" }, "Activity"), seg)];
    if (data.error) nodes.push(h("p", { class: "notice", role: "status" }, data.error));
    if (!data.loaded && !data.error) nodes.push(h("p", { class: "muted", role: "status" }, "Loading activity..."));
    else if (wanted.length === 0)
      nodes.push(emptyState({ icon: "inbox", title: view === "milestones" ? "No milestones yet" : "No activity yet", text: "Task changes, approvals, hand-offs and failures appear here as they happen." }));
    else nodes.push(h("ol", { class: "events", id: "events-list", "aria-label": view === "milestones" ? "Milestones" : "All activity" }, ...wanted.slice(0, shown).map((r) => eventRow(r, opened))));
    const actions: Node[] = [];
    if (wanted.length > shown) actions.push(button(`Show ${Math.min(PAGE, wanted.length - shown)} more`, () => { shown += PAGE; render(); }, { kind: "small" }));
    if (data.more) actions.push(button("Load older events", onOlder, { kind: "small" }));
    if (actions.length) nodes.push(h("div", { class: "actions" }, ...actions));
    if (plumbing.length) {
      const breakdown = plumbingBreakdown(plumbing);
      const toggle = button(plumbingOpen ? "Hide" : "Show", () => { plumbingOpen = !plumbingOpen; render(); }, { kind: "small", label: plumbingOpen ? "Hide background events" : "Show background events" });
      nodes.push(
        h(
          "div",
          { class: "plumbing-foot muted small", "data-plumbing-count": plumbing.length },
          h("span", {}, `${plumbing.length} background ${plumbing.length === 1 ? "event" : "events"} ${plumbingOpen ? "shown" : "hidden"} (${breakdown})`),
          toggle,
        ),
      );
      if (plumbingOpen) {
        nodes.push(h("ol", { class: "events plumbing muted", "aria-label": "Background events" }, ...plumbing.slice(0, plumbingShown).map((r) => eventRow(r, opened))));
        if (plumbing.length > plumbingShown) nodes.push(button(`Show ${Math.min(PLUMBING_PAGE, plumbing.length - plumbingShown)} more`, () => { plumbingShown += PLUMBING_PAGE; render(); }, { kind: "small" }));
      }
    }
    // Keep the focused control (a view button, Show or Hide) across the rebuild.
    const active = document.activeElement as HTMLElement | null;
    const key = active && el.contains(active) ? (active.getAttribute("data-view") ?? active.getAttribute("aria-label") ?? active.textContent ?? "") : null;
    el.replaceChildren(...nodes);
    if (key !== null && !(document.activeElement && el.contains(document.activeElement))) {
      for (const c of el.querySelectorAll<HTMLElement>("button")) {
        if ((c.getAttribute("data-view") ?? c.getAttribute("aria-label") ?? c.textContent ?? "") === key) {
          c.focus({ preventScroll: true });
          break;
        }
      }
    }
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
