# llm-tools project site

Static landing page for llm-tools. There is no build step, and the page loads
no scripts, fonts or analytics from other hosts (the footer badges are the only
external images): `index.html`,
`style.css`, `site.js` (theme toggle, tabs, copy buttons, hero video, full-demo
dialog) and `assets/`. All internal links are relative, so the site works when
opened from disk and when served from the `/llm-tools/` subpath on GitHub
Pages.

## Preview locally

```bash
python3 -m http.server 8000 --bind 127.0.0.1 -d docs/site
# open http://127.0.0.1:8000/
```

Dark is the default theme. Append `?theme=light` or `?theme=dark` to force one.
The header toggle stores the choice in `localStorage` (key `llm-tools-theme`).
The page works without storage or JavaScript.

## Page structure

| Section | Purpose |
| --- | --- |
| Hero | Headline, two-sentence lede, install tabs (pipx at a release tag, or from source), requirements, a link to the first-run steps, and a terminal with real `llm-usage` and `ralph-robin` output |
| See every limit (`#limits`) | The `llm-usage` table as text (wide and narrow variants), three points |
| When one runs dry (`#handoff`) | An example night as a timeline (HTML and CSS, no image), four short points |
| LLM Bridge (`#bridge`) | The limit hand-off clip, Plan, Run and Review as tabs with a screenshot each, the full-demo link, and six smaller features (mission picker, `/finish`, rotation, themes, wheel, JSON and `doctor`) |
| Control (`#control`) | Four cards for reliability, approval and review hold, cost awareness, and fit (no server, SSH, Apache-2.0) |
| Install (`#first-run`) | Requirements, then four steps that match `docs/llm-bridge/first-run.md`: install, `llm-bridge doctor`, start a mission, finish and approve. Measured timings and an optional `llm-usage` step |
| Built with itself | Counts taken from the author's Bridge database, and three dated facts |
| Know the limits (`#edges`) | Four limits of the tools and a privacy block: what is read, called and written |
| FAQ | Nine questions |
| Footer | Badges (external images with alt text), links |

Also published: `404.html` (absolute `/llm-tools/` URLs, because GitHub Pages serves it from any missing path under the project) and `sitemap.xml`. There is no `robots.txt`: crawlers read it only from the host root, which a project site does not control. The page carries `SoftwareApplication` JSON-LD with fields that can be checked against the repository; do not add a version, rating or review field.

Copy rules: no claim without a source in the code or docs, no social proof the
project does not have, no exclamation marks, no emoji, and nothing a new user
cannot observe. The first-run steps and timings must match `docs/llm-bridge/first-run.md`
and `./llm-bridge help quickstart`. The install command pins a release tag
(`@1.3.0`): publish the page only after that tag exists, or the install command
fails. Key names and commands must match `./llm-bridge help keys`.

Colours follow the semantic roles in `llm_tools/bridge_theme.py`. Keep the CSS
custom properties at the top of `style.css` in step with that file if the
palette changes. Spacing uses an 8px scale (`--s1` to `--s16`).

## Terminal text

The `llm-usage` table and the `ralph-robin` output are real program output
stored as HTML text, not screenshots. Text is crisp at every size, costs a few
hundred bytes, and holds no image for the OCR check to scan. To regenerate:

1. Run the tool on a pty with colour on. Use fixtures only: `HOME` and the cache
   in a temporary directory, `LLM_USAGE_NOW_EPOCH` fixed, session data from
   `tests/test_contracts.py` style JSONL, `LLM_USAGE_COPILOT_CAPTURE_TEXT` for
   Copilot, and `LLM_USAGE_PROVIDERS` limited to the providers shown. For
   `ralph-robin` use `--command-template` with a scripted agent and
   `LLM_SCHEDULER_USAGE_JSON`.
2. Convert the capture with `python3 scripts/site_terminal.py capture.ansi --from '^Provider'`.
   `--keep 0:11,19:47,69:78` produces the narrow table for phone screens
   (Provider, Scope, Remaining, Resets in). `--drop REGEX` removes a line, for
   example the one holding a local log path.
3. Paste the fragment into the matching `<pre>` in `index.html`.

The forecast in the table (`! empty in 1h 20m`) comes from a seeded
`llm-usage.log` (three samples for Claude and Codex over one hour). The page
says "sample numbers" next to each capture.

