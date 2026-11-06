import { expect, expectAccessible, expectNoHorizontalScroll, loginViaUi, navTo, PASSWORD, setupViaUi, signOutViaUi, test } from "./fixtures";

test("first-run setup signs in and shows the overview; unauthenticated visits go to login", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("#conn")).toContainText("Live");
  await expect(page.getByText("Workers online")).toBeVisible();
  await expect(page.getByText("healthy")).toBeVisible();
  // The setup token is consumed.
  await signOutViaUi(page);
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.locator("#login-notice")).toContainText("You signed out");
  // A deep link while signed out lands on login, not on data.
  await page.goto("/missions");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("wrong password shows an error; correct password signs in; logout ends the session", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await signOutViaUi(page);
  await page.getByLabel("Username", { exact: true }).fill("admin");
  await page.getByLabel("Password", { exact: true }).fill("wrong password!!");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("#form-error")).toContainText("Username or password is incorrect. Check both and try again.");
  await loginViaUi(page);
  await signOutViaUi(page);
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/workers");
  await expect(page).toHaveURL(/\/login$/);
});

test("setup rejects a wrong token and mismatched passwords", async ({ page, coord }) => {
  await page.goto("/");
  await page.getByLabel("Setup token").fill("not-the-token");
  await page.getByLabel("Administrator username").fill("admin");
  await page.getByLabel("New password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Repeat password").fill("different password");
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page.locator("#password-confirm-error")).toContainText("do not match");
  await page.getByLabel("Repeat password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page.locator("#setup-token-error")).toContainText("not the setup token");
  expect(coord.setupToken()).not.toBe("");
});

test("workers added with the CLI are listed as offline", async ({ page, coord }) => {
  coord.cli("worker", "add", "alpha");
  coord.cli("worker", "add", "beta");
  await setupViaUi(page, coord);
  await navTo(page, "Workers");
  await expect(page.getByRole("heading", { name: "Workers" })).toBeVisible();
  const rows = page.locator("#workers-table tbody tr");
  await expect(rows).toHaveCount(2);
  for (const name of ["alpha", "beta"]) {
    const row = page.locator(`tr[data-worker="${name}"]`);
    await expect(row.getByText("offline", { exact: true })).toBeVisible();
    await expect(row).toContainText("never");
  }
  await expectNoHorizontalScroll(page);
});

test("missions index is empty without workers and the SPA serves deep links", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await page.goto("/missions");
  await expect(page.getByRole("heading", { name: "Missions", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No missions yet" })).toBeVisible();
  await page.goto("/missions/nobody/none");
  await expect(page.getByRole("heading", { name: "none" })).toBeVisible();
  await expect(page.locator("#terminal-region")).toBeVisible();
  await page.goto("/does/not/exist");
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
});

test("keyboard: skip link, navigation and sign-in work without a mouse", async ({ page, coord, browserName }) => {
  test.skip(browserName === "webkit");
  await setupViaUi(page, coord);
  await signOutViaUi(page);
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).focus();
  await page.keyboard.type("admin");
  await page.keyboard.press("Tab");
  await page.keyboard.type(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  // main has focus after navigation; the first Tab stop is the skip link.
  // Move the sequential focus starting point to the top of the document, as a fresh page load does.
  await page.evaluate(() => {
    const marker = document.createElement("span");
    marker.tabIndex = -1;
    document.body.prepend(marker);
    marker.focus();
    marker.remove();
  });
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  // Keyboard focus is visible: the focused element has an outline.
  expect(await page.evaluate(() => getComputedStyle(document.activeElement as Element).outlineStyle)).not.toBe("none");
  const name = () => page.evaluate(() => (document.activeElement?.getAttribute("aria-label") || document.activeElement?.textContent || "").trim().slice(0, 40));
  const reached: string[] = [];
  for (let i = 0; i < 14; i++) {
    await page.keyboard.press("Tab");
    reached.push(await name());
  }
  const wide = (page.viewportSize()?.width ?? 0) >= 960;
  for (const label of ["LLM Bridge", "Overview", "Workers", "Missions", wide ? "Diagnostics" : "More"]) expect(reached.some((r) => r.includes(label)), `${label} in ${reached.join(" | ")}`).toBe(true);
  expect(reached.some((r) => r.startsWith("Theme:"))).toBe(true);
  expect(reached.some((r) => r.includes("account menu"))).toBe(true);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Workers" }).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/workers$/);
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Workers" })).toHaveAttribute("aria-current", "page");
});

test("a live refresh of the overview keeps keyboard focus on the same tile", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  // Hold the diagnostics reply so the re-render it causes happens after a tile has focus.
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/v1/diagnostics", async (route) => {
    await held;
    await route.continue();
  });
  await page.goto("/");
  const tile = page.locator('[data-tile="missions-running"]');
  await tile.focus();
  await tile.evaluate((el) => el.setAttribute("data-before-refresh", ""));
  release();
  await expect(page.locator("#ov-health")).toContainText("Coordinator healthy");
  // The tile element was replaced by the re-render, and focus moved to its replacement.
  await expect(page.locator("[data-before-refresh]")).toHaveCount(0);
  await expect(tile).toBeFocused();
});

