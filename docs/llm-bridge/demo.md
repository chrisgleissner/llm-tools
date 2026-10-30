# Recording the LLM Bridge demo

`scripts/bridge_demo.py` records a scripted run of LLM Bridge and encodes it as an animated GIF (and optionally an MP4 and WebM) for the README and the project website. Three storyboards exist: `full`, the default 60 second walkthrough, `highlight`, a 20 to 25 second loop, and `limits`, a 17 to 18 second captioned loop in which a provider runs out of capacity, the work continues on the next one and ends waiting for your approval (README GIF and site clip). It is a manual tool and is not part of the test suite, apart from the renderer unit tests in `tests/test_bridge_demo_render.py` and the limits tests in `tests/test_bridge_demo_limits.py`.

The recording does not use a real provider, a real account or live capacity data. Everything the demo touches lives in one temporary directory that is removed at the end.

## Requirements

* Python 3 with Pillow (`python3 -c "import PIL"`).
* `tmux` and `git` for recording.
* `ffmpeg` for MP4 output and for palette-optimised GIFs. Without ffmpeg the GIF is written by Pillow with one shared 256-colour palette; MP4 output is not available.
* A monospace TrueType font. DejaVu Sans Mono (`fonts-dejavu-core`) is used when present, otherwise the font that `fc-match monospace` returns.

## Record

```bash
python3 scripts/bridge_demo.py --out dark.gif --mp4 dark.mp4 --theme dark --frames-out dark.json
python3 scripts/bridge_demo.py --out light.gif --theme light
python3 scripts/bridge_demo.py --multi --out multi.gif      # adds the two-mission scene
```

### Highlight loop (README and website hero)

```bash
python3 scripts/bridge_demo.py --storyboard highlight --theme dark \
  --out demo-highlight-dark.gif --mp4 demo-highlight-dark.mp4 \
  --webm demo-highlight-dark.webm --poster demo-highlight-dark.png --frames-out dark.json
python3 scripts/bridge_demo.py --storyboard highlight --theme light --out demo-highlight-light.gif \
  --mp4 demo-highlight-light.mp4 --webm demo-highlight-light.webm --poster demo-highlight-light.png
```

