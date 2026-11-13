// Helpers shared by the lazily loaded account and diagnostics pages.
import * as api from "./api";
import { h } from "./dom";

/** The coordinator's own message for a failed request, or `fallback` when the failure has none. */
export const apiMsg = (error: unknown, fallback: string): string => (error instanceof api.ApiError ? error.message : fallback);

/** A definition list of `[term, value]` rows. A missing value reads "-". */
export const facts = (cls: string, label: string, rows: Array<[string, unknown]>): HTMLElement =>
  h("dl", { class: cls, "aria-label": label }, ...rows.flatMap(([term, value]) => [h("dt", {}, term), h("dd", {}, value === null || value === undefined || value === "" ? "-" : String(value))]));

/** A plain button: `label` is its text, `cls` its class, `extra` more attributes. */
export const btn = (label: string, onclick: () => void, cls = "btn", extra: Record<string, string> = {}): HTMLButtonElement => h("button", { type: "button", class: cls, onclick, ...extra }, label);
