import { expect, expectAccessible, expectNoHorizontalScroll, loginViaUi, PASSWORD, setupViaUi, test } from "./fixtures";

test("first-run setup signs in and shows the overview; unauthenticated visits go to login", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("#conn")).toContainText("Live");
  await expect(page.getByText("Workers online")).toBeVisible();
  await expect(page.getByText("healthy")).toBeVisible();
  // The setup token is consumed.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  // A deep link while signed out lands on login, not on data.
  await page.goto("/missions");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("wrong password shows an error; correct password signs in; logout ends the session", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password", { exact: true }).fill("wrong password!!");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("invalid username or password");
  await loginViaUi(page);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/workers");
  await expect(page).toHaveURL(/\/login$/);
});

test("setup rejects a wrong token and mismatched passwords", async ({ page, coord }) => {
  await page.goto("/");
  await page.getByLabel("Setup token").fill("not-the-token");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("New password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Repeat password").fill("different password");
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page.getByRole("alert")).toContainText("do not match");
  await page.getByLabel("Repeat password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page.getByRole("alert")).toContainText("invalid setup token");
  expect(coord.setupToken()).not.toBe("");
});

test("workers added with the CLI are listed as offline", async ({ page, coord }) => {
  coord.cli("worker", "add", "alpha");
  coord.cli("worker", "add", "beta");
  await setupViaUi(page, coord);
  await page.getByRole("link", { name: "Workers" }).click();
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
  await expect(page.getByText("No missions.")).toBeVisible();
  await page.goto("/missions/nobody/none");
  await expect(page.getByRole("heading", { name: "none" })).toBeVisible();
  await expect(page.locator("#terminal-region")).toBeVisible();
  await page.goto("/does/not/exist");
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
});

test("keyboard: skip link, navigation and sign-in work without a mouse", async ({ page, coord, browserName }) => {
  test.skip(browserName === "webkit");
  await setupViaUi(page, coord);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.goto("/login");
  await page.getByLabel("Username").focus();
  await page.keyboard.type("admin");
  await page.keyboard.press("Tab");
  await page.keyboard.type(PASSWORD);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Sign in" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  // main has focus after navigation; shift-tab backwards then tab forward reaches the skip link first.
  await page.locator("body").press("Home");
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  const reached: string[] = [];
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press("Tab");
    reached.push((await page.evaluate(() => document.activeElement?.textContent?.trim() ?? "")).slice(0, 30));
  }
  for (const label of ["Overview", "Workers", "Missions", "Diagnostics", "Account", "Theme: auto", "Sign out"]) expect(reached).toContain(label);
  // Focus is visible: the focused element has a non-zero outline.
  const outline = await page.evaluate(() => getComputedStyle(document.activeElement as Element).outlineStyle);
  expect(outline).not.toBe("none");
  await page.getByRole("link", { name: "Workers" }).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/workers$/);
  await expect(page.getByRole("link", { name: "Workers" })).toHaveAttribute("aria-current", "page");
});

test("theme toggle cycles and persists", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  const toggle = page.locator("#theme-toggle");
  await toggle.click();
  await expect(toggle).toHaveText("Theme: light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await toggle.click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("password change signs out and the new password works", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await page.getByRole("link", { name: "Account" }).click();
  await page.getByLabel("Current password").fill(PASSWORD);
  await page.getByLabel("New password", { exact: true }).fill("another long passphrase");
  await page.getByLabel("Repeat new password").fill("another long passphrase");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByText("Password changed")).toBeVisible();
  await loginViaUi(page, "admin", "another long passphrase");
});

test("diagnostics shows coordinator data", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await page.getByRole("link", { name: "Diagnostics" }).click();
  await expect(page.getByRole("heading", { name: "Coordinator" })).toBeVisible();
  await expect(page.getByRole("row", { name: /Protocol/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /Live stream live/ })).toBeVisible();
});

test("static responses carry the security headers and the app logs no console errors", async ({ page, coord, request }) => {
  const r = await request.get(coord.url + "/");
  const h = r.headers();
  expect(h["content-security-policy"]).toContain("default-src 'self'");
  expect(h["content-security-policy"]).not.toContain("unsafe-inline");
  expect(h["x-content-type-options"]).toBe("nosniff");
  expect(h["referrer-policy"]).toBe("no-referrer");
  expect(h["x-frame-options"]).toBe("DENY");
  expect(h["strict-transport-security"]).toBeTruthy();
  // No console errors (including CSP violations) while using the app.
  const problems: string[] = [];
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(String(e)));
  await setupViaUi(page, coord);
  await page.getByRole("link", { name: "Diagnostics" }).click();
  await expect(page.getByRole("heading", { name: "Coordinator" })).toBeVisible();
  expect(problems).toEqual([]);
});

test("pages have no horizontal scroll and no accessibility violations", async ({ page, coord }) => {
  coord.cli("worker", "add", "alpha");
  await page.goto("/");
  await expectNoHorizontalScroll(page);
  await expectAccessible(page, "setup");
  await setupViaUi(page, coord);
  for (const [path, heading] of [
    ["/", "Overview"],
    ["/workers", "Workers"],
    ["/missions", "Missions"],
    ["/diagnostics", "Diagnostics"],
    ["/account", "Account"],
  ] as const) {
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: heading }).click();
    await expect(page).toHaveURL(new RegExp(`${path === "/" ? "/$" : path + "$"}`));
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    if (path === "/diagnostics") await expect(page.getByRole("heading", { name: "Coordinator" })).toBeVisible();
    await expectNoHorizontalScroll(page);
    await expectAccessible(page, path);
  }
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expectAccessible(page, "login");
});
