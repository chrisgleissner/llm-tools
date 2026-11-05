// First-run registration, sign-in, sessions and the account menu (AUTH-002, AUTH-005, AUTH-006, WEB-003, WEB-009).
import { expect, expectAccessible, loginViaUi, navTo, PASSWORD, setupViaUi, signOutViaUi, test } from "./fixtures";

test.describe("first-run registration", () => {
  test("explains the setup token, states the policy, shows strength live and validates inline", async ({ page, coord }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "First-run setup" })).toBeVisible();
    // Where to find the setup token, and what the server enforces, are visible before anything is typed.
    await expect(page.locator("#token-help")).toContainText("bridge-coordinator setup-token");
    await expect(page.locator("#token-help")).toContainText("setup-token");
    await expect(page.locator("#policy-list li")).toHaveCount(2);
    await expect(page.locator('#policy-list li[data-rule="min"]')).toContainText("At least 12 characters");
    await expect(page.locator('#policy-list li[data-rule="min"]')).not.toHaveClass(/met/);

    const pass = page.getByLabel("New password", { exact: true });
    const strengthText = page.locator("#strength-text");
    await pass.fill("short");
    await expect(strengthText).toContainText("Too short");
    await expect(strengthText).toContainText("5 of 12");
    await expect(page.locator(".meter")).toHaveAttribute("data-level", "0");
    await pass.fill("aaaaaaaaaaaaaaaa");
    await expect(strengthText).toContainText("Weak");
    await expect(page.locator('#policy-list li[data-rule="min"]')).toHaveClass(/met/);
    await pass.fill("correct horse battery staple");
    await expect(strengthText).toContainText("Strong");
    await expect(page.locator(".meter")).toHaveAttribute("data-level", "4");
    await expect(page.locator("#strength-tip")).toContainText("not enforced");

    // Show / hide.
    await expect(pass).toHaveAttribute("type", "password");
    await page.getByRole("button", { name: "Show password" }).click();
    await expect(pass).toHaveAttribute("type", "text");
    await page.getByRole("button", { name: "Hide password" }).click();
    await expect(pass).toHaveAttribute("type", "password");

    // Inline, accessible validation: fields are marked invalid, the message is linked and announced, focus goes to the first problem.
    await pass.fill("short");
    await page.getByRole("button", { name: "Create administrator" }).click();
    const token = page.getByLabel("Setup token");
    await expect(token).toHaveAttribute("aria-invalid", "true");
    await expect(token).toBeFocused();
    await expect(page.locator("#setup-token-error")).toHaveAttribute("role", "alert");
    await expect(token).toHaveAttribute("aria-describedby", /setup-token-error/);
    await expect(page.locator("#username-error")).toContainText("Choose a username");
    await expect(page.locator("#password-error")).toContainText("at least 12 characters");
    await expect(page.locator("#form-error")).toContainText("Fix the highlighted fields");
    await expectAccessible(page, "setup with validation errors");

    // Errors clear as the user fixes them.
    await token.fill(coord.setupToken());
    await token.blur();
    await expect(token).not.toHaveAttribute("aria-invalid", "true");
    await page.getByLabel("Administrator username").fill("bad name!");
    await page.getByLabel("Administrator username").blur();
    await expect(page.locator("#username-error")).toContainText("letters, digits");
    await page.getByLabel("Administrator username").fill("root");
    await page.getByLabel("Administrator username").blur();
    await expect(page.locator("#username-error")).toBeEmpty();
  });

  test("a wrong setup token is reported on the token field and the form stays usable", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Setup token").fill("definitely-wrong");
    await page.getByLabel("Administrator username").fill("admin");
    await page.getByLabel("New password", { exact: true }).fill(PASSWORD);
    await page.getByLabel("Repeat password").fill(PASSWORD);
    await page.getByRole("button", { name: "Create administrator" }).click();
    await expect(page.locator("#setup-token-error")).toContainText("not the setup token");
    await expect(page.getByLabel("Setup token")).toBeFocused();
    await expect(page.getByRole("button", { name: "Create administrator" })).toBeEnabled();
  });

  test("success is shown and leads straight into the signed-in app", async ({ page, coord }) => {
    await page.goto("/");
    await page.getByLabel("Setup token").fill(coord.setupToken());
    await page.getByLabel("Administrator username").fill("operator");
    await page.getByLabel("New password", { exact: true }).fill(PASSWORD);
    await page.getByLabel("Repeat password").fill(PASSWORD);
    await page.getByRole("button", { name: "Create administrator" }).click();
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
    await expect(page.locator(".toast.applied")).toContainText("Setup complete. You are signed in as operator");
    await expect(page.locator("#account-button")).toContainText("operator");
    // The first-run state is gone for good.
    await signOutViaUi(page);
    await expect(page.getByLabel("Setup token")).toHaveCount(0);
  });
});

