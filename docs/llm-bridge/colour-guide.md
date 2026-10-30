# Bridge colour guide

Summary: Bridge uses colour on four independent dimensions, and each dimension
has one fixed visual form. State is coloured text with a glyph. Capacity is a
coloured bar or percentage. Provider identity is a coloured `■` square before
the provider name. Mission identity is a coloured chip (`▌` bar or a filled
status-line segment) before the mission name. A colour never appears in a form
that belongs to another dimension, so a user can read "amber text" as "needs
attention" and "blue chip" as "this mission" without learning a legend. Every
colour cue is paired with a text cue, so mono, 8-colour, colour-blind and
`NO_COLOR` users lose nothing. The roles are implemented in
`llm_tools/bridge_theme.py` and previewed with `llm-bridge theme`.

## 1. Dimensions and their forms

| Dimension | Question it answers | Form (only this form) | Roles |
|---|---|---|---|
| State | What is happening, does it need me? | Status word plus glyph, coloured text | `state_running` ●, `state_waiting` ○, `state_review` ◆, `state_done` ✓, `state_failed` ✕, `state_paused` ‖ |
| Capacity | Can this provider keep working? | Percentage and bar, coloured | `capacity_ok`, `capacity_low` (≤ 25%), `capacity_empty` (0% or rate-limited) |
| Provider | Who is doing the work? | `■` square before the provider name; the name itself in default text | `provider_codex`, `provider_claude`, `provider_copilot`, `provider_other` |
| Mission | Which mission or repository am I looking at? | `▌` chip before the mission label; in tmux, the centre window-status segment's background | `mission_1` … `mission_8` (see section 3) |

Severity messages (`ok`, `warn`, `error`) are state for one-off results such as
"Applied", "Rejected" and "Unknown command". They use the same hue families as
the state roles.

## 2. Structure roles (no meaning beyond emphasis)

- `brand`: the `LLM BRIDGE` wordmark only.
- `heading`: view titles and section labels (`Controller`, `Now`, `Recent`).
- `accent`: focus, meaning the active tab, the focused field and the command
  prompt `›` while typing. One focused thing at a time.
- `selected`: the selection bar. This is the only filled background inside
  views.
- `key`: key names in hints (`Ctrl-D`, `Enter`, `/approve`). The action words
  stay `muted`.
- `muted`: secondary information such as IDs, times, ages, folders, and hint
  verbs.
- `dim`: separators, borders and disabled items.
- `info`: everything else, in the terminal's own foreground.

## 3. Mission identity colours

A user who runs several missions across repositories must be able to tell
stations apart at a glance, including in a terminal tab title they only
glimpse.

- Each mission gets one of eight identity hues, `mission_1` … `mission_8`. The
  hue is chosen by a stable hash of the mission ID (for example `int(id[:8], 16)
  % 8`). It never changes during the mission's life. With eight hues, about
  one pair of missions in eight shares a hue, even in the same folder; pin one
  with `llm-bridge mission colour` to tell them apart.
- The identity hues avoid the state hues: none is pure green, amber/orange or
  red. The palette is drawn from teal, azure, blue, indigo, violet, magenta, pink and
  slate (slots 1-8, ordered by hue), and each value meets the contrast rules of
  the active theme. The eight values of every theme are at least 10 apart in
  OKLab (distance x100; about 2 is just noticeable), so no two chips read as the
  same colour, and the sets differ per theme because the colours must also meet
  the contrast floor on that theme's backgrounds (`tests/test_bridge_r7b_hues.py`). Where
  a hue is close to a provider square colour, the different form (chip versus
  square) keeps them apart, and the two never sit next to each other.
- **Where the mission chip appears:**
  - home-screen mission rows (`▌` before the name, replacing the plain `●` "here"
    marker, which moves to the folder column);
  - the mission picker rows;
  - the console header (`LLM BRIDGE · ▌ c64bridge password handling ·
    RUNNING`);
  - the tmux station's centre window-status segment, as a filled background
    with a contrast-checked foreground, holding the mission label. The left
    segment carries the `Bridge: Ctrl-] then B` hint.

  That status segment is the strongest cue when switching stations with
  `Ctrl-]` then `m`.
- **Where it never appears:** status words, capacity, provider names, hints and
  selection bars.
- Users may pin a colour per mission (`llm-bridge mission colour --mission ID
  N`) and override the eight hues via `[bridge.theme.roles] mission_1 = "#..."`.

## 4. Rules

1. **One meaning per hue family per screen.** If amber means "needs you", no
   decorative element on that screen may be amber.
2. **Colour never carries information alone.** Every coloured element has a
   word, glyph or position that says the same thing.
