// Screens owned by the session package (PRD-037). Names are baseline file names, so they stay unique across packages.
import { expect } from "../fixtures";
import { IN, live, type Screen } from "./types";
import { MISSION_PATH } from "../mock";

export const SCREENS: Screen[] = [
  { name: "mission-live-output", mock: { ...IN, terminalScreen: true }, path: MISSION_PATH, run: async (p) => { await expect(p.locator("#terminal-status")).toContainText("Live - view only"); await expect(p.locator("#terminal-host .xterm-rows")).toContainText("Reading src/tokenizer.rs"); } },
];
