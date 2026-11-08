import { expect, test, PASSWORD } from "./fixtures";
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

test("another hostname keeps password sign-in and links to the canonical passkey address", async ({ page }) => {
  await installPasskeySupport(page);
  await mockApp(page, { auth: "login", passkeySignInAvailable: false, passkeyCanonicalOrigin: "https://bridge.example.test:8443" });
  await page.goto("/login");

  await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeHidden();
  await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
  await expect(page.locator("#passkey-canonical-link")).toContainText("Passkeys are available at");
  await expect(page.locator("#passkey-canonical-link a")).toHaveAttribute("href", "https://bridge.example.test:8443");
});

test("Chromium virtual authenticator completes registration and passwordless sign-in", async ({ page, coord, browserName }) => {
  test.skip(browserName !== "chromium", "Chromium DevTools Protocol provides the virtual authenticator");

  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  // Keep this test on the explicit button path. Conditional mediation is covered separately;
  // leaving it pending here can race the deliberate user click below.
  await page.addInitScript(() => {
    const pk = window.PublicKeyCredential as typeof PublicKeyCredential & {
      isConditionalMediationAvailable?: () => Promise<boolean>;
      getClientCapabilities?: () => Promise<{ conditionalGet?: boolean }>;
    };
    Object.defineProperty(pk, "isConditionalMediationAvailable", { configurable: true, value: async () => false });
    Object.defineProperty(pk, "getClientCapabilities", { configurable: true, value: async () => ({ conditionalGet: false }) });
  });

  // The test CA includes localhost as a DNS SAN. Using that name exercises the same secure,
  // DNS-origin path that passkeys require, while the coordinator still binds only to loopback.
  const localUrl = coord.url.replace("127.0.0.1", "localhost");
  await page.goto(localUrl);
  await expect(page.getByRole("heading", { name: "First-run setup" })).toBeVisible();
  await page.getByLabel("Setup token").fill(coord.setupToken());
  await page.getByLabel("Administrator username").fill("admin");
  await page.getByLabel("New password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Repeat password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();

  const registration = await page.evaluate(async (password) => {
    const state = await fetch("/api/v1/auth/state").then((response) => response.json()) as { csrf_token: string };
    const stepped = await fetch("/api/v1/auth/step-up", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf_token },
      body: JSON.stringify({ password }),
    });
    if (!stepped.ok) throw new Error(`step-up failed: ${stepped.status} ${await stepped.text()}`);
    const fresh = await stepped.json() as { csrf_token: string };
    const started = await fetch("/api/v1/auth/passkeys/registration/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": fresh.csrf_token },
      body: JSON.stringify({ name: "Virtual authenticator" }),
    });
    if (!started.ok) throw new Error(`registration start failed: ${started.status} ${await started.text()}`);
    const ceremony = await started.json() as { challenge_id: string; options: { publicKey: Record<string, unknown> } };
    const publicKey = ceremony.options.publicKey as Record<string, unknown> & {
      challenge: string;
      user: { id: string };
      excludeCredentials?: Array<{ id: string; type: "public-key"; transports?: AuthenticatorTransport[] }>;
    };
    const decode = (value: string) => Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
    const credential = await navigator.credentials.create({ publicKey: {
      ...publicKey,
      challenge: decode(publicKey.challenge),
      user: { ...publicKey.user, id: decode(publicKey.user.id) },
      excludeCredentials: publicKey.excludeCredentials?.map((item) => ({ ...item, id: decode(item.id) })),
    } as unknown as PublicKeyCredentialCreationOptions }) as PublicKeyCredential;
    const response = credential.response as AuthenticatorAttestationResponse;
    const encode = (value: ArrayBuffer | ArrayBufferView) => {
      const bytes = value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
      let binary = "";
      for (const byte of bytes) binary += String.fromCharCode(byte);
      return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
    };
    const payload = {
      challenge_id: ceremony.challenge_id,
      credential: {
        id: credential.id,
        rawId: encode(credential.rawId),
        type: credential.type,
        response: {
          clientDataJSON: encode(response.clientDataJSON),
          attestationObject: encode(response.attestationObject),
          transports: response.getTransports(),
        },
        clientExtensionResults: credential.getClientExtensionResults(),
      },
    };
    const finished = await fetch("/api/v1/auth/passkeys/registration/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": fresh.csrf_token },
      body: JSON.stringify(payload),
    });
    return { status: finished.status, body: await finished.text() };
  }, PASSWORD);
  expect(registration.status, registration.body).toBe(201);

  await page.evaluate(async () => {
    const state = await fetch("/api/v1/auth/state").then((response) => response.json()) as { csrf_token: string };
    const response = await fetch("/api/v1/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf_token },
      body: "{}",
    });
    if (!response.ok) throw new Error(`sign-out failed: ${response.status}`);
  });
  await page.goto(`${localUrl}/login`);
  await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeVisible();
  await page.getByRole("button", { name: "Sign in with a passkey" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  const categories = await page.evaluate(async () => (await fetch("/api/v1/security/notifications").then((r) => r.json()) as { notifications: Array<{ category: string }> }).notifications.map((item) => item.category));
  expect(categories).toContain("new_sign_in_device");
  await expect(page.getByText("A sign-in came from a new device.")).toBeVisible();

  await page.goto(`${localUrl}/account`);
  await page.getByLabel("Token name").fill("passkey-step-up-test");
  await page.getByRole("button", { name: "Create token" }).click();
  const tokenStepUpDialog = page.getByRole("dialog", { name: "Use a passkey to continue" });
  await expect(tokenStepUpDialog).toBeVisible();
  await expect(tokenStepUpDialog.getByLabel("Current password")).toBeHidden();
  await tokenStepUpDialog.getByRole("button", { name: "Use a passkey instead" }).click();
  await expect(tokenStepUpDialog).toHaveCount(0);
  await expect(page.getByText("Token passkey-step-up-test created.")).toBeVisible();

  await page.evaluate(async () => {
    const state = await fetch("/api/v1/auth/state").then((response) => response.json()) as { csrf_token: string };
    const response = await fetch("/api/v1/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf_token },
      body: "{}",
    });
    if (!response.ok) throw new Error(`sign-out failed: ${response.status}`);
  });
  await page.goto(`${localUrl}/login`);
  await page.getByLabel("Username", { exact: true }).fill("admin");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page.goto(`${localUrl}/account`);

  const passkeys = page.getByRole("list", { name: "Registered passkeys" });
  const rename = passkeys.getByLabel("New name for Virtual authenticator");
  await rename.fill("Primary device");
  await passkeys.getByRole("button", { name: "Rename" }).click();
  const stepUpDialog = page.getByRole("dialog", { name: "Verify it is you" });
  await expect(stepUpDialog).toBeVisible();
  await stepUpDialog.getByRole("button", { name: "Use a passkey instead" }).click();
  await expect(stepUpDialog).toHaveCount(0);
  await expect(passkeys).toContainText("Primary device");

  await page.getByLabel("New passkey name").fill("Travel device");
  await page.getByRole("button", { name: "Add passkey" }).click();
  await expect(passkeys).toContainText("Travel device");
  page.once("dialog", (dialog) => dialog.accept());
  await passkeys.getByRole("button", { name: "Remove Travel device" }).click();
  await expect(passkeys).not.toContainText("Travel device");
  await cdp.detach();
});
