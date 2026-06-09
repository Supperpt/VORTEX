# Handoff: VORTEX — Polished TUI (interactive shell dashboard)

## Overview
This package specifies a **polished terminal UI (TUI)** for VORTEX's existing interactive shell
(`./run-cli.sh shell`). The goal is to turn the current command-by-command REPL into a
**stateful dashboard**: a persistent session-status panel, a pipeline state-machine view that
shows the optimal workflow and "where you are", an inline ASCII bulge-heatmap for the
`clip-sac` tune-and-look loop, and a command line with autosuggestion.

This stays **CLI-native on purpose** — no OpenGL, no Qt, no X11/Wayland window server. Per the
project's own history (`LLM.md`, "GUI Stability & X11/Wayland"), the GUI was abandoned because
of display-server instability. This design delivers a high-quality interface **inside the
terminal**, using the libraries the shell already depends on.

## About the Design Files
The files in this bundle (`VORTEX Wireframes.html`, `wireframe.css`, `wireframe.js`) are a
**design reference created in HTML** — a visual prototype of the intended terminal layout, not
production code to copy. Open `VORTEX Wireframes.html` in a browser and select the **"Polished
TUI"** tab to see the target. Only that tab (Approach B) is in scope here; the other tabs are
alternative directions that were not chosen.

**Your task is to recreate this design in the existing Python codebase** — specifically the
interactive shell in `vortex/cli.py` — using the libraries it already uses:

