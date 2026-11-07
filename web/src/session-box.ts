// The reserved box for the session pane. This module is part of the first load (a few bytes): a page reserves the pane's size with `sessionReserve()` before the lazy terminal chunk arrives and replaces it with
// `sessionPane(...).element`, which has the same size, so nothing shifts.
import { h } from "./dom";

export function sessionReserve(): HTMLElement {
  return h("section", { class: "session-reserve", "data-pane": "session-pending", "aria-busy": "true", "aria-label": "Live session" });
}
