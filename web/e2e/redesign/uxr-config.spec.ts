// T45 (UXR-037): playwright.config.ts defines the nine matrix projects and CI runs them (and keeps the typing-latency test T36 alone).
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import config from "../../playwright.config";
import { expect, uxrStatic } from "./gate";

const repo = resolve(import.meta.dirname, "../../..");
const MATRIX: Array<[number, number]> = [[2560, 1440], [1280, 720], [768, 1024], [1024, 768], [430, 932], [360, 640], [320, 568], [740, 360], [568, 320]];

uxrStatic({ t: "T45", uxr: ["UXR-037"], needs: [] }, "playwright.config.ts defines the nine matrix projects next to the four existing ones", async () => {
  const projects = config.projects ?? [];
  const names = projects.map((p) => p.name);
  for (const existing of ["chromium-desktop", "chromium-mobile", "firefox-desktop", "firefox-mobile"]) expect(names, `existing project ${existing}`).toContain(existing);
  for (const [w, h] of MATRIX) {
    const project = projects.find((p) => p.name === `matrix-${w}x${h}`);
    expect(project, `matrix project ${w}x${h}`).toBeDefined();
    expect(project?.use?.viewport, `viewport of matrix-${w}x${h}`).toEqual({ width: w, height: h });
    expect(String(project?.testMatch), `matrix-${w}x${h} runs the matrix spec only`).toContain("uxr-matrix");
  }
  // Two more projects run only the click-path specs (UXR-043 to UXR-046) at the two smallest phone sizes.
  for (const name of ["paths-320x568", "paths-360x640"]) expect(names, `click-path project ${name}`).toContain(name);
  // T36 typing under a terminal flood runs in a project of its own that every other project depends on, so it is the only test running while it measures.
  const typing = projects.find((p) => p.name === "typing-flood");
  expect(String(typing?.testMatch), "typing-flood runs the typing spec only").toContain("uxr-typing");
  for (const p of projects.filter((q) => q.name !== "typing-flood")) expect(p.dependencies, `${p.name} waits for typing-flood`).toEqual(["typing-flood"]);
  expect(projects.length, "typing-flood, four existing projects, nine matrix projects and two click-path projects").toBe(16);
});

uxrStatic({ t: "T45", uxr: ["UXR-037"], needs: [] }, "the CI workflow runs the matrix projects", async () => {
  const workflow = readFileSync(join(repo, ".github", "workflows", "test.yml"), "utf8");
  expect(workflow, "a CI step runs the matrix projects").toMatch(/--project=["']?matrix-\*|--project=matrix-2560x1440/);
  for (const [w, h] of MATRIX) {
    const pattern = new RegExp(`matrix-\\*|matrix-${w}x${h}`);
    expect(workflow, `CI covers matrix-${w}x${h}`).toMatch(pattern);
  }
});
