import type * as api from "./api";

export type PasskeyCapability = { supported: boolean; conditional: boolean; platformAuthenticator: boolean };

export function supportsPasskeysOnHost(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  return !host.includes(":") && !/^\d+(?:\.\d+){3}$/.test(host);
}

export async function detectPasskeySupport(): Promise<PasskeyCapability> {
  const pk = window.PublicKeyCredential as (typeof PublicKeyCredential & {
    isConditionalMediationAvailable?: () => Promise<boolean>;
    isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean>;
    getClientCapabilities?: () => Promise<{ conditionalGet?: boolean }>;
  }) | undefined;
  const supported = !!pk && window.isSecureContext && typeof navigator.credentials?.get === "function";
  // Nothing is probed when passkeys are not supported.
  const probe = (name: "isConditionalMediationAvailable" | "isUserVerifyingPlatformAuthenticatorAvailable" | "getClientCapabilities") => supported ? Promise.resolve(pk?.[name]?.()).catch(() => undefined) : undefined;
  const [platformAuthenticator, mediation, caps] = await Promise.all([probe("isUserVerifyingPlatformAuthenticatorAvailable"), probe("isConditionalMediationAvailable"), probe("getClientCapabilities")]);
  return { supported, conditional: !!mediation || (caps as { conditionalGet?: boolean } | undefined)?.conditionalGet === true, platformAuthenticator: !!platformAuthenticator };
}

const decodeBase64Url = (value: string): ArrayBuffer => Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0)).buffer;

function encodeBase64Url(value: ArrayBuffer | ArrayBufferView): string {
  const bytes = ArrayBuffer.isView(value) ? new Uint8Array(value.buffer, value.byteOffset, value.byteLength) : new Uint8Array(value);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export function requestOptions(options: api.PasskeyAssertionStart["options"]): PublicKeyCredentialRequestOptions {
  const { challenge, rpId, timeout, userVerification, allowCredentials, extensions } = options.publicKey as {
    challenge: string;
    rpId?: string;
    timeout?: number;
    userVerification?: UserVerificationRequirement;
    allowCredentials?: Array<Omit<PublicKeyCredentialDescriptor, "id"> & { id: string }>;
    extensions?: AuthenticationExtensionsClientInputs;
  };
  return {
    challenge: decodeBase64Url(challenge),
    rpId,
    timeout,
    userVerification,
    allowCredentials: allowCredentials?.map((item) => ({ ...item, id: decodeBase64Url(item.id) })),
    extensions,
  };
}

export function serializeAssertion(credential: Credential): Record<string, unknown> {
  const value = credential as PublicKeyCredential;
  const response = value.response as AuthenticatorAssertionResponse;
  if (!value.rawId || !response?.clientDataJSON || !response.authenticatorData || !response.signature) {
    throw new Error("The browser returned an incomplete passkey assertion.");
  }
  return {
    id: value.id,
    rawId: encodeBase64Url(value.rawId),
    type: value.type,
    response: {
      clientDataJSON: encodeBase64Url(response.clientDataJSON),
      authenticatorData: encodeBase64Url(response.authenticatorData),
      signature: encodeBase64Url(response.signature),
      userHandle: response.userHandle ? encodeBase64Url(response.userHandle) : null,
    },
    clientExtensionResults: value.getClientExtensionResults(),
  };
}