- **[`rich`](https://rich.readthedocs.io/)** — for the status/pipeline/clip-sac panels, tables,
  colored text, and the heatmap ramp.
- **[`prompt_toolkit`](https://python-prompt-toolkit.readthedocs.io/)** — for the command line,
  history, and autosuggestion.

Do **not** introduce a GUI toolkit, a web server, or any GL dependency. Do not break headless
execution (`run-cli.sh` sets `QT_QPA_PLATFORM=offscreen` / `LIBGL_ALWAYS_SOFTWARE=1`).

## Fidelity
**Low-fidelity wireframe.** Treat the HTML as a guide for **layout, content, and behavior**, not
pixel measurements (pixels are meaningless in a terminal). However, this README gives you the
**exact text content, glyphs, colors, and `rich` styles** to use, so the result should be a
faithful reproduction of the wireframe's information design.

---

## Target environment & file map
| Concern | Location |
|---|---|
| Interactive shell / REPL (`shell` command) | `vortex/cli.py` |
| Session state (what's loaded) | `vortex/cli.py` → `Session` dataclass + `vortex/state/app_state.py` (`PipelineParams`) |
| Each command handler | `vortex/cli.py` → `do_load`, `do_seed`, `do_segment`, `do_mesh`, `do_centerlines`, `do_extend`, `do_clip_sac`, `do_export`, … |
| `clip-sac` stats + bulge field | `vortex/pipeline/sac_clipping.py` (`compute_bulge_field`, returns median/p90/p99/max) |

The shell already prints a `status` table and uses `rich`'s `console.status` spinner — reuse and
extend those patterns rather than starting fresh.

---

## Rendering model (read this first — it resolves the hard part)

Do **not** attempt a full-screen `prompt_toolkit` Application or a `rich.Live` region that stays
pinned while the user types — combining a live-updating region with `prompt_toolkit`'s prompt is
fragile and easy to break in a headless terminal. That risk is exactly what this project is
trying to avoid.

**Use the "scrollback dashboard" model instead:**

1. On shell start, and **after every command completes**, clear the screen
   (`console.clear()`) and re-render the dashboard **once** (status panel + pipeline panel, plus
   the context panel for the last command, e.g. `clip-sac` stats).
2. Then drop to a normal `prompt_toolkit` `PromptSession.prompt()` for the next command.

This produces exactly the wireframe's appearance (dashboard on top, command line at the bottom)
with none of the live-region fragility. The "persistent" feeling comes from re-rendering the
compact dashboard each turn. Keep the dashboard compact so it always fits above the prompt.

---

## Component specs

The wireframe screen has four stacked regions. Build each as a `rich` renderable.

### 1. Session Status panel  (always visible)
A `rich.panel.Panel` titled `SESSION STATUS`, containing a borderless 3-column grid (label,
status, detail). Drives off the live `Session` object — show real values, the wireframe numbers
are placeholders.

Rows (in this order), with glyphs:
```
DICOM        ✓ loaded     584 slc
Seed         ✓ set        38.2 41.0 12.7
Mask         — (load-mesh)
Surface      ✓            99,184 pts
Centerlines  ✓            2 openings
Caps         ✓            extended
```
Rules:
- Present/ready → green `✓` + value. Not yet done → dim `—` + a hint of how to produce it.
- The detail column shows the most useful fact for that artifact (slice count, seed coords,
  point count, opening count, capped/extended state).

### 2. Pipeline state-machine panel  (always visible)
A `Panel` titled `PIPELINE`. Renders the workflow as text, grouped into the README's phases,
with separators `›`, the external-edit detour on its own line, and a "you are here" marker under
the **current** step. The current step is the next not-yet-completed command (or the one just
run). Completed steps are green, current step is bright cyan + bold, future steps are dim.

Exact layout (highlighting reflects the example state "at clip-sac"):
```
load › seed › segment › mesh
   └→ [edit ext.] → load-mesh
centerlines › extend › set-seed
 » clip-sac › check › export
         ▲ you are here
```
- `[edit ext.]` is the external MeshLab/Meshmixer hop — render in the "external" color (orange),
  it is intentionally outside the app.
- The `»` and `▲ you are here` marker move with the current step. Compute the current step from
  `Session` state (e.g. surface present + centerlines present + caps present + not yet clipped →
  current = `clip-sac`).

Place panels 1 and 2 **side by side** (two columns) when the terminal is ≥ ~100 cols, stacked
otherwise. Use `rich`'s `Columns` or a `Table.grid`.

### 3. Clip-sac context panel  (only after `clip-sac` runs)
A `Panel` titled `CLIP-SAC · ratio <N>` shown after the user runs `clip-sac`. Contents:

```
Bulge median / p90 / p99 / max   1.19 / 2.25 / 4.75 / 5.81
Dome cells   3,120      Parent cells   18,400

bulge field   1.0 ░░░░▒▒▒▒▓▓▓▓████ 5.8
              neck ········· dome apex

wrote sac_bulge_heatmap.ply  (diagnostic — open in MeshLab)
```

**The ASCII bulge heatmap** is the key new element. Build it data-driven, not decorative:
- Take the bulge-field stats already returned by `compute_bulge_field` (min≈1.0 … max).
- Render a fixed-width ramp (e.g. 24 chars) spanning min→max, using the block ramp
  `░ ▒ ▓ █` and a cold→hot color sweep: cyan → green → yellow → orange (low bulge = parent /
  cold; high bulge = dome / hot).
- **Mark the current `--ratio` cut position** on the ramp (e.g. a `┃` marker or a caret on the
  line below) so the user can see whether their ratio sits between p90 and p99. This is the
  whole point of the tune-and-look loop — make the cut location visible without leaving the
  terminal.
- Print min and max values at the ramp ends.

The numbers (median/p90/p99/max, dome/parent cell counts) must come from the real
`clip-sac` run, not be hardcoded.

### 4. Command line + hint  (the prompt)
Standard `prompt_toolkit` prompt, rendered after the dashboard:
```
vortex › ▌
          tab ⇥ accepts suggestion · ? for commands · F1 status
```
- Prompt string: `vortex › ` (note the `›` and trailing space). Use cyan/bold for `vortex ›`.
- Below the prompt, a dim one-line hint. (In `prompt_toolkit`, use a `bottom_toolbar` or print
  the hint above the prompt — a static dim line is fine.)
- `?` lists commands; `F1` re-prints the status table (these can map to existing handlers).

---

## Interactions & behavior
- **After every command**: clear, re-render dashboard (panels 1+2, plus panel 3 if a clip-sac
  result exists in the session), then prompt. Long operations keep using the existing
  `console.status` spinner *during* the operation; the dashboard re-renders after it finishes.
- **Autosuggestion**: enable `prompt_toolkit`'s `AutoSuggestFromHistory` on the `PromptSession`.
  `Tab` / `→` accepts the suggestion.
- **Smart completer (stretch)**: a `prompt_toolkit` `Completer` that suggests the *valid next
  commands* given session state (e.g. after `extend`, suggest `clip-sac`). Optional but high-value.
- **`clip-sac --ratio N`**: re-runs cheaply and replaces panel 3 with new stats + a new cut
  marker on the ramp. This is the tune-and-look loop.
- **Errors**: render in the orange/red error color inside the scrollback (don't crash the
  dashboard). Preserve the existing Unicode-safe printing fix (`'ascii','ignore'`) so malformed
  DICOM tags never crash `rich`.

---

## State the UI reads (already in `Session`)
| UI element | Reads from |
|---|---|
| Status: DICOM | `session.sitk_image` / loaded series + slice count |
| Status: Seed | `session.seed_mm` or `params.seed_point_ijk` |
| Status: Mask / Surface | `session.surface`, point count |
| Status: Centerlines | `session.centerlines`, `session.profiles` (opening count) |
| Status: Caps | `session.final_surface` has `CellEntityIds` / `extend` ran |
| Pipeline "you are here" | derived from the above (first incomplete step) |
| Clip-sac panel | `session.sac_surface` / stats from `compute_bulge_field` + dome/parent cell counts |
| Ratio shown | `params.sac_bulge_ratio` |

No new state should be required; if a value isn't tracked yet, add a minimal field to `Session`.

---

## Design tokens — terminal color palette
Map these to `rich` styles (define them once in a `rich.theme.Theme`). Hex values are the
wireframe's; the developer may snap them to the nearest 256-color/truecolor value `rich` supports.

| Token | Hex | Use | Suggested `rich` style |
|---|---|---|---|
| `bg` | `#1c1b17` | terminal background (don't force it; respect user's terminal) | — |
| `text` | `#d8d2c2` | default text | `default` |
| `dim` | `#8a8470` | panel titles, separators, hints, future steps | `dim` |
| `accent` | `#5fc6d8` | current step, prompt, cold end of heatmap | `cyan` / `bold cyan` |
| `ok` | `#7fb98a` | completed steps, `✓`, dome/parent counts | `green` |
| `warn` | `#d8c46a` | mid heatmap, cautions | `yellow` |
| `hot` | `#d8856a` | external-edit hop, errors, hot end of heatmap | `red` / `orange1` |
| `bright` | `#f1ece0` | emphasized values (stats numbers) | `bold white` |

Glyphs to use verbatim: `✓ — › » ▲ └→ ┃ · ░ ▒ ▓ █`. All are common and render in most terminals;
keep an ASCII fallback path if you want to be safe on minimal terminals.

---

## Implementation order (suggested commits)
1. **Pipeline panel** — add a `render_pipeline(session)` returning a `rich` renderable; call it
   after each command. (Low effort, high signal.)
2. **Status panel upgrade** — promote the existing `status` table into a compact always-rendered
   panel via `render_status(session)`; render panels 1+2 side by side.
3. **Autosuggest** — add `AutoSuggestFromHistory` to the `PromptSession`. (A few lines.)
4. **Clip-sac heatmap** — add `render_clip_sac(stats, ratio)` with the data-driven ramp + cut
   marker; show it after `clip-sac`. Wire real stats from `sac_clipping.py`.
5. **Scrollback dashboard loop** — refactor the shell loop to `clear → render dashboard → prompt`
   after each command. (Do this last; it ties 1–4 together.)
6. **Smart completer** (stretch) — state-aware next-command suggestions.

### Acceptance criteria
- Running `shell` shows the status + pipeline dashboard above the prompt and re-renders after
  each command, with the correct "you are here" marker for the current state.
- `clip-sac` prints the stats panel with a colored, data-driven ramp whose cut marker moves when
  `--ratio` changes.
- History autosuggestion works; `Tab`/`→` accepts.
- Everything works headless (`./run-cli.sh shell`) with no GL/Qt/X11 dependency and no crash on
  non-ASCII DICOM metadata.

---

## Files in this bundle
| File | What it is |
|---|---|
| `VORTEX Wireframes.html` | The visual reference. Open it and select the **"Polished TUI"** tab. |
| `wireframe.css` | Styles for the reference (so the HTML renders correctly). |
| `wireframe.js` | Tab switching + tweak controls for the reference. |
| `screenshots/polished-tui-overview.png` | Rendered screenshot of the target TUI layout. |
| `screenshots/polished-tui-annotated.png` | Same layout with the numbered annotations explaining each region. |
| `README.md` | This document — the self-sufficient spec. |

> The HTML reference also contains three other layout directions (Mission Control, Focus Stage,
> Pipeline Graph). They are **out of scope** — only "Polished TUI" was selected for development.
