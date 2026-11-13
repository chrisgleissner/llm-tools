// T01 to T04 (UXR-001, UXR-022, UXR-027, UXR-030, UXR-042): design tokens are the only source of styles.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, uxr, uxrStatic } from "./gate";
import { declarations, filesUnder, rel, rules, SRC } from "./css";
import { openApp } from "./harness";

function toRgb(value: string): [number, number, number] {
  const v = value.trim().toLowerCase();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(v);
  if (hex) {
    const h = hex[1]!.length === 3 ? [...hex[1]!].map((c) => c + c).join("") : hex[1]!;
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  const rgb = /^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/.exec(v);
  if (rgb) return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
  throw new Error(`token value is not a plain colour: "${value}"`);
}
const luminance = ([r, g, b]: [number, number, number]): number => {
  const f = (c: number) => { const s = c / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
};
const ratio = (a: string, b: string): number => {
  const [x, y] = [luminance(toRgb(a)), luminance(toRgb(b))];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};

/** Pairs of spec section 4.3 with the ratio each must reach. */
const PAIRS: Array<[string, string, number]> = [
  ...["--surface-0", "--surface-1", "--surface-2", "--surface-3"].flatMap((s): Array<[string, string, number]> => [["--text", s, 4.5], ["--text-muted", s, 4.5]]),
  ...["--surface-0", "--surface-1", "--surface-2"].map((s): [string, string, number] => ["--border-strong", s, 3]),
  ...["--surface-0", "--surface-1", "--surface-3", "--accent-tint"].map((s): [string, string, number] => ["--accent", s, 4.5]),
  ["--on-accent", "--accent", 4.5],
  ["--focus", "--surface-0", 3], ["--focus", "--surface-1", 3], ["--focus", "--term-bg", 3],
  ["--alert-attention-fg", "--alert-attention-bg", 4.5], ["--alert-attention-bar", "--surface-1", 3],
  ["--alert-failed-fg", "--alert-failed-bg", 4.5], ["--alert-failed-bar", "--surface-1", 3],
  ...["--icon-running", "--icon-done", "--icon-offline", "--icon-idle"].map((i): [string, string, number] => [i, "--surface-2", 3]),
  ["--danger", "--surface-1", 4.5],
  ["--on-danger", "--danger", 4.5],
];

uxr({ t: "T01", uxr: ["UXR-001", "UXR-030"], needs: ["WP-0"] }, "every token pair of section 4.3 meets its contrast ratio in light and dark", async ({ page }) => {
  await openApp(page, { auth: "login" }, "/login");
  for (const theme of ["light", "dark"] as const) {
    const values = await page.evaluate((t) => {
      document.documentElement.setAttribute("data-theme", t);
      const style = getComputedStyle(document.documentElement);
      const names = new Set<string>();
      for (const sheet of document.styleSheets) for (const rule of sheet.cssRules) if (rule instanceof CSSStyleRule) for (const p of rule.style) if (p.startsWith("--")) names.add(p);
      return Object.fromEntries([...names].map((n) => [n, style.getPropertyValue(n).trim()]));
    }, theme);
    const failures: string[] = [];
    for (const [fg, bg, need] of PAIRS) {
      if (!values[fg] || !values[bg]) { failures.push(`${theme}: token ${!values[fg] ? fg : bg} is not defined`); continue; }
      const r = ratio(values[fg]!, values[bg]!);
      if (r < need) failures.push(`${theme}: ${fg} on ${bg} is ${r.toFixed(2)}:1, needs ${need}:1`);
    }
    expect(failures, `contrast failures in the ${theme} theme`).toEqual([]);
  }
});

uxrStatic({ t: "T02", uxr: ["UXR-001"], needs: ["WP-0"] }, "no colour literals outside tokens.css (CSS) and terminal-theme.ts (TypeScript)", async () => {
  const offenders: string[] = [];
  const cssLiteral = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(|\bcolor-mix\(in srgb,\s*#/;
  for (const file of filesUnder(SRC, ".css")) {
    if (file.endsWith(join("css", "tokens.css"))) continue;
    for (const rule of rules(file)) for (const [prop, value] of declarations(rule.body)) if (cssLiteral.test(value)) offenders.push(`${rel(file)}: ${rule.selector} { ${prop}: ${value} }`);
  }
  const tsLiteral = /["'`]#[0-9a-fA-F]{3,8}["'`]|#[0-9a-fA-F]{6}\b|\brgba?\(|\bhsla?\(/;
  for (const file of filesUnder(SRC, ".ts")) {
    if (file.endsWith("terminal-theme.ts") || file.endsWith(".generated.ts")) continue;
    readFileSync(file, "utf8").split("\n").forEach((line, i) => { if (tsLiteral.test(line)) offenders.push(`${rel(file)}:${i + 1}: ${line.trim()}`); });
  }
  expect(offenders, "colour literals (use a token from tokens.css)").toEqual([]);
});

uxrStatic({ t: "T03", uxr: ["UXR-022", "UXR-027"], needs: ["WP-0"] }, "motion and hover lint: no transition all, hover only under (hover: hover), reduced motion removes animation", async () => {
  const ALLOWED = new Set(["transform", "opacity", "background-color", "border-color", "color", "none"]);
  const problems: string[] = [];
  let reducedMotionAnimation = false;
  for (const file of filesUnder(SRC, ".css")) {
    for (const rule of rules(file)) {
      const where = `${rel(file)}: ${rule.selector}`;
      if (/:hover/.test(rule.selector) && !rule.ancestors.some((a) => /^@media[^{]*\(\s*hover\s*:\s*hover\s*\)/.test(a))) problems.push(`${where} uses :hover outside @media (hover: hover)`);
      const inReduced = rule.ancestors.some((a) => /prefers-reduced-motion\s*:\s*reduce/.test(a));
      for (const [prop, value] of declarations(rule.body)) {
        if (prop === "transition" || prop === "transition-property") {
          if (/\ball\b/.test(value)) problems.push(`${where} { ${prop}: ${value} } transitions all`);
          const names = (prop === "transition" ? value.split(",").map((part) => part.trim().split(/\s+/)[0] ?? "") : value.split(",").map((part) => part.trim())).filter((n) => n && !/^(var\(|[0-9.]+m?s$)/.test(n));
          for (const n of names) if (!ALLOWED.has(n)) problems.push(`${where} { ${prop}: ${value} } transitions "${n}", which is not allowed`);
        }
        if (inReduced && /^animation(-name)?$/.test(prop) && /\bnone\b/.test(value)) reducedMotionAnimation = true;
        if (inReduced && prop === "animation-duration") reducedMotionAnimation = true;
      }
    }
  }
  expect(reducedMotionAnimation, "a prefers-reduced-motion: reduce block removes animation").toBe(true);
  expect(problems, "motion and hover problems").toEqual([]);
});

uxrStatic({ t: "T04", uxr: ["UXR-042"], needs: ["WP-0"] }, "viewport meta carries viewport-fit=cover and interactive-widget=resizes-content and does not block zoom", async () => {
  const html = readFileSync(join(SRC, "index.html"), "utf8");
  const meta = /<meta[^>]+name=["']viewport["'][^>]*>/i.exec(html)?.[0] ?? "";
  const content = /content=["']([^"']*)["']/i.exec(meta)?.[1] ?? "";
  const parts = Object.fromEntries(content.split(",").map((p) => p.trim().split("=").map((x) => x.trim())).map(([k, v]) => [k, v ?? ""]));
  expect(parts["width"], "width").toBe("device-width");
  expect(parts["initial-scale"], "initial-scale").toBe("1");
  expect(parts["viewport-fit"], "viewport-fit").toBe("cover");
  expect(parts["interactive-widget"], "interactive-widget").toBe("resizes-content");
  expect(content, "user-scalable").not.toMatch(/user-scalable/i);
  expect(content, "maximum-scale").not.toMatch(/maximum-scale/i);
});
