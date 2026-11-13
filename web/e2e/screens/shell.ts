// Screens owned by the shell package (PRD-037). Names are baseline file names, so they stay unique across packages.
import { expect } from "../fixtures";
import { IN, heading, live, type Screen } from "./types";

export const SCREENS: Screen[] = [
  { name: "user-menu", mock: { ...IN, sessionRemembered: true }, path: "/", run: async (p) => { await live(p); await p.locator("#account-button").click(); await expect(p.getByRole("menuitem", { name: "Sign out" })).toBeVisible(); } },
  { name: "shortcut-help", mock: IN, path: "/", run: async (p) => { await live(p); await p.keyboard.press("?"); await expect(p.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible(); } },
  { name: "command-palette", mock: IN, path: "/", run: async (p) => { await live(p); await p.keyboard.press("Control+k"); await p.keyboard.type("release"); await expect(p.getByRole("option")).toHaveCount(2); await expect(p.getByRole("option").first()).toHaveAttribute("aria-selected", "true"); } },
  // The stream is refused after the page has loaded its data over HTTP; the banner appears after the 4 s debounce and shows the (fixed) last sync time.
  { name: "conn-banner", mock: IN, path: "/", run: async (p) => { await live(p); await p.routeWebSocket(/\/api\/v1\/stream/, (ws) => void ws.close({ code: 1011, reason: "stream down (test)" })); await p.reload(); await expect(p.locator("#conn-banner")).toContainText("Showing data as of", { timeout: 15_000 }); } },
  { name: "not-found", mock: IN, path: "/nowhere", run: (p) => heading(p, "Not found") },
];
