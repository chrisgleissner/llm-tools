// A small CSS walker for the lint tests (T02, T03): rules with their selector, declaration text and enclosing at-rules.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

export const WEB = resolve(import.meta.dirname, "../..");
export const SRC = join(WEB, "src");

export function filesUnder(dir: string, ext: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...filesUnder(path, ext));
    else if (name.endsWith(ext)) out.push(path);
  }
  return out.sort();
}
export const rel = (path: string): string => relative(WEB, path);

export interface Rule {
  file: string;
  selector: string;
  body: string;
  /** Preludes of the enclosing blocks, outermost first ("@media (hover: hover)"). */
  ancestors: string[];
}

const stripComments = (css: string): string => css.replace(/\/\*[\s\S]*?\*\//g, "");

/** Every rule with a declaration body, at any nesting depth. At-rule blocks are walked into, not reported. */
export function rules(file: string, css = readFileSync(file, "utf8")): Rule[] {
  const text = stripComments(css);
  const out: Rule[] = [];
  const stack: Array<{ prelude: string; start: number; hasChild: boolean }> = [];
  let last = 0;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "{") {
      const prelude = text.slice(last, i).trim();
      if (stack.length > 0) stack[stack.length - 1]!.hasChild = true;
      stack.push({ prelude, start: i + 1, hasChild: false });
      last = i + 1;
    } else if (ch === "}") {
      const top = stack.pop();
      if (top && !top.hasChild && !top.prelude.startsWith("@")) {
        out.push({ file, selector: top.prelude, body: text.slice(top.start, i), ancestors: stack.map((s) => s.prelude) });
      } else if (top && top.prelude.startsWith("@font-face")) {
        // no declarations of interest
      }
      last = i + 1;
    } else if (ch === ";" && stack.length === 0) {
      last = i + 1;
    }
  }
  return out;
}

export function declarations(body: string): Array<[string, string]> {
  return body.split(";").map((d) => d.trim()).filter(Boolean).map((d) => {
    const i = d.indexOf(":");
    return [d.slice(0, i).trim().toLowerCase(), d.slice(i + 1).trim()] as [string, string];
  }).filter(([p]) => p.length > 0);
}