## Assets

**Screenshots must come only from the demo recorder** (`scripts/bridge_demo.py`,
`docs/llm-bridge/demo.md`): the demo provider, `~/acme-api` and `/home/demo`.
Never capture a real session, because a real terminal shows user names, host
names, home paths and prompts from private projects. `check_site.py` fails the
build when a file or the OCR text of an image contains a home path other than
`/home/demo`, or a known private string, and when an asset is not referenced.

| File | Source |
| --- | --- |
| `demo-{dark,light}.mp4` | The full recording, opened by "Watch the full 60-second run". |
| `step-{plan,run,review}-{dark,light}.png`, `-1x` variants | Step screenshots. Rendered by `scripts/site_stills.py` from an 84-column recording (`bridge_demo.py --size 84x30 --frames-out`). The 1x file is 700 px wide, the other is 1372 px for high-density screens; both are in `srcset`. |
| `social-card.png` | 1200x630 Open Graph and Twitter image, rendered by headless Chrome from `assets/src/social-card.html` (it embeds the real `llm-usage` text). |
| `favicon.svg` | The brand chip. |
| `src/` | Sources that are not referenced by the page (exempt from the unreferenced-asset check). |

The Bridge section opens with a looping clip of a limit hand-off, built from
`assets/demo-limits-{dark,light}.{mp4,webm,webp}` (plus `-mobile-` variants for
screens up to 600 px). Each theme has its own `.win only-*` frame and
`.hero-video-wrap` element. `site.js` starts and pauses these videos: a clip and
its poster load only when the visible theme's clip is within 300 px of the
viewport, the hidden theme's clip is never fetched, and `prefers-reduced-motion`
keeps the clip paused on its poster. The page has a play overlay, a Pause button
and a progress bar driven by `timeupdate`.

Render the step screenshots again with:

```bash
python3 scripts/bridge_demo.py --theme dark  --size 84x30 --out /tmp/d.gif --frames-out /tmp/dark.json
python3 scripts/bridge_demo.py --theme light --size 84x30 --out /tmp/l.gif --frames-out /tmp/light.json
python3 scripts/site_stills.py /tmp/dark.json dark docs/site/assets
python3 scripts/site_stills.py /tmp/light.json light docs/site/assets
```

## Checks before publishing

```bash
python3 docs/site/check_site.py
```

The Pages workflow runs it (with `tesseract-ocr` and `ffmpeg` installed) on pull
requests and before each deploy. It checks that every `src`, `href`, `poster`,
`srcset` candidate and CSS `url(...)` resolves, that the Open Graph image
exists, that no asset is unreferenced, and that nothing leaks a private path,
private IP address, token or private string. Images are OCR'd, and six frames
spread over each video and GIF are extracted with `ffmpeg` and OCR'd too.
Without `tesseract` or `ffmpeg` the check skips that step with a warning, and
fails when `CI` is set.

The file is published next to the site source, so it holds only generic
patterns (a home directory other than `/home/demo`, macOS and root home
directories, agent scratch directories, private IP ranges, and GitHub, `sk-`
and AWS token shapes; the list is `GENERIC_PATTERNS` in the script). Put your own user name, host names and e-mail address in a
regular-expression list outside the published tree, one pattern per line:
`LLM_TOOLS_SITE_PRIVATE_PATTERNS` (environment) or the untracked
`.site-private-patterns` file in the repository root (or the file named by
`LLM_TOOLS_SITE_PRIVATE_FILE`).

Also check by hand:

* No horizontal page scroll at 360 px width in both themes. Screenshots inside
  a window frame pan on small screens, with a fade and a "Swipe sideways" hint.
* Text contrast stays at WCAG AA (4.5:1) in both themes.
* The first view stays under 1.5 MB (it is under 150 KB without a hero video).

## Deploy

`.github/workflows/pages.yml` copies the site files into `_site/` (without
`check_site.py`, this README and `assets/src/`) and deploys that directory with
`actions/upload-pages-artifact` and `actions/deploy-pages` on pushes to `main`
that touch `docs/site/**`, and on manual dispatch. Only the deploy job has
`pages: write` and `id-token: write`.

One-time setup: in the repository, open Settings, Pages, and set Source to
"GitHub Actions". The site is then served at
`https://chrisgleissner.github.io/llm-tools/`.
