// The reserved box for the session pane, and the Back guard of its full-screen mode. This module is part of the first load (a few
// bytes): a page reserves the pane's size with `sessionReserve()` before the lazy terminal chunk arrives and replaces it with
// `sessionPane(...).element`, which has the same size, so nothing shifts.
import { h } from "./dom";

export function sessionReserve(): HTMLElement {
  return h("section", { class: "session-reserve", "data-pane": "session-pending", "aria-busy": "true", "aria-label": "Live session" });
}

/**
 * Back guard for the session pane's full-screen mode. This listener is registered when the module is evaluated, before the router's
 * `popstate` listener (main.ts imports the page modules first), so Back leaves full screen without the router re-rendering the page.
 * The pane (session-extras.ts) hears it as a `session-pop` event on the document.
 */
window.addEventListener("popstate", (e) => {
  if (!document.body.hasAttribute("data-session-fs") && !document.body.hasAttribute("data-session-back")) return;
  e.stopImmediatePropagation();
  document.dispatchEvent(new Event("session-pop"));
}, true);
