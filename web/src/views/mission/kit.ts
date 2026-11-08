// Small helpers shared by the lazily loaded modules of the mission page.
import type { WorkerInfo } from "../../api";
import { absTime, button, copyText, h, hooks, relFromMs } from "../../dom";
import { icon } from "../../icons";
import { announce } from "../../ui";
import type { Task } from "./types";

/** Copy `text` to the clipboard and say so (`what` names it in the message). */
export const copy = (text: string, what: string): void => void copyText(text).then((ok) => hooks.say(ok ? `Copied ${what}.` : `Could not copy the ${what}; select it and copy manually.`));

export const taskTitle = (t: Task): string => t.title || `Task ${t.ordinal}`;

/** " since 14:02" for a worker that has been seen, else "". */
export const since = (w: WorkerInfo | undefined): string => (w?.last_seen ? ` since ${absTime(new Date(w.last_seen * 1000)).slice(11)}` : "");

/** The "completed or cancelled" test for a mission status. */
export const finished = (status: string): boolean => status === "COMPLETED" || status === "CANCELLED";

/** A `<time>` with the relative form as text and the absolute form as title. */
export const tsEl = (ms: number): HTMLElement => h("time", { class: "ts", datetime: new Date(ms).toISOString(), "data-ms": ms, title: absTime(new Date(ms)) }, h("span", { class: "rel" }, relFromMs(ms)));

/** Announce why a change is refused (`why` is the reason, "" when allowed); true when it was refused. */
export function refused(why: string, action = ""): boolean {
  if (why) announce(action ? `Could not ${action}. ${why}` : why, "failed");
  return Boolean(why);
}

/** The `disabled` and `reason` fields of a menu item that is off while `why` is not empty. */
export const gate = (why: string): { disabled: boolean; reason: string } => ({ disabled: Boolean(why), reason: why });

/** The "Show 20 more" button of a list that is cut at `step` rows. */
export const more = (step: number, left: number, go: () => void): HTMLButtonElement => button(`Show ${Math.min(step, left)} more`, go, { kind: "small" });

/** A definition list of label and value pairs. */
export const fields = (cls: string, rows: Array<[string, Node | string]>): HTMLElement => h("dl", { class: cls }, ...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));

/** The breadcrumb back to the mission list, with or without the chevron. */
export const crumbs = (chevron: boolean): HTMLElement => h("nav", { "aria-label": "Breadcrumb", class: "crumbs" }, h("a", { href: "/missions", class: "back" }, chevron ? icon("chevronLeft", 18) : null, h("span", {}, "Missions")));

/** A paragraph that screen readers announce: the error of a panel or its loading text. */
export const note = (cls: "notice" | "muted", text: string): HTMLElement => h("p", { class: cls, role: "status" }, text);

/** An object, else `{}`: the status projection is JSON of an unknown shape. */
export const record = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

/** A status or name the worker reports that is worth showing: not empty and not "unknown". */
export const known = (v: unknown): v is string => typeof v === "string" && v.trim() !== "" && v.toLowerCase() !== "unknown";
