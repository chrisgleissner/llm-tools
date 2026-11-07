// Screens owned by the shell package (PRD-037). Names are baseline file names, so they stay unique across packages.
import { expect } from "../fixtures";
import { IN, heading, live, type Screen } from "./types";

export const SCREENS: Screen[] = [
  { name: "account-menu", mock: { ...IN, sessionRemembered: true }, path: "/", run: async (p) => { await live(p); await p.locator("#account-button").click(); await expect(p.locator("#account-panel")).toBeVisible(); } },
  { name: "shortcut-help", mock: IN, path: "/", run: async (p) => { await live(p); await p.locator("#account-button").click(); await p.locator("#menu-shortcuts").click(); await expect(p.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible(); } },
  { name: "more-sheet", mock: IN, path: "/", narrowOnly: true, run: async (p) => { await live(p); await p.locator("#more-button").click(); await expect(p.locator("#more-sheet")).toBeVisible(); } },
  { name: "not-found", mock: IN, path: "/nowhere", run: (p) => heading(p, "Not found") },
];
