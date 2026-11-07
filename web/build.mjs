// Bundles src/ into dist/ with esbuild: hashed app-HASH.js / app-HASH.css and an index.html that references them.
import { build } from "esbuild";
import { readFileSync, writeFileSync, rmSync, mkdirSync, existsSync } from "node:fs";
import { basename } from "node:path";
import { spawnSync } from "node:child_process";

// Windows' Python lacks curses, and the container image build stages only the checked-in web
// tree. The artifact is regenerated in source checkouts and drift-checked by the Python tests.
if (process.platform === "win32" || !existsSync("../llm_tools/bridge_tui.py")) {
  if (!existsSync("src/event-phrases.generated.ts")) throw new Error("missing generated TUI event phrases");
} else {
  const phraseGeneration = spawnSync("python3", ["generate_event_phrases.py"], { encoding: "utf8" });
  if (phraseGeneration.status !== 0) throw new Error(`cannot generate TUI event phrases: ${phraseGeneration.stderr || phraseGeneration.error}`);
}

rmSync("dist", { recursive: true, force: true });
mkdirSync("dist", { recursive: true });
// The xterm.js stylesheet is only needed on a mission page: it is its own hashed file that the terminal chunk loads, so it stays out of the first load.
const xtermCss = await build({
  entryPoints: { xterm: "node_modules/@xterm/xterm/css/xterm.css" },
  bundle: true,
  minify: true,
  outdir: "dist",
  entryNames: "[name]-[hash]",
  metafile: true,
  logLevel: "warning",
});
const xtermFile = Object.keys(xtermCss.metafile.outputs).map((p) => basename(p)).find((f) => f.endsWith(".css"));
if (!xtermFile) throw new Error("esbuild produced no xterm css output");
// Page stylesheets that belong to lazy routes are separate hashed files too. The route chunk's loader fetches the stylesheet in
// parallel with the chunk and waits for both before it renders (src/route-css.ts), so the first-load CSS stays small.
const routeCssEntries = { fleet: "src/css/fleet.css", auth: "src/css/auth.css", mission: "src/css/route-mission.css" };
const routeCss = await build({
  entryPoints: routeCssEntries,
  bundle: true,
  minify: true,
  outdir: "dist",
  entryNames: "[name]-[hash]",
  metafile: true,
  logLevel: "warning",
});
const routeCssFiles = Object.fromEntries(
  Object.entries(routeCss.metafile.outputs).map(([out, o]) => [Object.entries(routeCssEntries).find(([, src]) => src === o.entryPoint)?.[0] ?? "", `/${basename(out)}`]),
);
for (const name of Object.keys(routeCssEntries)) if (!routeCssFiles[name]) throw new Error(`esbuild produced no ${name} route css output`);
const result = await build({
  define: { __XTERM_CSS__: JSON.stringify(`/${xtermFile}`), __ROUTE_CSS__: JSON.stringify(routeCssFiles) },
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
