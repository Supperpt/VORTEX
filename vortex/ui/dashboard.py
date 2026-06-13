"""Polished TUI dashboard renderables for the VORTEX interactive shell.

Pure `rich` renderables that turn the command-by-command REPL (`vortex/cli.py`
→ `do_shell`) into a stateful "scrollback dashboard": a session-status panel, a
pipeline state-machine panel with a "you are here" marker, and a data-driven
ASCII bulge-heatmap panel for the `clip-sac` tune-and-look loop.

CLI-native on purpose — no GL/Qt/X11. See `UI_UX_development/README.md`
(Approach B / "Polished TUI") for the authoritative spec.

The render functions read live `Session` state but never mutate it, so they are
safe to call once per shell turn and easy to unit-test against a stub session.
"""

from __future__ import annotations

import sys

from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Theme — maps the spec's design tokens to rich styles (truecolor).
# ---------------------------------------------------------------------------

THEME = Theme({
    "vortex.dim":    "#8a8470",        # titles, separators, hints, future steps
    "vortex.accent": "bold #5fc6d8",   # current step, prompt, cold end of heatmap
    "vortex.ok":     "#7fb98a",        # completed steps, ✓, dome/parent counts
    "vortex.warn":   "#d8c46a",        # mid heatmap, cautions
    "vortex.hot":    "#d8856a",        # external-edit hop, errors, hot end of heatmap
    "vortex.bright": "bold #f1ece0",   # emphasized values (stat numbers)
})


# ---------------------------------------------------------------------------
# Glyphs — Unicode by default, ASCII fallback on limited terminals.
# ---------------------------------------------------------------------------

def _supports_unicode() -> bool:
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in enc


USE_UNICODE = _supports_unicode()

_GLYPHS_UNICODE = {
    "ok": "✓", "no": "—", "sep": "›", "cur": "»", "here": "▲",
    "detour": "└→", "arrow": "→", "cut": "┃", "dot": "·",
    "ramp": "░▒▓█",
}
_GLYPHS_ASCII = {
    "ok": "v", "no": "-", "sep": ">", "cur": ">", "here": "^",
    "detour": "\\>", "arrow": "->", "cut": "|", "dot": ".",
    "ramp": ".:+#",
}

GLYPHS = _GLYPHS_UNICODE if USE_UNICODE else _GLYPHS_ASCII


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------

def _has_dicom(session) -> bool:
    return getattr(session, "sitk_image", None) is not None


def _has_seed(session) -> bool:
    return (getattr(session, "seed_mm", None) is not None
            or getattr(session.params, "seed_point_ijk", None) is not None)


def _has_mask(session) -> bool:
    return getattr(session, "vtk_image", None) is not None


def _has_surface(session) -> bool:
    return getattr(session, "surface", None) is not None


def _has_centerlines(session) -> bool:
    return getattr(session, "centerlines", None) is not None


def _has_caps(session) -> bool:
    """True once `extend` ran — final_surface carries the CellEntityIds patch labels."""
    fs = getattr(session, "final_surface", None)
    if fs is None:
        return False
    try:
        return fs.GetCellData().GetArray("CellEntityIds") is not None
    except Exception:
        return False


def _has_sac(session) -> bool:
    return getattr(session, "sac_surface", None) is not None


def current_step(session) -> str:
    """First not-yet-completed step — drives the pipeline "you are here" marker.

    `seed`/`check` are intentionally non-blocking, so a typical
    surface+centerlines+caps (not-yet-clipped) state resolves to `clip-sac`,
    matching the wireframe example.
    """
    if not (_has_surface(session) or _has_dicom(session)):
        return "load"
    if _has_dicom(session) and not _has_mask(session) and not _has_surface(session):
        return "segment"
    if _has_mask(session) and not _has_surface(session):
        return "mesh"
    if not _has_centerlines(session):
        return "centerlines"
    if not _has_caps(session):
        return "extend"
    if not _has_seed(session):
        return "set-seed"
    if not _has_sac(session):
        return "clip-sac"
    return "export"


def _completed_steps(session) -> set:
    """Steps that are visibly done (rendered green in the pipeline panel)."""
    done = set()
    if _has_dicom(session):
        done.add("load")
    if _has_seed(session):
        done.update(("seed", "set-seed"))
    if _has_mask(session):
        done.add("segment")
    if _has_surface(session):
        done.add("mesh")
    if _has_centerlines(session):
        done.add("centerlines")
    if _has_caps(session):
        done.add("extend")
    if _has_sac(session):
        done.add("clip-sac")
    return done


