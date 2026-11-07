// The reserved box for the session pane. It is part of the first load (a few bytes), so a page can reserve the pane's size before
// the lazy terminal chunk arrives and replace it with `sessionPane(...).element`, which has the same size: no layout shift.
import { h } from "./dom";

export function sessionReserve(): HTMLElement {
  return h("section", { class: "session-reserve", "data-pane": "session-pending", "aria-busy": "true", "aria-label": "Live session" });
}