3. **Colour the smallest element that carries the meaning.** Colour the status
   word, not the whole row. Colour the `■` square, not the provider name. Colour
   the `▌` chip, not the mission title. Rows stay in the default foreground, so
   long lists remain calm.
4. **Attention budget:** at most one `state_review` or `error` element should
   draw the eye per view. The `Needs you` row is the canonical place.
5. **The background belongs to the user.** No fills except the selection bar
   and the tmux mission segment.
6. **Finished things recede.** Rows of completed or cancelled missions and tasks
   use `muted` for everything except the state word.
7. **Same thing, same colour everywhere.** A task's state colour in the Tasks
   list, the preview separator, the Overview `Now` row and the tmux status line
   is identical.
8. **Degradation:**
   - `mono`: bold for `state_review`/`error`, underline for `accent`, reverse
     for `selected`. The wordmark, headings and key names also stay bold and
     secondary text stays dim; see Deviations.
   - 8/16 colours: the nearest safe ANSI colour, as decided by
     `fit_role_color`.
   - Non-UTF-8 locale: the state glyphs fall back to ASCII (`*` running, `o`
     queued, `#` verifying, `v` completed, `x` failed, `=` paused), and `|`
     replaces the `·` separator.

## 5. Where each role is applied (implementation checklist)

| UI element | Role |
|---|---|
| Wordmark | `brand` |
| Mission chip and tmux centre (window-status) segment | `mission_N` |
| Mission/task status word and glyph (header, lists, Now, preview separator, tmux) | `state_*` |
| Active tab, focused field, command prompt while typing | `accent` |
| Selected row | `selected` |
| Section labels (`Controller`, `Now`, `Next`, `Needs you`, `Recent`, `Capacity`) | `heading` (`Needs you` label: `state_review`) |
| IDs, ages, times, folders, hint verbs, "no output for 2m" | `muted` (inactivity ≥ 10 min: `warn`) |
| Key names in hints and in Actions rows (`/pause`) | `key` |
| Provider square `■` | `provider_*` |
| Capacity percentage/bar, `lasts past reset`, `resets in` | `capacity_*` / `muted` |
| Activity rows: plumbing | `dim`; operator events default; failures `error` |
| Command results | `ok` / `warn` / `error` |
| Separators, borders | `dim` |

## 6. Implementation notes

- **Roles and helpers.** `mission_1` ... `mission_8` are ordinary roles in every
  theme (text colour on the terminal background). `bridge_theme.mission_role(id,
  config)` returns the pinned `config["colour"]` (1-8) or `int(id[:8], 16) % 8 +
  1`; `mission_chip_style` / `mission_chip_sgr` / `tmux_mission_chip_markup` give
  the filled chip form (the hue as background, black or white text, whichever
  contrasts more, at least 4.5:1). `llm-bridge mission colour [--mission ID]
  N|auto` records a `mission-colour` event.
- **One vocabulary.** `bridge_glyphs.STATE_LOOK` maps a status word to its glyph
  and role; the console, home screen, picker and tmux status all use it.
  Every string drawn goes through `bridge_glyphs.localize`, so a non-UTF-8
  locale never receives a non-ASCII byte.
- **Multi-segment lines.** `bridge_tui._put_segments` draws `(text, role)`
  segments clipped by cell width. System-written phrasing (percentages, ages,
  state words, `/commands`, key names, ids) is coloured by
  `_inline_segments`; user-written text such as task titles is never tokenised.
- **tmux station.** The label is drawn with `@llm_bridge_chip` (the chip
  style); the state text is `@llm_bridge_context_styled` (state words in their
  roles) when the console published one, else the plain `@llm_bridge_context`.
  The `Ctrl-]` hints use `key` for key names and `muted` for the words.

### Deviations from the sections above

- **Neutral fallback.** No colour reaches 4.5:1 on both white and black. When
  the background is not detected the `neutral` palette keeps every hue at the
  best attainable level (about 3.6:1 on all typical backgrounds), uses bold for
  emphasis, `dim` for secondary text and reverse for the selection.
- **Mono.** Bold marks `state_review`, errors and failures, underline marks
  `accent`, and reverse only the selection. Wordmark, headings and key names stay
  bold and secondary text stays dim, because nothing would separate labels from
  values otherwise.
- **8/16 colours.** The eight mission hues collapse onto the few ANSI colours
  that contrast with the background, so two missions can share a colour there;
  the label and the `▌` glyph still identify the mission.
- **Task counts.** The `Tasks` line of the Overview colours `running`, `queued`,
  `completed` and `paused` words but keeps `verifying` and `blocked` in default
  text, so the `Needs you` row stays the one element that draws the eye.