The highlight storyboard defaults to a 90x24 terminal with 10x20 pixel cells (928x528 pixels, about the aspect ratio of the site's hero video), 60 characters per second typing, a longest still hold of 3 s and a 2 s hold on the last frame. Encoding the WebM takes a few minutes.

Before the recording starts, the driver creates four missions in four repositories (`acme-web`, `acme-api`, `acme-docs`, `acme-infra`) with pinned colours and drives them to different states without showing it: one completed, one whose provider has exited and whose task awaits approval, one only created, and one whose provider is busy on its test run. The recording then starts on the home screen and shows, with the time each beat is held:

| Beat | What is on screen | Held (s) |
| --- | --- | --- |
| 1 | Home screen: four missions with colour chips and states | 2.2 |
| 2 | `Enter` opens the running mission's Overview; the status strip, next task, capacity and recent events | 2.0 |
| 3 | `/attach` shows the provider station with Bridge's status line | 1.4 |
| 4 | `Ctrl-]` then `M`: the mission picker, then the second mission is selected | 1.7 |
| 5 | `Ctrl-]` then `B`: that mission's Overview with *Needs you* (this is the poster) | 2.0 |
| 6 | `/approve` and `y`: the mission shows `COMPLETED` | 2.4 |
| 7 | Detach and leave the controls: the home screen again, with the mission now completed | 2.2 |

The loop restarts on the first frame. The home screen at the end differs from the first frame only in the second mission's state and the update ages; `--seam-window` (default 4 s for this storyboard) cuts the recording at the frame, among those in its last 4 seconds, that is closest to the first frame (`loop_seam_end`).

The poster is the capture at the `poster` mark that the storyboard sets in beat 5; for captures without a mark the densest screen is used (`poster_index`). A capture file saved with `--frames-out` stores the marks and the storyboard name, so `--frames-in` re-renders with the right defaults.

Some captures are removed before drawing (`drop_transient_samples`, `drop_blips`): empty screens between two launches of `llm-bridge`, the first paint of the controls (which shows the full mission id), tmux's `[detached ...]` messages and the empty tmux window shown for a moment when a station reached through the mission picker is detached, and single captures where tmux painted a half-scrolled provider pane between two identical screens.

Bridge's `/approve` hint and command use the task's number across all missions, not its number within the mission. The mission that is approved on camera is therefore created first, so that both numbers are 1.

The demo provider reads `DEMO_PROVIDER_GATE_DIR`: the pending test run of each mission is released by a file named after its folder, so the running mission stays busy while the others finish.

To rebuild every site clip, poster and the README GIF in one step (about 20 minutes), run `scripts/record_site_clips.sh` from the repository root, then `python3 docs/site/check_site.py`.

Size limits for the outputs: GIF at most 2.5 MB, MP4 at most 1.5 MB. The recording above produces a GIF of about 0.7 MB, an MP4 of about 0.8 MB and a WebM of about 0.6 MB.

### Limits loop (README GIF and site clip)

`--storyboard limits` is a 17 to 18 second loop for the claim "run out of tokens, not momentum": one provider uses up its 5-hour window and the same task carries on with the next provider, which finishes it and leaves it waiting for your approval. It is a scripted demo. The two providers are the demo provider (no model, no network), and the capacity figures are injected. The terminal part of every frame is Bridge's own output. The recorder adds two things on top, both described below: a caption band under the terminal and a reading highlight on one row.

```bash
for theme in dark light; do
  python3 scripts/bridge_demo.py --storyboard limits --theme $theme --out limits-$theme.gif \
    --mp4 demo-limits-$theme.mp4 --webm demo-limits-$theme.webm --poster demo-limits-$theme.webp
  python3 scripts/bridge_demo.py --storyboard limits --theme $theme --size 60x30 --out limits-mobile-$theme.gif \
    --mp4 demo-limits-mobile-$theme.mp4 --webm demo-limits-mobile-$theme.webm --poster demo-limits-mobile-$theme.webp
done
```

The site assets are `docs/site/assets/demo-limits-{dark,light}.{mp4,webm,webp}` and the `demo-limits-mobile-*` variants. The mobile variant is recorded in a 60x30 terminal (not cropped from the wide one), so Bridge itself lays out its screens for 60 columns; use it for screens up to 600 px wide. The README GIF `docs/img/llm-bridge-demo.gif` is made from the dark 90x24 recording (12 frames per second, 960 px wide, at most 2.5 MB). The default cell size is 12x24 pixels. The frame is the terminal (1108x624 for 90x24, 748x768 for 60x30) plus the caption band (84 px and 88 px high), so the clips are 1108x708 and 748x856.

To re-record after Bridge's wording changes, run the commands above again; the screens come from the live UI and nothing in the clip is typed in by hand. Two strings are looked up on screen while recording (`5-hour limit reached`, which the stand-in provider prints, and `Switched provider`), plus the Overview row labels `Recent`, `Now`, `Tasks` and `Needs you` for the highlight. Rotation reasons in parentheses are not looked up.

| Beat | Caption | What is on screen | Length (s) |
| --- | --- | --- | --- |
| 1 | Claude is working | Claude's provider station, already streaming when the clip starts. One line appears every half second. | 4.0 |
| 2 | 5-hour limit hit | The stand-in Claude prints `5-hour limit reached` and exits with status 1; the station shows `Provider exited (status 1)`. | 1.3 |
| 3 | Bridge hands the task to Codex | `Ctrl-]` then `B`: Overview. The capacity block shows Claude `(not ready)` at 0% and Codex active. Under Recent, the highlight steps down the last four events: provider exited, task handed off, switched provider, started provider. The poster is the frame with the highlight on `Switched provider`. Then back to the station: Codex runs its checks line by line. | 8.5 |
| 4 | Codex finishes · you approve | Codex prints `6 passed` and `Task complete`, exits, and `Ctrl-] B` shows the Overview with the task `VERIFYING` and `Needs you`. The highlight steps through Now, Tasks and Needs you; the last frame is held for 1.3 s. | 4.0 |

The loop restarts on the first frame with a plain cut.

How the clip is kept moving:

* **Steady streaming.** `DEMO_PROVIDER_CADENCE` (0.5 s) makes each stand-in print one line at a time. `DEMO_PROVIDER_GO_DIR` holds each stand-in until the recorder writes `<name>.go`, so Claude is already mid-stream at the first frame (the preroll releases it `LIMITS_HEAD_START` seconds before recording starts) and Codex starts when the station comes back.
* **Caption band.** `CaptionBand` draws four progress pills and one caption in theme colours (dark text on a light tint, white text on a dark tint; contrast at least 7:1). The caption is sized to the longest caption, 36 px on the wide clip and 38 px on the narrow one, which is about 19 px on a 390 px wide phone. Each new caption wipes in from the left over three capture steps, so every beat change shows motion. The marks `caption2` to `caption4` place the captions; the first one starts at time 0. Captions describe the screen and are in `CAPTIONS`.
* **Reading highlight.** `Walk` colours one Overview row at a time, 0.5 s per row, so the console beat is not a still screen. Rows are found by block label (`Recent`, then `Now`, `Tasks`, `Needs you`), not by their text.
* **Cut.** `cut_start` and `cut_end` remove the time tmux takes to switch the station from Claude's finished pane to Codex's banner (about a second). Later captures and marks move earlier.
* **Static run check.** The recorder prints the longest time one picture stays on screen, apart from the final hold, and warns above 0.8 s. The scene check used for review is `ffmpeg -i demo-limits-dark.mp4 -vf "select='gt(scene\,0.001)',showinfo" -f null -`.

How the scripted parts reach Bridge's own code paths:

* **Capacity figures.** `LLM_SCHEDULER_USAGE_JSON_FILE` names a file that holds the same JSON as `LLM_SCHEDULER_USAGE_JSON`; `common.usage_snapshot_for_provider` reads it again on every call. The controller's capacity observer (`--capacity-refresh-seconds 5`, the smallest value Bridge accepts) and Ralph's provider selection both go through that function, so rewriting the file changes what the Overview shows within five seconds and what the selection decides. The variable is unset by default and has no effect then. The driver publishes steps (Claude 14, 12, 6 and 0 percent left; Codex 89, 89, 88 and 86); steps 1 and 3 are used, the last one when Claude's window is used up.
* **Forecast.** `empty in ~2m at this pace` is Bridge's own estimate from the usage log (`common.estimate_remaining_time_from_log`). The driver writes 33 minutes of history to that log before the mission starts: one sample a minute, Claude falling from 100% to its first figure at about 2.6 points a minute, Codex falling slowly. Each refresh during the recording appends a real sample.
* **Rotation.** `[ralph] even_burn = false` in the demo's llm-tools config keeps the mission on Claude until Claude cannot be used, and `retry_delays = ""` hands the task over as soon as the provider exits with an error instead of retrying it after a pause. Bridge does not interrupt a running provider when capacity falls; the hand-off happens when the provider process ends, which is what the demo provider does at 0%.
* **Demo providers.** The launchers `claude` and `codex` set `DEMO_PROVIDER_NAME`; `DEMO_PROVIDER_SCENARIO=limits` selects the story. The Claude stand-in streams until the file named by `DEMO_PROVIDER_LIMIT_FILE` exists (the driver creates it after the first 3.4 s), prints that the 5-hour limit is reached and exits with status 1. The Codex stand-in resumes from the working tree, passes its tests, prints `Task complete` and exits with status 0, which moves the task to verification. Both repaint their transcript on a terminal resize, as a full-screen agent does; without that, tmux can leave blank rows above the output of a program that never redraws after the station resizes the pane.

Encoding. `--max-kb 600` (the default for `limits`) raises the CRF of the MP4 and WebM until each file is at most 600 KB. Both are written for reliable playback: H.264 High, `yuv420p`, `-movflags +faststart`, a key frame every second (`-g 30 -keyint_min 30 -sc_threshold 0`), and VP9 with the same key frame interval. The key frames dominate the size of a text clip, which is why the CRF ends up high (34 to 40).

Known limits of what is shown. The Usage view (`F8`) reads the real provider readers, which have no injection, so the demo does not show the quota bars of `llm-usage`; Bridge's own capacity lines are used instead. At 60 columns Bridge shortens the tab strip, truncates long Recent lines and cuts `Task 1` in the station status line to `<sk 1 >`; that is Bridge's output and is not changed for the recording.

### Full walkthrough

A recording takes about 70 seconds of real time and must not be disturbed while it runs: the driver types into a private tmux server, but it also starts a Bridge controller, and heavy machine load changes the pacing.

Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--out PATH` | `demo.gif` | GIF output. |
| `--mp4 PATH` | none | Also write an H.264 MP4 (30 fps, yuv420p). |
| `--theme dark\|light` | `dark` | Renderer palette. While recording it also sets `LLM_BRIDGE_THEME`, so Bridge draws its own dark or light colours. A light recording needs its own run. |
| `--storyboard full\|highlight\|limits` | `full` | Which scripted run to record (see above). A saved capture remembers its storyboard. |
| `--webm PATH` | none | Also write a VP9 WebM. |
| `--poster PATH` | none | Also write the most informative frame as a PNG. |
| `--seam-window S` | off, `highlight` 4 | Cut the end of the recording at the frame, in its last S seconds, that looks most like the first. |
| `--gif-colors N` | `256` | GIF palette size. |
| `--size COLSxROWS` | `110x30` (`highlight` and `limits` `90x24`) | Terminal size. The pixel size is the cell size times this, plus the title bar and padding. |
| `--speed N` | `1.0` | Playback speed. Frame durations are divided by N. |
| `--fps N` | `10` | Screen capture rate while recording. |
| `--cps N` | `25` (`highlight` and `limits` `60`) | Typing speed in characters per second. |
| `--max-hold S` | `4` (`highlight` `3`, `limits` `1.6`) | Longest time a still frame stays on screen. Longer real pauses are shortened. |
| `--tail-hold S` | `3` (`highlight` `2`, `limits` `1.3`) | Time the last frame stays before the loop restarts. |
| `--gif-fps N`, `--gif-width PX` | frame timing, frame width (`limits` `12`, `960`) | Frame rate and width of the GIF. |
| `--max-kb N` | none (`limits` `600`) | Raise the CRF until the MP4 and the WebM are each at most N KB. |
| `--cell-width PX` | `10` (`limits` `12`) | Cell width in pixels; the height is twice the width and the font size follows. |
| `--no-cursor`, `--no-chrome` | off | Omit the cursor or the window title bar. |
| `--multi` | off | Append the two-mission scene (see below). |
| `--frames-out PATH` | none | Save the raw captures as JSON. The file is written even if the storyboard fails part-way, which helps when debugging. |
| `--frames-in PATH` | none | Skip recording and render a saved capture. |
| `--keep-temp` | off | Keep the temporary directory. |

## Re-render without recording

Captures are saved with `--frames-out`. Rendering options (speed, cell size, cursor, chrome, hold times) can then be changed without another recording:

```bash
python3 scripts/bridge_demo.py --frames-in dark.json --out dark-fast.gif --speed 1.5 --max-hold 2.5
python3 scripts/bridge_demo.py --frames-in dark.json --out dark-large.gif --cell-width 12
```

The `--theme` of a re-render only changes the renderer palette. Because Bridge picks its own colours from `LLM_BRIDGE_THEME`, re-rendering a dark capture with `--theme light` produces light-on-dark application colours on a light background. Record again with `--theme light` instead.

## What the recording does

The driver creates the following, all below one temporary root with mode 700 directories:

* `home/` with a git repository `acme-api` (and `acme-web` with `--multi`). `HOME` points here, so Bridge shows `~/acme-api`.
* `state/` as `LLM_BRIDGE_STATE_DIR`, and `run/` as `XDG_RUNTIME_DIR`, so mission data and the station tmux sockets are private to the recording.
* `bin/` with launchers named `codex`, `claude` and `copilot` that run `scripts/bridge_demo_provider.py`, and `llm-bridge` that runs the repository's `llm-bridge`. Bridge finds these on `PATH`, so the home screen lists the three providers as installed and starts the demo provider for codex. A real provider CLI on the machine is never started, because the recording's `PATH` is `bin/` plus `/usr/bin:/bin` and the shell runs under `env -i`.
* A private tmux server (`tmux -L bridge-demo-<pid>`) holding one detached 110x30 session that runs a bash shell. The recorder runs `tmux capture-pane -e -p` on it about ten times a second. The mission's own station runs in a second tmux server (Bridge's normal behaviour) and is attached as a nested client, so the capture shows what a user sees.

The environment sets `LLM_USAGE_NO_SERVICE=1`, `LLM_USAGE_DISABLE_COPILOT=1`, `LLM_USAGE_DISABLE_CODEX_APP_SERVER=1`, `LLM_USAGE_LIVE_FETCH_RETRIES=0`, `LLM_TOOLS_NO_INHIBIT=1`, `LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1` and `LLM_SCHEDULER_USAGE_JSON` with fixed capacity figures (codex: 66% left in the 5h window, 81% weekly). No network access or live usage read happens. The reset times in that JSON are computed from the current time at the start of the run, so the "resets in" values on screen vary by a few minutes.

The fake provider does not use `--command-template`, because the home screen form does not accept one. Instead it is launched as `codex -C <dir> <prompt>`, exactly as Bridge launches the real CLI. The provider prints a banner that says `demo provider`, streams invented tool calls (reading files, a diff, running tests) and has no network or process access. One tool call ("Running tests") waits for a gate file that the driver creates when the recording returns to the provider, so the provider is visibly busy while the Bridge controls are shown. It then asks for `/exit`. `DEMO_PROVIDER_SPEED` (set to 1.5 by the driver) scales its delays.

### Storyboard

1. **Home screen.** Shell prompt, `llm-bridge` typed, first-run home screen, `N`, the objective typed, Tab through Providers (codex preselected), Folder and Mode (Native interactive), then Enter on *Start and open provider*.
2. **Station.** The demo provider streams its work. Two more tasks are queued with `llm-bridge task add` from the driver (not visible on screen), as if from another terminal.
3. **Bridge controls.** `Ctrl-]` then `b`: Overview (status strip, capacity, recent events), F2 Tasks with the selected task previewed, Down twice, `Ctrl-K` Actions.
4. **Back to the provider.** `Ctrl-]` then `p`. The provider finishes the tests, prints a summary and asks for `/exit`. `/exit` is typed; the provider exits and task 1 moves to verification.
5. **Approval.** `Ctrl-]` then `b`, F1 Overview shows *Needs you*, `/approve` is typed, the confirmation is answered with `y`, and F2 shows task 1 completed and task 2 running. Bridge starts the next ready task as soon as the first provider exits, so task 2 is already running at this point.
6. **`--multi` only.** A second mission is saved and started in `~/acme-web` (not visible), `Ctrl-]` then `d` detaches, `Ctrl-D` leaves the controls, `llm-bridge` shows the mission list with both missions, and the second one is opened.

The default recording runs about 64 seconds; `--multi` about 80 seconds. Typing speed and pauses are set in `run_storyboard` and `run_multi_scene`; typing jitter uses a fixed random seed, so a recording differs between runs only by timestamps and the clock values Bridge prints.

### Paths in the captures

Private temporary paths are replaced in the captured text before it is saved or drawn (`/…/home` becomes `/home/demo`, padded with spaces so later columns do not move). Bridge itself prints `~/acme-api` in most places, but the *Folder* field of the new mission form shows the absolute path.

## Renderer

`parse_ansi` reads the SGR stream that `capture-pane -e` produces: reset, bold, dim, italic, underline, strike, reverse, the 16 base colours, the 256-colour palette (`38;5;n`) and truecolor (`38;2;r;g;b`), including the colon sub-parameter forms, and default colours. Wide characters take two cells, combining marks attach to the preceding cell, and other escape sequences are ignored.

`Renderer` draws each cell on a fixed grid (10x20 pixels by default; the font size is derived from the cell width so that glyph advances are whole pixels). Box-drawing characters and block elements are drawn as rectangles instead of font glyphs so lines join without gaps between cells. Characters missing from DejaVu Sans Mono are looked up through `fc-match` with a charset query. Rows that did not change between frames are cached.

Identical consecutive captures are merged into one frame whose duration is the time until the next different capture. Durations are rounded to 10 ms (the GIF delay unit) with the rounding error carried forward so the total length stays correct.

The GIF is encoded with ffmpeg `palettegen` and `paletteuse` (no dithering, which keeps text edges crisp); a typical 64 second recording is about 1 MB. The MP4 is H.264, CRF 16, 30 fps. Both are produced from the same frame list.

## Troubleshooting

* `timed out waiting for '...'` followed by a screen dump: the storyboard waits for text on the screen. Bridge's wording may have changed; update the `wait_for` strings in `run_storyboard`.
* A recording that stops early leaves nothing behind: the driver stops the controller and station tmux servers it started and removes the temporary directory, even on failure. Use `--keep-temp` to inspect the state directory.
* Missing glyphs (empty boxes) mean the system font lacks a character; install `fonts-dejavu-core` or a font that covers it.
* If `tmux` reports an unexpected pane size, check that no `~/.tmux.conf` setting overrides it. The driver starts its server with `-f /dev/null`.

## Tests

```bash
python3 -m pytest -q tests/test_bridge_demo_render.py tests/test_bridge_demo_limits.py tests/test_usage_json_file.py
```

The tests cover the highlight helpers (transient and blip filtering, loop seam, poster selection, saved marks, storyboard defaults and the seeded missions), SGR parsing (including 256-colour, truecolor and reverse video), wide and combining characters, frame de-duplication and duration handling, and rendering of backgrounds, box drawing, blocks and the cursor. The limits tests cover the capacity steps and file writes, the seeded usage history (including that `common.estimate_remaining_time_from_log` produces a forecast from it), the demo config, the extra transient-screen rule, and the Claude stand-in's exit status; `tests/test_usage_json_file.py` covers `LLM_SCHEDULER_USAGE_JSON_FILE`. They need Pillow and a monospace font, but not tmux or ffmpeg.
