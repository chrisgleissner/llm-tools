// Password policy checks (the rules the server enforces) and an advisory strength estimate.
import type { PasswordPolicy } from "./api";

export const DEFAULT_POLICY: PasswordPolicy = { min_length: 12, max_length: 256 };

/** The server counts bytes, not characters. */
export const byteLength = (s: string): number => new TextEncoder().encode(s).length;

export interface Rule {
  id: string;
  text: string;
  met: boolean;
}

/** The rules the server enforces (`valid_password`). */
export function requiredRules(pw: string, policy: PasswordPolicy): Rule[] {
  const n = byteLength(pw);
  return [
    { id: "min", text: `At least ${policy.min_length} characters`, met: n >= policy.min_length },
    { id: "max", text: `At most ${policy.max_length} bytes`, met: n <= policy.max_length },
  ];
}

export interface Strength {
  level: 0 | 1 | 2 | 3 | 4;
  label: string;
  tip: string;
}

const COMMON = ["password", "passw0rd", "letmein", "qwerty", "123456", "abcdef", "admin", "welcome", "iloveyou", "changeme"];

/** An estimate from length and variety. It is advice only: the server enforces only the length rules. */
export function strength(pw: string, username: string, policy: PasswordPolicy): Strength {
  const n = byteLength(pw);
  if (n === 0) return { level: 0, label: "Not entered", tip: "Four or more random words make a strong, memorable passphrase." };
  if (n < policy.min_length) return { level: 0, label: "Too short", tip: `${policy.min_length - n} more character${policy.min_length - n === 1 ? "" : "s"} needed.` };
  const lower = pw.toLowerCase();
  let pool = 0;
  if (/[a-z]/.test(pw)) pool += 26;
  if (/[A-Z]/.test(pw)) pool += 26;
  if (/[0-9]/.test(pw)) pool += 10;
  if (/[^A-Za-z0-9]/.test(pw)) pool += 33;
  let bits = pw.length * Math.log2(Math.max(pool, 2));
  const unique = new Set(pw).size;
  if (unique <= 3) bits = Math.min(bits, 20);
  else if (unique < pw.length / 2) bits *= 0.6;
  if (COMMON.some((c) => lower.includes(c))) bits = Math.min(bits, 30);
  if (username.length >= 3 && lower.includes(username.toLowerCase())) bits = Math.min(bits, 30);
  if (/(0123|1234|2345|3456|4567|5678|6789|abcd|bcde|cdef|qwer|asdf)/.test(lower)) bits *= 0.7;
  if (/^\d+$/.test(pw)) bits = Math.min(bits, 35);
  const level = bits < 40 ? 1 : bits < 60 ? 2 : bits < 80 ? 3 : 4;
  const label = ["", "Weak", "Fair", "Good", "Strong"][level] ?? "Weak";
  const tip = level >= 3 ? "Good. Do not reuse it for anything else." : level === 2 ? "Add another word or a few more characters." : "Avoid common words, keyboard runs and your username; add more words.";
  return { level: level as Strength["level"], label, tip };
}
