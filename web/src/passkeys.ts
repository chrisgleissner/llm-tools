import type * as api from "./api";

export type PasskeyCapability = { supported: boolean; conditional: boolean; platformAuthenticator: boolean };

export async function detectPasskeySupport(): Promise<PasskeyCapability> {
  if (!window.isSecureContext || !navigator.credentials || typeof navigator.credentials.get !== "function" || !window.PublicKeyCredential) {
    return { supported: false, conditional: false, platformAuthenticator: false };
  }
  const pk = window.PublicKeyCredential as typeof PublicKeyCredential & {
    isConditionalMediationAvailable?: () => Promise<boolean>;
    isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean>;
    getClientCapabilities?: () => Promise<{ conditionalGet?: boolean }>;
  };
  const [platformAuthenticator, mediation, caps] = await Promise.all([
    pk.isUserVerifyingPlatformAuthenticatorAvailable?.().catch(() => false) ?? Promise.resolve(false),
    pk.isConditionalMediationAvailable?.().catch(() => false) ?? Promise.resolve(false),
    pk.getClientCapabilities?.().catch(() => ({})) ?? Promise.resolve({}),
  ]);
  const clientCapabilities = caps as { conditionalGet?: boolean };
  return { supported: true, conditional: mediation || clientCapabilities.conditionalGet === true, platformAuthenticator };
}

function decodeBase64Url(value: string): ArrayBuffer {
  const binary = atob(value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "="));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function encodeBase64Url(value: ArrayBuffer | ArrayBufferView): string {
  const bytes = value instanceof ArrayBuffer
    ? new Uint8Array(value)
    : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export function requestOptions(options: api.PasskeyAssertionStart["options"]): PublicKeyCredentialRequestOptions {
  const json = options.publicKey as {
    challenge: string;
    rpId?: string;
    timeout?: number;
    userVerification?: UserVerificationRequirement;
    allowCredentials?: Array<Omit<PublicKeyCredentialDescriptor, "id"> & { id: string }>;
    extensions?: AuthenticationExtensionsClientInputs;
  };
  return {
    challenge: decodeBase64Url(json.challenge),
    rpId: json.rpId,
    timeout: json.timeout,
    userVerification: json.userVerification,
    allowCredentials: json.allowCredentials?.map((item) => ({ ...item, id: decodeBase64Url(item.id) })),
    extensions: json.extensions,
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