test("theme toggle cycles and persists", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  const toggle = page.locator("#theme-toggle");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-label", /Theme: light/);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await toggle.click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("password change signs out and the new password works", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await navTo(page, "Account");
  await page.getByLabel("Current password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("New password", { exact: true }).fill("another long passphrase");
  await page.getByLabel("Repeat new password", { exact: true }).fill("another long passphrase");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.locator("#login-notice")).toContainText("Password changed");
  await loginViaUi(page, "admin", "another long passphrase");
});

test("diagnostics shows coordinator data", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await navTo(page, "Diagnostics");
  await expect(page.getByRole("heading", { name: "Coordinator" })).toBeVisible();
  await expect(page.getByRole("row", { name: /Protocol/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /Live stream live/ })).toBeVisible();
});

test("static responses carry the security headers and the app logs no console errors", async ({ page, coord, request }) => {
  const r = await request.get(coord.url + "/");
  const h = r.headers();
  expect(h["content-security-policy"]).toContain("default-src 'self'");
  // xterm.js injects <style> elements, so only style-src allows 'unsafe-inline'; scripts stay 'self' only.
  const csp = h["content-security-policy"] ?? "";
  expect(csp).toContain("script-src 'self';");
  expect(csp.replace("style-src 'self' 'unsafe-inline'", "")).not.toContain("unsafe-inline");
  expect(h["x-content-type-options"]).toBe("nosniff");
  expect(h["referrer-policy"]).toBe("no-referrer");
  expect(h["x-frame-options"]).toBe("DENY");
  expect(h["strict-transport-security"]).toBeTruthy();
  // No console errors (including CSP violations) while using the app.
  const problems: string[] = [];
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(String(e)));
  await setupViaUi(page, coord);
  await navTo(page, "Diagnostics");
  await expect(page.getByRole("heading", { name: "Coordinator" })).toBeVisible();
  expect(problems).toEqual([]);
});

test("pages have no horizontal scroll and no accessibility violations", async ({ page, coord }) => {
  coord.cli("worker", "add", "alpha");
  await page.goto("/");
  await expectNoHorizontalScroll(page);
  await expectAccessible(page, "setup");
  await setupViaUi(page, coord);
  for (const name of ["Overview", "Workers", "Missions", "Sessions", "Usage", "Diagnostics", "Account"] as const) {
    await navTo(page, name);
    await expect(page.getByRole("heading", { name: name === "Usage" ? "Provider usage" : name, exact: true })).toBeVisible();
    if (name === "Diagnostics") await expect(page.getByRole("heading", { name: "Coordinator" })).toBeVisible();
    await expectNoHorizontalScroll(page);
    await expectAccessible(page, name);
  }
  await signOutViaUi(page);
  await expectAccessible(page, "login");
});
