import { defineConfig, devices } from "@playwright/test";

// Every test starts its own real coordinator (see e2e/fixtures.ts). The coordinator uses a self-signed
// local CA, so the E2E browsers run with ignoreHTTPSErrors (E2E only; the product never disables TLS checks).
export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  workers: 2,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: { ignoreHTTPSErrors: true, trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "chromium-desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "chromium-mobile", use: { ...devices["Pixel 7"] } },
    { name: "firefox-desktop", use: { ...devices["Desktop Firefox"], viewport: { width: 1280, height: 800 } } },
    // Firefox has no mobile emulation; this project only narrows the viewport (iPhone 12 size) and enables touch.
    { name: "firefox-mobile", use: { ...devices["Desktop Firefox"], viewport: { width: 390, height: 844 }, hasTouch: true } },
  ],
});
