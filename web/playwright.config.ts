import { defineConfig, devices } from "@playwright/test";

// Every test starts its own real coordinator (see e2e/fixtures.ts). The coordinator uses a self-signed
// local CA, so the E2E browsers run with ignoreHTTPSErrors (E2E only; the product never disables TLS checks).
//
// Projects: desktop Chromium and Firefox at 1440x900, Chromium on the built-in Pixel 4 profile, and a Firefox
// phone profile (Firefox has no mobile emulation: viewport, device scale factor and touch only).
// Visual baselines (e2e/screenshots.spec.ts) exist for the two Chromium projects only and are generated in the
// official Playwright Docker image (see web/README.md); the file name carries no platform suffix for that reason.
// T36 typing under a terminal flood (e2e/redesign/uxr-typing.spec.ts) measures wall-clock key-to-paint latency, which grows with every other busy
// process. It has a project of its own that every other project depends on, so Playwright runs it first and alone, before any parallel worker starts.
const TYPING_SPEC = /redesign[\\/]uxr-typing\.spec\.ts$/;
const aloneFirst = { dependencies: ["typing-flood"] };

// PRD-037 device matrix (docs/distributed/web-design-spec.md section 12, T45): nine more Chromium projects that run
// only the matrix spec (e2e/redesign/uxr-matrix.spec.ts: fold budgets, no horizontal scroll, target sizes, reflow, axe,
// keyboard simulation, navigation). The four projects above run every spec, the matrix spec included.
const MATRIX_SPEC = /redesign[\\/]uxr-matrix\.spec\.ts$/;
const matrix = (width: number, height: number, kind: "desktop" | "tablet" | "phone") => ({
  name: `matrix-${width}x${height}`,
  testMatch: MATRIX_SPEC,
  ...aloneFirst,
  use:
    kind === "desktop"
      ? { ...devices["Desktop Chrome"], viewport: { width, height } }
      : { ...devices["Pixel 4"], viewport: { width, height }, hasTouch: true, isMobile: true, deviceScaleFactor: kind === "tablet" ? 2 : 3 },
});

// The click-path inventory (UXR-043 to UXR-046): the paths, on-screen keyboard and thumb-reach specs also run at 320x568 and 360x640,
// the two smallest phones of the addendum, in two projects of their own. The four standard projects run them at 1440x900 and 353x745.
const PATHS_SPEC = /redesign[\\/]uxr-(paths|keyboard|keyboard-only|reach)\.spec\.ts$/;
const phonePaths = (width: number, height: number) => ({ name: `paths-${width}x${height}`, testMatch: PATHS_SPEC, ...aloneFirst, use: { ...devices["Pixel 4"], viewport: { width, height }, hasTouch: true, isMobile: true, deviceScaleFactor: 3 } });

export default defineConfig({
  testDir: "e2e",
  // Visual baselines are Linux-only (fonts and anti-aliasing differ on macOS and Windows).
  testIgnore: process.platform === "linux" ? [] : [/screenshots\.spec\.ts$/],
  timeout: 90_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: { maxDiffPixelRatio: 0.002, animations: "disabled", caret: "hide" },
  },
  snapshotPathTemplate: "{testDir}/__screenshots__/{projectName}/{arg}{ext}",
  fullyParallel: true,
  workers: process.env["CI"] ? 2 : 3,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: { ignoreHTTPSErrors: true, trace: "retain-on-failure", screenshot: "only-on-failure", locale: "en-US", timezoneId: "UTC" },
  projects: [
    { name: "typing-flood", testMatch: TYPING_SPEC, use: { ...devices["Pixel 4"] } },
    { name: "chromium-desktop", testIgnore: TYPING_SPEC, ...aloneFirst, use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "chromium-mobile", testIgnore: TYPING_SPEC, ...aloneFirst, use: { ...devices["Pixel 4"] } },
    { name: "firefox-desktop", testIgnore: TYPING_SPEC, ...aloneFirst, use: { ...devices["Desktop Firefox"], viewport: { width: 1440, height: 900 } } },
    // 393x851 CSS px at device scale factor 2.75 (the size the operator asked for); touch enabled.
    { name: "firefox-mobile", testIgnore: TYPING_SPEC, ...aloneFirst, use: { ...devices["Desktop Firefox"], viewport: { width: 393, height: 851 }, deviceScaleFactor: 2.75, hasTouch: true } },
    matrix(2560, 1440, "desktop"),
    matrix(1280, 720, "desktop"),
    matrix(768, 1024, "tablet"),
    matrix(1024, 768, "tablet"),
    matrix(430, 932, "phone"),
    matrix(360, 640, "phone"),
    matrix(320, 568, "phone"),
    matrix(740, 360, "phone"),
    matrix(568, 320, "phone"),
    phonePaths(320, 568),
    phonePaths(360, 640),
  ],
});
