import { expect, test } from "@playwright/test";
import { passkeyProviderName } from "../src/passkey-provider";

test.describe("offline passkey provider labels", () => {
  test("recognizes a bounded set of known names case-insensitively", () => {
    expect(passkeyProviderName("ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4")).toBe("Google Password Manager");
    expect(passkeyProviderName("BADA5566-A7AA-401F-BD96-45619A55120D")).toBe("1Password");
  });

  test("does not identify zero, missing, malformed, or unlisted AAGUIDs as a provider", () => {
    expect(passkeyProviderName("00000000-0000-0000-0000-000000000000")).toBe("Provider not identified");
    expect(passkeyProviderName(null)).toBe("Provider not reported");
    expect(passkeyProviderName("")).toBe("Provider not reported");
    expect(passkeyProviderName("not-an-aaguid")).toBe("Unknown provider");
    expect(passkeyProviderName("00112233-4455-6677-8899-aabbccddeeff")).toBe("Unknown provider");
  });
});
