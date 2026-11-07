// Contract for the per-package screen lists that T42 turns into visual baselines. A page package adds
// `web/e2e/screens/<package>.ts` that exports `screens: ScreenDef[]`; file names decide the work package that gates it
// (see PACKAGE_OF in uxr-baselines.spec.ts). Screen names are unique across packages and prefixed by the package
// ("inbox-dense", "mission-error"), so baselines of different packages never share a file.
import type { Page } from "@playwright/test";
import type { MockOptions } from "../mock";

export interface ScreenDef {
  name: string;
  mock: MockOptions;
  path: string;
  /** Drive the page into the state to capture and wait until it is ready (no screenshot yet). */
  run: (page: Page) => Promise<void>;
  /** Inbox and Mission detail need all four variants. */
  variant?: "empty" | "sparse" | "dense" | "error";
  narrowOnly?: boolean;
  wideOnly?: boolean;
}
