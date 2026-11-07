import { expect, test } from "./fixtures";
import { mockApp } from "./mock";
import { supportsPasskeysOnHost } from "../src/passkeys";

test("passkey RP support requires a DNS host, not an IP literal", () => {
  expect(supportsPasskeysOnHost("bridge.example.test")).toBe(true);
  expect(supportsPasskeysOnHost("localhost")).toBe(true);
  expect(supportsPasskeysOnHost("192.0.2.7")).toBe(false);
  expect(supportsPasskeysOnHost("[2001:db8::7]")).toBe(false);
});

async function installPasskeySupport(page: import("@playwright/test").Page, conditional = false): Promise<void> {
  await page.addInitScript((useConditional) => {
    const constructor = window.PublicKeyCredential as typeof PublicKeyCredential & {
      isConditionalMediationAvailable?: () => Promise<boolean>;
      isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean>;
      getClientCapabilities?: () => Promise<{ conditionalGet?: boolean }>;
    };
    Object.defineProperty(constructor, "isConditionalMediationAvailable", { configurable: true, value: async () => useConditional });
    Object.defineProperty(constructor, "isUserVerifyingPlatformAuthenticatorAvailable", { configurable: true, value: async () => true });
    Object.defineProperty(constructor, "getClientCapabilities", { configurable: true, value: async () => ({ conditionalGet: useConditional }) });
    Object.defineProperty(navigator.credentials, "get", {
      configurable: true,
      value: async (options: CredentialRequestOptions) => {
        Object.defineProperty(window, "__passkeyRequest", { configurable: true, value: options });
        if (useConditional && options.mediation === "conditional") {
          return new Promise<null>((_resolve, reject) => {
            options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
          });
        }
        const bytes = (values: number[]) => new Uint8Array(values).buffer;
        return {
          id: "AQID",
          rawId: bytes([1, 2, 3]),
          type: "public-key",
          response: {
            clientDataJSON: bytes([4, 5]),
            authenticatorData: bytes([6]),
            signature: bytes([7, 8]),
            userHandle: null,
          },
          getClientExtensionResults: () => ({}),
        } as unknown as Credential;
      },
    });
  }, conditional);
}

test("passkey button signs in without a username and keeps password fallback", async ({ page }) => {
  await installPasskeySupport(page);
  const mock = await mockApp(page, { auth: "login", passkeySignInAvailable: true });
  await page.goto("/login");

  await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeVisible();
  await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Sign in with a passkey" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  expect(mock.passkeyAssertionStartCount()).toBe(1);
  const finish = mock.passkeyAssertionFinish();
  expect(finish?.challenge_id).toBe("mock-challenge-1");
  expect(finish?.credential).toMatchObject({
    id: "AQID",
    rawId: "AQID",
    type: "public-key",
    response: {
      clientDataJSON: "BAU",
      authenticatorData: "Bg",
      signature: "Bwg",
      userHandle: null,
    },
  });
});

test("conditional mediation uses username WebAuthn autocomplete without disabling password sign-in", async ({ page }) => {
  await installPasskeySupport(page, true);
  await mockApp(page, { auth: "login", passkeySignInAvailable: true, passkeyDiscoverable: true });
  await page.goto("/login");

  const username = page.getByLabel("Username", { exact: true });
  await expect(username).toHaveAttribute("autocomplete", "username webauthn");
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeEnabled();
  await expect.poll(() => page.evaluate(() => (window as Window & { __passkeyRequest?: CredentialRequestOptions }).__passkeyRequest?.mediation)).toBe("conditional");
});

test("no passkey control appears when browser WebAuthn support is unavailable", async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(window, "PublicKeyCredential", { configurable: true, value: undefined }));
  await mockApp(page, { auth: "login", passkeySignInAvailable: true });
  await page.goto("/login");

  await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeHidden();
  await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
});