def next_commands(session) -> list:
    """Valid next commands given session state — drives the smart completer.

    The current step first (highest-value suggestion), then sensible neighbours,
    then always-available commands.
    """
    cur = current_step(session)
    ordered = [cur]
    # After a clip, inspecting it is the highest-value next action.
    if _has_sac(session):
        ordered.append("view")
    # Reasonable follow-ons from the current state.
    if _has_surface(session):
        for c in ("check", "remesh", "centerlines", "extend", "clip-sac", "cap_label", "export", "metrics"):
            if c not in ordered:
                ordered.append(c)
    if not _has_surface(session):
        for c in ("load", "load-mesh", "list", "seed", "segment", "mesh"):
            if c not in ordered:
                ordered.append(c)
    for c in ("status", "params", "help", "exit"):
        if c not in ordered:
            ordered.append(c)
    return ordered


# ---------------------------------------------------------------------------
# Panel 1 — Session Status
# ---------------------------------------------------------------------------

def _row(grid, label, ready, value, hint):
    g = GLYPHS
    if ready:
        grid.add_row(label, Text(f"{g['ok']} ", style="vortex.ok") + Text(value or "", style="vortex.ok"),
                     "")
    else:
        grid.add_row(label, Text(f"{g['no']} ", style="vortex.dim"),
                     Text(hint, style="vortex.dim"))


def render_status(session) -> Panel:
    """SESSION STATUS panel — live artifact readiness from the Session object."""
    g = GLYPHS
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="vortex.dim", no_wrap=True)   # label
    grid.add_column(no_wrap=True)                        # status (✓ value / — )
    grid.add_column(style="vortex.dim")                  # detail / hint

    # DICOM
    if _has_dicom(session):
        try:
            slc = session.sitk_image.GetDepth()
            detail = f"{slc} slc"
        except Exception:
            detail = "loaded"
        grid.add_row("DICOM", Text(f"{g['ok']} loaded", style="vortex.ok"), detail)
    else:
        grid.add_row("DICOM", Text(f"{g['no']}", style="vortex.dim"),
                     Text("(load <dir>)", style="vortex.dim"))

    # Seed
    if getattr(session, "seed_mm", None) is not None:
        x, y, z = session.seed_mm
        grid.add_row("Seed", Text(f"{g['ok']} set", style="vortex.ok"),
                     f"{x:.1f} {y:.1f} {z:.1f}")
    elif getattr(session.params, "seed_point_ijk", None) is not None:
        ijk = session.params.seed_point_ijk
        grid.add_row("Seed", Text(f"{g['ok']} set", style="vortex.ok"),
                     f"ijk {ijk[0]},{ijk[1]},{ijk[2]}")
    else:
        grid.add_row("Seed", Text(f"{g['no']}", style="vortex.dim"),
                     Text("(set-seed X Y Z)", style="vortex.dim"))

    # Mask
    if _has_mask(session):
        grid.add_row("Mask", Text(f"{g['ok']} extracted", style="vortex.ok"), "")
    else:
        grid.add_row("Mask", Text(f"{g['no']}", style="vortex.dim"),
                     Text("(segment / load-mesh)", style="vortex.dim"))

    # Surface
    if _has_surface(session):
        try:
            pts = session.surface.GetNumberOfPoints()
            detail = f"{pts:,} pts"
        except Exception:
            detail = ""
        grid.add_row("Surface", Text(f"{g['ok']}", style="vortex.ok"), detail)
    else:
        grid.add_row("Surface", Text(f"{g['no']}", style="vortex.dim"),
                     Text("(mesh / load-mesh)", style="vortex.dim"))

    # Centerlines
    if _has_centerlines(session):
        n_open = len(getattr(session, "profiles", None) or [])
        detail = f"{n_open} openings" if n_open else ""
        grid.add_row("Centerlines", Text(f"{g['ok']}", style="vortex.ok"), detail)
    else:
        grid.add_row("Centerlines", Text(f"{g['no']}", style="vortex.dim"),
                     Text("(centerlines)", style="vortex.dim"))

    # Caps
    if _has_caps(session):
        grid.add_row("Caps", Text(f"{g['ok']}", style="vortex.ok"), "extended")
    else:
        grid.add_row("Caps", Text(f"{g['no']}", style="vortex.dim"),
                     Text("(extend)", style="vortex.dim"))

    return Panel(grid, title="SESSION STATUS", title_align="left",
                 border_style="vortex.dim", padding=(0, 1))


# ---------------------------------------------------------------------------
# Panel 2 — Pipeline state machine
# ---------------------------------------------------------------------------

# Each line is a list of (token, kind) where kind ∈ {step, sep, ext, plain}.
def _pipeline_lines():
    g = GLYPHS
    return [
        [("load", "step"), (f" {g['sep']} ", "sep"), ("seed", "step"),
         (f" {g['sep']} ", "sep"), ("segment", "step"),
         (f" {g['sep']} ", "sep"), ("mesh", "step")],
        [("   ", "plain"), (g["detour"], "sep"), (" ", "plain"),
         ("[edit ext.]", "ext"), (f" {g['arrow']} ", "sep"), ("load-mesh", "step")],
        [("centerlines", "step"), (f" {g['sep']} ", "sep"), ("extend", "step"),
         (f" {g['sep']} ", "sep"), ("set-seed", "step")],
        [("clip-sac", "step"), (f" {g['sep']} ", "sep"), ("check", "step"),
         (f" {g['sep']} ", "sep"), ("export", "step")],
    ]