test.describe("sign-in", () => {
  test("wrong credentials give the same message for a wrong password and an unknown user", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    await signOutViaUi(page);
    const attempt = async (user: string, pass: string) => {
      await page.getByLabel("Username", { exact: true }).fill(user);
      await page.getByLabel("Password", { exact: true }).fill(pass);
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page.locator("#form-error")).not.toBeEmpty();
      return page.locator("#form-error").innerText();
    };
    const wrongPassword = await attempt("admin", "not the password!!");
    const unknownUser = await attempt("nobody", "not the password!!");
    expect(unknownUser).toBe(wrongPassword);
    expect(wrongPassword).toContain("username or password is not correct");
    expect(wrongPassword).not.toMatch(/no such user|unknown user|does not exist/i);
  });

  test("shows a caps lock warning while the password field has focus", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    await signOutViaUi(page);
    const pass = page.getByLabel("Password", { exact: true });
    await pass.focus();
    await expect(page.locator("#password-caps")).toBeHidden();
    await pass.evaluate((el) => el.dispatchEvent(new KeyboardEvent("keydown", { key: "A", modifierCapsLock: true, bubbles: true })));
    await expect(page.locator("#password-caps")).toBeVisible();
    await expect(page.locator("#password-caps")).toContainText("Caps Lock is on");
    await pass.evaluate((el) => el.dispatchEvent(new KeyboardEvent("keyup", { key: "CapsLock", modifierCapsLock: false, bubbles: true })));
    await expect(page.locator("#password-caps")).toBeHidden();
  });

  test("rate limiting says when to retry and treats every user name alike (no enumeration)", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    await signOutViaUi(page);
    const users = ["admin", "nobody", "admin", "root", "admin"];
    for (const [i, user] of users.entries()) {
      await page.getByLabel("Username", { exact: true }).fill(user);
      await page.getByLabel("Password", { exact: true }).fill(`wrong password ${i}`);
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page.locator("#form-error")).toContainText("not correct");
    }
    // The sixth attempt is refused, whatever the user name.
    await page.getByLabel("Username", { exact: true }).fill("someone-else");
    await page.getByLabel("Password", { exact: true }).fill("another wrong one");
    await page.getByRole("button", { name: "Sign in" }).click();
    const box = page.locator("#form-error");
    await expect(box).toContainText("Too many failed sign-in attempts");
    await expect(box).toContainText(/try again in (\d+ min )?\d+ s \(at \d\d:\d\d:\d\d\)/);
    await expect(page.getByRole("button", { name: /Try again in/ })).toBeDisabled();
    expect(await box.innerText()).not.toMatch(/someone-else|admin|exists/i);
    await expectAccessible(page, "login rate limited");
  });

  test("a session ended while the page is open shows a session-expired notice; sign-out shows a signed-out notice", async ({ page, coord, context }) => {
    await setupViaUi(page, coord);
    await navTo(page, "Missions");
    await expect(page.getByRole("heading", { name: "No missions yet" })).toBeVisible();
    await context.clearCookies();
    await page.getByLabel("Search missions").fill("x");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(page.locator("#login-notice")).toHaveAttribute("data-kind", "expired");
    await expect(page.locator("#login-notice")).toContainText("session expired");
    await loginViaUi(page);
    await signOutViaUi(page);
    await expect(page.locator("#login-notice")).toHaveAttribute("data-kind", "signed-out");
    await expectAccessible(page, "login with notice");
  });

  test("after signing in a deep link is opened", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    await signOutViaUi(page);
    await page.goto("/diagnostics");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await page.getByLabel("Username", { exact: true }).fill("admin");
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/diagnostics$/);
    await expect(page.getByRole("heading", { name: "Diagnostics" })).toBeVisible();
  });

  test("remember this device selects the session lifetime that the server applies", async ({ page, coord, context }) => {
    await setupViaUi(page, coord);
    await signOutViaUi(page);
    // The explanation quotes the server's policy.
    await expect(page.locator("#remember-hint")).toContainText("2 h without activity or 12 h at most");
    await expect(page.locator("#remember-hint")).toContainText("7 days");
    const cookie = async () => (await context.cookies()).find((c) => c.name === "__Host-bridge_session");
    // Not remembered: a browser-session cookie, and the account page says so.
    await page.getByLabel("Username", { exact: true }).fill("admin");
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await expect(page.getByLabel("Remember this device")).not.toBeChecked();
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
    expect((await cookie())?.expires).toBe(-1);
    await navTo(page, "Account");
    await expect(page.locator("#acct-session").locator("..")).toContainText("Not remembered");
    await expect(page.locator("#acct-session").locator("..")).toContainText("2 h without activity");
    // Remembered: a persistent cookie of about seven days.
    await signOutViaUi(page);
    await page.getByLabel("Username", { exact: true }).fill("admin");
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByLabel("Remember this device").check();
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
    const days = ((await cookie())?.expires ?? 0) - Date.now() / 1000;
    expect(days).toBeGreaterThan(6.9 * 86400);
    expect(days).toBeLessThan(7.1 * 86400);
    await navTo(page, "Account");
    await expect(page.locator("#acct-session").locator("..")).toContainText("Remembered");
    await expect(page.locator("#acct-session").locator("..")).toContainText("8 h without activity");
  });
});

