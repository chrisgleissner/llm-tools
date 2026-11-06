// Bundles src/ into dist/ with esbuild: hashed app-HASH.js / app-HASH.css and an index.html that references them.
import { build } from "esbuild";
import { readFileSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { basename } from "node:path";
import { spawnSync } from "node:child_process";

const phraseGeneration = spawnSync("python3", ["generate_event_phrases.py"], { encoding: "utf8" });
if (phraseGeneration.status !== 0) throw new Error(`cannot generate TUI event phrases: ${phraseGeneration.stderr || phraseGeneration.error}`);

rmSync("dist", { recursive: true, force: true });
mkdirSync("dist", { recursive: true });
const result = await build({
  entryPoints: { app: "src/main.ts" },
  bundle: true,
  minify: true,
  sourcemap: false,
  format: "esm",
  splitting: true, // the terminal (xterm.js) is a separate chunk, loaded only on a mission page
  chunkNames: "chunk-[hash]",
  target: ["es2022", "chrome110", "firefox110", "safari16"],
  outdir: "dist",
  entryNames: "[name]-[hash]",
  metafile: true,
  legalComments: "none",
  logLevel: "warning",
});
const css = await build({
  entryPoints: { app: "src/app.css" },
  bundle: true,
  minify: true,
  outdir: "dist",
  entryNames: "[name]-[hash]",
  metafile: true,
  logLevel: "warning",
});
const outputs = [...Object.keys(result.metafile.outputs), ...Object.keys(css.metafile.outputs)].map((p) => basename(p));
const js = outputs.find((f) => f.startsWith("app-") && f.endsWith(".js"));
const style = outputs.find((f) => f.endsWith(".css"));
if (!js || !style) throw new Error("esbuild produced no js/css output");
const html = readFileSync("src/index.html", "utf8").replace("{{JS}}", js).replace("{{CSS}}", style);
writeFileSync("dist/index.html", html);
console.log(`dist/index.html -> ${js}, ${style}`);
