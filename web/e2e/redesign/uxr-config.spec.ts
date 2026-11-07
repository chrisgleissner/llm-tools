// T45 (UXR-037): playwright.config.ts defines the nine matrix projects and CI runs them.
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
  expect(projects.length, "four existing projects and nine matrix projects").toBe(13);
});

uxrStatic({ t: "T45", uxr: ["UXR-037"], needs: [] }, "the CI workflow runs the matrix projects", async () => {
  const workflow = readFileSync(join(repo, ".github", "workflows", "test.yml"), "utf8");
  expect(workflow, "a CI step runs the matrix projects").toMatch(/--project=["']?matrix-\*|--project=matrix-2560x1440/);
  for (const [w, h] of MATRIX) {
    const pattern = new RegExp(`matrix-\\*|matrix-${w}x${h}`);
    expect(workflow, `CI covers matrix-${w}x${h}`).toMatch(pattern);
  }
});
