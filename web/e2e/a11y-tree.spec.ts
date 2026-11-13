// PRD-019 and PRD-032: focus management on a route change, and accessibility-tree assertions for four key flows
// (sign-in, Inbox, a mission, answering a question). The tree is what a screen reader receives, so these templates
// pin roles, names, order and states, not markup. Templates match in part: elements that are not listed may exist.
// A real screen-reader run (Orca on Linux, NVDA on Windows) is operator evidence and is not part of this file.
import { expect, test } from "./fixtures";
import { MISSIONS, mockApp } from "./mock";

const MISSION = MISSIONS[0]!.mission;

test.describe("focus on a route change", () => {
  for (const [name, path, heading] of [
    ["Workers", "/workers", "Workers"],
    ["Missions", "/missions", "Missions"],
    ["Usage", "/usage", "Provider usage"],
  ] as const) {
    test(`${name}: focus moves to the page's main heading`, async ({ page }) => {
      await mockApp(page, { auth: "in" });
      await page.goto("/");
      await expect(page.locator("#conn")).toContainText("Live");
      await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`${path}$`));
      const h1 = page.getByRole("main").getByRole("heading", { level: 1 });
      await expect(h1).toBeVisible();
      await expect(h1).toContainText(heading);
      await expect(h1, "the main heading has the focus after the route change").toBeFocused();
    });
  }
});

test("accessibility tree: sign-in", async ({ page }) => {
  await mockApp(page, { auth: "login" });
  await page.goto("/login");
  await expect(page.getByRole("main")).toMatchAriaSnapshot(`
    - main:
      - heading "Sign in" [level=1]
      - text: Username
      - textbox "Username"
      - text: Password
      - textbox "Password"
      - button "Show password"
      - checkbox "Remember this device"
      - button "Sign in"
  `);
});

test("accessibility tree: Inbox", async ({ page }) => {
  await mockApp(page, { auth: "in", providerQuestion: true });
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeFocused();
  await expect(page.getByRole("navigation", { name: "Main" })).toMatchAriaSnapshot(`
    - navigation "Main":
      - list:
        - listitem:
          - link "Inbox"
        - listitem:
          - link "Missions"
        - listitem:
          - link "Sessions"
        - listitem:
          - link "Workers"
        - listitem:
          - link "Usage"
  `);
  await expect(page.getByRole("main")).toMatchAriaSnapshot(`
    - main:
      - heading "Inbox" [level=1]
      - region "Needs you (2)":
        - heading "Needs you (2)" [level=2]
        - list "Needs you":
          - listitem:
            - link "Fix login redirect"
            - button "Retry task 2 of Fix login redirect"
            - link "Open Fix login redirect"
          - listitem:
            - link "Refactor the parser"
            - link "Answer in Refactor the parser"
  `);
});

test("accessibility tree: a mission", async ({ page }) => {
  await mockApp(page, { auth: "in", providerQuestion: true });
  await page.goto(`/missions/alpha/${MISSION.id}`);
  await expect(page.getByRole("main")).toMatchAriaSnapshot(`
    - main:
      - navigation "Breadcrumb":
        - link "Missions"
      - heading "Refactor the parser" [level=1]
      - group "Mission actions":
        - button "Take control"
        - button "Pause"
        - button "Stop"
      - tablist "Mission views":
        - tab /Tasks/
        - tab "Activity"
      - region "Session":
        - region "Live session":
          - status
          - button "Take control"
          - textbox "Message Codex" [disabled]
          - button "Send" [disabled]
      - tabpanel /Tasks/:
        - heading "Tasks" [level=2]
        - list "Open tasks"
  `);
});

test("accessibility tree: answering a question", async ({ page }) => {
  await mockApp(page, { auth: "in", providerQuestion: true, terminalScreen: true });
  await page.goto("/");
  await page.getByRole("link", { name: "Answer in Refactor the parser" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Refactor the parser" })).toBeVisible();
  await expect(page.getByRole("main")).toMatchAriaSnapshot(`
    - main:
      - paragraph: /.*Take control to answer.*/
      - region "Session":
        - region "Live session":
          - textbox "Message Codex" [disabled]
  `);
  await page.getByRole("button", { name: "Take control" }).first().click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  const field = page.getByRole("textbox", { name: "Message Codex" });
  await expect(field, "the answer field is enabled once the operator has control").toBeEnabled();
  await expect(page.getByRole("region", { name: "Live session" })).toMatchAriaSnapshot(`
    - region "Live session":
      - textbox "Message Codex"
      - button "Send"
  `);
});