test.describe("account menu and password change", () => {
  test("the account menu shows who is signed in and offers password change and sign out", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    const button = page.locator("#account-button");
    await expect(button).toHaveAttribute("aria-expanded", "false");
    await button.click();
    await expect(button).toHaveAttribute("aria-expanded", "true");
    const panel = page.locator("#account-panel");
    await expect(panel).toContainText("admin");
    await expect(panel).toContainText("This browser session");
    await expectAccessible(page, "account menu open");
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
    await expect(button).toBeFocused();
    await button.click();
    await panel.getByRole("button", { name: "Change password" }).click();
    await expect(page).toHaveURL(/\/account$/);
    await expect(page.getByRole("heading", { name: "Account" })).toBeVisible();
  });

  test("password change checks the current password and the new password rules inline, then signs out", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    await navTo(page, "Account");
    await page.getByLabel("Current password").fill("not my password!");
    await page.getByLabel("New password", { exact: true }).fill("another long passphrase");
    await page.getByLabel("Repeat new password").fill("another long passphrase");
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.locator("#current-password-error")).toContainText("not correct");
    await page.getByLabel("Current password").fill(PASSWORD);
    await page.getByLabel("Repeat new password").fill("different");
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.locator("#new-password-confirm-error")).toContainText("do not match");
    await page.getByLabel("Repeat new password").fill("another long passphrase");
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(page.locator("#login-notice")).toContainText("Every session was signed out");
    await loginViaUi(page, "admin", "another long passphrase");
  });
});
