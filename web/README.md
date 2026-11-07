# LLM Bridge web interface

TypeScript, esbuild, xterm.js; no UI framework. `src/` holds the application, `e2e/` the Playwright tests, `budget.json`
the bundle budget.

```bash
npm ci
npm run typecheck && npm run build
# the tests start a real coordinator (coordinator/target/release) and, for worker tests, the Python worker in ../.venv
npx playwright test
```

## Projects

| Project | Profile |
|---|---|
| `chromium-desktop`, `firefox-desktop` | 1440x900 |
| `chromium-mobile` | Playwright `devices['Pixel 4']` (353x745 CSS px, DPR 3, touch) |
| `firefox-mobile` | 393x851 CSS px, DPR 2.75, touch (Firefox has no mobile emulation, so viewport, scale factor and touch only) |

Every spec runs on all four projects. `mobile.spec.ts` asserts no horizontal scrolling, touch targets of at least
44x44 CSS px, input font size of at least 16 px, dialogs inside the viewport and the terminal key bar (Esc, Tab, Ctrl,
arrows; the test captures the bytes sent on the WebSocket) in portrait and landscape.

## Click paths, the on-screen keyboard and thumb reach (UXR-043 to UXR-046)

`e2e/redesign/click-paths-data.ts` lists every operator task with its interaction count at 1440x900 and at phone widths; `docs/distributed/ux-audit.md`
holds the table generated from it. After changing a row run `node scripts/click-paths-doc.mjs` (with `--check` it only verifies). The specs
`uxr-paths` (interaction counts, equal to the table), `uxr-keyboard` (typing above a keyboard that covers half the screen), `uxr-reach` (primary
control in the lower 60% of the screen on phones) and `uxr-keyboard-only` (every task with the keyboard alone on a desktop) run on the four standard
projects and, with `uxr-keyboard` and `uxr-reach`, also on `paths-320x568` and `paths-360x640`.

## Visual baselines

`e2e/screenshots.spec.ts` renders every page and state (first-run setup and its errors, sign-in and its error
states, overview, workers, missions, mission detail, dialogs, menus, usage, diagnostics, account) in light and dark
on fixed data (`e2e/mock.ts`) with a fixed clock and animations disabled. The same states are scanned with axe-core
in both themes on all four projects and must have no violations of any impact.

The screens are listed per work package in `e2e/screens/<package>.ts` (`auth`, `shell`, `session`, `mission`, `inbox`,
`fleet`), aggregated by `e2e/screens/index.ts`. A package adds or changes screens only in its own file, and a screen's
name is its baseline file name, so names stay unique across packages.

Baselines (`e2e/__screenshots__/<project>/`) exist for `chromium-desktop` and `chromium-mobile` only. Firefox
renders text with different anti-aliasing and the runs would need a loose threshold; the Firefox projects run the
same states for behaviour and axe but are not compared pixel by pixel. Font rendering also differs between machines,
so baselines are generated and compared only inside the official image that matches `@playwright/test`:

```bash
docker run --rm --ipc=host -e CI=1 -e CARGO_TARGET_DIR=/work/coordinator/target \
  -v "$PWD/..":/work -w /work/web mcr.microsoft.com/playwright:v1.64.1-noble \
  node node_modules/@playwright/test/cli.js test e2e/screenshots.spec.ts \
  --project=chromium-desktop --project=chromium-mobile --update-snapshots
```

(Run `npm run build` and `cargo build --release --locked` first. With rootless Docker do not pass `--user`.)
CI compares against the committed files in the same image. On any other machine set `BRIDGE_SKIP_VISUAL=1` to skip
the pixel comparison and keep the axe checks. Update the image tag together with `@playwright/test`.

## Bundle budget and load time

`e2e/budget.spec.ts` counts the app entry and its transitive static JavaScript imports as initial load. Chunks reachable
only through dynamic imports are counted twice: the xterm.js chunk against the `terminal` cap and all other lazy chunks
(login, reset, account, workers, sessions, usage, diagnostics) against the `routes` cap. It fails when a group or the CSS
exceeds `budget.json`, when the terminal chunk is imported statically, or when a route listed under `lazyRoutes` is found in
the initial graph. The xterm.js stylesheet is a separate file loaded by the terminal chunk, not part of the first-load CSS. It also measures the
first-run page on Pixel 4 emulation with and without 4x CPU throttling and writes `test-results/load-metrics.json`.
