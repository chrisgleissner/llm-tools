// The six pages of the redesigned console, the work packages each depends on, and how to get each into a populated state.
import type { MockOptions } from "../mock";
import { MISSION_URL } from "./harness";
import type { Wp } from "./gate";

export interface PageDef {
  id: "inbox" | "missions" | "mission" | "sessions" | "workers" | "usage";
  path: string;
  /** Mock for a populated page (dense data where the page has lists). */
  mock: MockOptions;
  /** Work package that builds the page; every page also needs WP-0 (tokens, pill) and WP-1 (shell). */
  wp: Wp;
  /** Selector that proves the page content (not a skeleton) is on screen. */
  ready: string;
}

export const PAGES: PageDef[] = [
  { id: "inbox", path: "/", mock: { auth: "in", dataset: "dense" }, wp: "WP-4", ready: '[data-section="needs-you"]' },
  { id: "missions", path: "/missions", mock: { auth: "in", dataset: "dense" }, wp: "WP-4", ready: '[data-entity^="mission:"]' },
  { id: "mission", path: MISSION_URL, mock: { auth: "in", terminalScreen: true }, wp: "WP-3", ready: '[data-pane="session"]' },
  { id: "sessions", path: "/sessions", mock: { auth: "in" }, wp: "WP-5", ready: '[data-entity^="session:"]' },
  { id: "workers", path: "/workers", mock: { auth: "in" }, wp: "WP-5", ready: '[data-entity^="worker:"]' },
  { id: "usage", path: "/usage", mock: { auth: "in" }, wp: "WP-5", ready: '[data-entity^="provider:"]' },
];

/** Packages every page test needs: foundations, shell and the page itself. */
export const needsFor = (page: PageDef): Wp[] => ["WP-0", "WP-1", page.wp];