def render_pipeline(session) -> Panel:
    """PIPELINE panel — workflow as a state machine with a "you are here" marker."""
    g = GLYPHS
    cur = current_step(session)
    done = _completed_steps(session)
    lines = _pipeline_lines()

    body = Text()
    for li, line in enumerate(lines):
        # The line that holds the current step gets the » prefix; others are indented
        # by one space so columns line up.
        has_cur = any(tok == cur and kind == "step" for tok, kind in line)
        prefix = f"{g['cur']} " if has_cur else "  "
        body.append(prefix, style="vortex.accent" if has_cur else "vortex.dim")

        col = len(prefix)        # running column to locate the current token
        cur_col = None
        for tok, kind in line:
            if kind == "step":
                if tok == cur:
                    cur_col = col
                    style = "vortex.accent"
                elif tok in done:
                    style = "vortex.ok"
                else:
                    style = "vortex.dim"
            elif kind == "ext":
                style = "vortex.hot"
            else:  # sep / plain
                style = "vortex.dim"
            body.append(tok, style=style)
            col += len(tok)
        body.append("\n")

        # "▲ you are here" line directly under the current token.
        if has_cur and cur_col is not None:
            body.append(" " * cur_col + f"{g['here']} you are here\n", style="vortex.accent")

    return Panel(body, title="PIPELINE", title_align="left",
                 border_style="vortex.dim", padding=(0, 1))


# ---------------------------------------------------------------------------
# Panel 3 — Clip-sac context (heatmap)
# ---------------------------------------------------------------------------

def _ramp_text(stats, ratio, width=24) -> Text:
    """Data-driven cold→hot block ramp spanning 1.0 → max, with the cut marker."""
    g = GLYPHS
    blocks = g["ramp"]
    band_styles = ["vortex.accent", "vortex.ok", "vortex.warn", "vortex.hot"]
    lo = 1.0
    hi = max(float(stats["max"]), lo + 1e-3)

    ramp = Text()
    for i in range(width):
        band = min(int(i / width * 4), 3)
        ramp.append(blocks[band], style=band_styles[band])

    # Cut-marker column for the current ratio.
    frac = (float(ratio) - lo) / (hi - lo)
    cut_col = max(0, min(width - 1, round(frac * width)))

    line = Text()
    line.append("bulge field   ", style="vortex.dim")
    line.append(f"{lo:.1f} ", style="vortex.accent")
    line.append(ramp)
    line.append(f" {hi:.1f}", style="vortex.hot")

    # Caret line under the ramp, aligned to the cut column (account for the
    # "bulge field   " + "1.0 " prefix length).
    prefix_len = len("bulge field   ") + len(f"{lo:.1f} ")
    caret = Text(" " * (prefix_len + cut_col) + g["cut"], style="vortex.bright")
    caret.append(f"  cut @ ratio {float(ratio):.2f}", style="vortex.dim")
    return line + Text("\n") + caret


def render_clip_sac(view) -> Panel:
    """CLIP-SAC panel from a cached view dict (stats/ratio/cell counts/heatmap_path)."""
    g = GLYPHS
    stats = view["stats"]
    ratio = view["ratio"]

    body = Text()
    body.append("Bulge median / p90 / p99 / max   ", style="vortex.dim")
    body.append(f"{stats['median']:.2f} / {stats['p90']:.2f} / "
                f"{stats['p99']:.2f} / {stats['max']:.2f}\n", style="vortex.bright")

    body.append("Dome cells ", style="vortex.dim")
    body.append(f"{view['dome_cells']:,}", style="vortex.ok")
    body.append("      Parent cells ", style="vortex.dim")
    body.append(f"{view['parent_cells']:,}\n\n", style="vortex.ok")

    body.append(_ramp_text(stats, ratio))
    body.append("\n")
    body.append(f"\n              neck {g['dot'] * 9} dome apex\n", style="vortex.dim")

    if view.get("heatmap_path"):
        body.append(f"\nwrote {view['heatmap_path']}  (diagnostic — open in MeshLab)",
                    style="vortex.dim")

    return Panel(body, title=f"CLIP-SAC {g['dot']} ratio {float(ratio):.2f}",
                 title_align="left", border_style="vortex.dim", padding=(0, 1))


# ---------------------------------------------------------------------------
# Dashboard — clear + render panels (scrollback model)
# ---------------------------------------------------------------------------

def render_dashboard(console, session) -> None:
    """Clear the screen and draw the dashboard above the prompt (one render/turn)."""
    console.clear()
    status = render_status(session)
    pipeline = render_pipeline(session)
    if console.width >= 100:
        console.print(Columns([status, pipeline], equal=True, expand=True))
    else:
        console.print(status)
        console.print(pipeline)

    view = getattr(session, "clip_sac_view", None)
    if view:
        console.print(render_clip_sac(view))
