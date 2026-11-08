/**
 * Bounded, offline display labels for a small set of common passkey providers.
 *
 * The snapshot is a subset of the community UI-naming list at
 * https://github.com/passkeydeveloper/passkey-authenticator-aaguids/blob/d53c0f358deeec6ba23d6c6e1f25f32ce45c22f9/aaguid.json
 * (retrieved 2026-11-07). That upstream list is community-sourced and announced
 * retirement in January 2027. It is not an authenticity source. FIDO's MDS is
 * authoritative for certified authenticator metadata and security status:
 * https://fidoalliance.org/metadata/ . Labels here are only UI hints; unknown
 * identifiers stay unknown and are never used to make a security decision.
 */

const ZERO_AAGUID = "00000000-0000-0000-0000-000000000000";
const AAGUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// Deliberately small: known common names, no network request and no vendor
// supplied icon or markup. Additions should cite a stable source revision.
const PROVIDER_BY_AAGUID: Readonly<Record<string, string>> = Object.freeze({
  "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Password Manager",
  "531126d6-e717-415c-9320-3d9aa6981239": "Dashlane",
  "bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
  "b84e4048-15dc-4dd0-8640-f4f60813c8af": "NordPass",
  "0ea242b4-43c4-4a1b-8b17-dd6d0b6baec6": "Keeper",
  "d548826e-79b4-db40-a3d8-11116f7e8349": "Bitwarden",
  "fbfc3007-154e-4ecc-8c0b-6e020557d7bd": "Apple Passwords",
  "50726f74-6f6e-5061-7373-50726f746f6e": "Proton Pass",
  "b78a0a55-6ef8-d246-a042-ba0f6d55050c": "LastPass",
  "d3452668-01fd-4c12-926c-83a4204853aa": "Microsoft Password Manager",
});

/** Return a display-only label without exposing an unrecognized identifier. */
export function passkeyProviderName(aaguid: unknown): string {
  if (typeof aaguid !== "string" || aaguid.length === 0) return "Provider not reported";
  if (aaguid.length !== 36 || !AAGUID_PATTERN.test(aaguid.toLowerCase())) return "Unknown provider";

  const normalized = aaguid.toLowerCase();
  if (normalized === ZERO_AAGUID) return "Provider not identified";
  return PROVIDER_BY_AAGUID[normalized] ?? "Unknown provider";
}
