// Password reset from an SSH session (PRD-036): the sign-in page hint, the notice after a reset on the host and the
// forced password change after a temporary password. The coordinator is real; the reset runs as the host command.
import { expect, expectAccessible, loginViaUi, navTo, PASSWORD, setupViaUi, signOutViaUi, test, type Coordinator } from "./fixtures";
import type { Page } from "@playwright/test";

const NEW_PASSWORD = "a different long passphrase";
const NOTICE = "Your password was reset by an administrator on the host.";

/** Set up the administrator, then sign out so the sign-in page is showing. */
async function signedOutAdmin(page: Page, coord: Coordinator): Promise<void> {
  await setupViaUi(page, coord);
  await signOutViaUi(page);
}

test.describe("forgotten password", () => {
  test("the sign-in page says how to reset the password from an SSH session", async ({ page, coord }) => {
    await signedOutAdmin(page, coord);
    const hint = page.locator("#forgot-password-hint");
    await expect(hint).toBeVisible();
    await expect(hint).toContainText("llm-bridge-coordinator reset-password");
    await expect(hint).toContainText("Over SSH");
    const link = hint.getByRole("link", { name: "Locked out or forgot the password" });
    await expect(link).toHaveAttribute("href", /docs\/distributed\/install\.md#locked-out-or-forgot-the-password$/);
    await expect(link).toHaveAttribute("rel", /noopener/);
    await expectAccessible(page, "sign-in page with the reset hint");
  });

  test("a password chosen on the host signs in normally and shows the notice once", async ({ page, coord }) => {
    await signedOutAdmin(page, coord);
    coord.resetPassword([], `${NEW_PASSWORD}\n`);
    await loginViaUi(page, "admin", NEW_PASSWORD);
    const notice = page.locator("#reset-notice");
    await expect(notice).toContainText(NOTICE);
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    // Dismissing hides it, and moving to another page and back does not show it again.
    await page.locator("#reset-notice-dismiss").click();
    await expect(notice).toHaveCount(0);
    await page.goto("/workers");
    await expect(page.getByRole("heading", { name: "Workers", exact: true })).toBeVisible();
    await expect(page.locator("#reset-notice")).toHaveCount(0);
    // The old password no longer works.
    await signOutViaUi(page);
    await page.getByLabel("Username", { exact: true }).fill("admin");
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator("#form-error")).toContainText("Username or password is incorrect");
  });

  test("a temporary password leads only to the password change, and the change clears the notice", async ({ page, coord }) => {
    await signedOutAdmin(page, coord);
    const out = coord.resetPassword(["--temporary"]);
    const temporary = /temporary password[^:\n]*: (\S+)/.exec(out)?.[1];
    expect(temporary, out).toBeTruthy();

    await page.goto("/login");
    await page.getByLabel("Username", { exact: true }).fill("admin");
    await page.getByLabel("Password", { exact: true }).fill(temporary ?? "");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
    await expect(page.locator("#reset-notice")).toContainText(NOTICE);
    await expect(page.locator("#forced-change-text")).toContainText("choose a new one");
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
    await expectAccessible(page, "forced password change");

    // Other pages are blocked, not just hidden.
    await page.goto("/workers");
    await expect(page.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Workers", exact: true })).toHaveCount(0);

    await page.getByLabel("Current password", { exact: true }).fill(temporary ?? "");
    await page.getByLabel("New password", { exact: true }).fill(NEW_PASSWORD);
    await page.getByLabel("Repeat new password", { exact: true }).fill(NEW_PASSWORD);
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(page.locator("#login-notice")).toContainText("Password changed");

    await loginViaUi(page, "admin", NEW_PASSWORD);
    await expect(page.locator("#reset-notice")).toHaveCount(0);
  });

  test("a forced change can be abandoned by signing out", async ({ page, coord }) => {
    await signedOutAdmin(page, coord);
    const temporary = /temporary password[^:\n]*: (\S+)/.exec(coord.resetPassword(["--temporary"]))?.[1] ?? "";
    await page.goto("/login");
    await page.getByLabel("Username", { exact: true }).fill("admin");
    await page.getByLabel("Password", { exact: true }).fill(temporary);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.locator("#forced-signout").click();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  });

  test("a 403 password-change-required from any call opens the password change", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    // The coordinator answers this way for a signed-in session while a temporary password is in place. Another
    // device can be in that state when this page is already open, so the page must follow the code, not just the sign-in.
    await page.route("**/api/v1/workers**", (route) =>
      route.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ error: { code: "password-change-required", message: "change the password first" } }) }),
    );
    await page.route("**/api/v1/auth/state", async (route) => {
      const res = await route.fetch();
      const body = (await res.json()) as Record<string, unknown>;
      if (body["authenticated"]) body["password_change_required"] = true;
      await route.fulfill({ response: res, json: body });
    });
    await navTo(page, "Workers");
    await expect(page.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
  });
});
