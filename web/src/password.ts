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
  const missing = policy.min_length - n;
  if (n === 0) return { level: 0, label: "Not entered", tip: "Four or more random words make a strong, memorable passphrase." };
  if (missing > 0) return { level: 0, label: "Too short", tip: `${missing} more character${missing === 1 ? "" : "s"} needed.` };
  const lower = pw.toLowerCase();
  const pool = (/[a-z]/.test(pw) ? 26 : 0) + (/[A-Z]/.test(pw) ? 26 : 0) + (/\d/.test(pw) ? 10 : 0) + (/[^A-Za-z0-9]/.test(pw) ? 33 : 0);
  let bits = pw.length * Math.log2(Math.max(pool, 2));
  const unique = new Set(pw).size;
  const cap = (max: number): void => {
    bits = Math.min(bits, max);
  };
  if (unique <= 3) cap(20);
  else if (unique < pw.length / 2) bits *= 0.6;
  if (COMMON.some((c) => lower.includes(c))) cap(30);
  if (username.length >= 3 && lower.includes(username.toLowerCase())) cap(30);
  if (/(0123|1234|2345|3456|4567|5678|6789|abcd|bcde|cdef|qwer|asdf)/.test(lower)) bits *= 0.7;
  if (/^\d+$/.test(pw)) cap(35);
  const level = (1 + Number(bits >= 40) + Number(bits >= 60) + Number(bits >= 80)) as Strength["level"];
  const tip = level >= 3 ? "Good. Do not reuse it for anything else." : level === 2 ? "Add another word or a few more characters." : "Avoid common words, keyboard runs and your username; add more words.";
  return { level, label: ["Weak", "Fair", "Good", "Strong"][level - 1] as string, tip };
}
