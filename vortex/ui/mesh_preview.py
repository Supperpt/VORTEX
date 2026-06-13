"""In-terminal rotatable preview of the clip-sac result.

Replaces the "export the heatmap PLY and open it in MeshLab" round-trip with a
software-rendered, rotatable projection drawn in Unicode braille — no VTK render
window, no GL/Qt/X11 (consistent with the TUI's CLI-native premise).

Two toggleable views, both verifying a different thing about the clip:
  - "dome"  : dome (sac) hot vs parent vessel cold, with the neck cut ring bright
              → did it tag the right bulge, and where did the cut land?
  - "bulge" : continuous cold→hot BulgeRatio field with the --ratio cut contour
              → why is that region the dome?

Public API:
  build_preview(session)               → PreviewData | None
  render_frame(data, mode, az, el, zoom, cols, rows) → rich.text.Text
  run_viewer(console, session, mode)   → drives the interactive viewer
"""

from __future__ import annotations

import sys
from io import StringIO

import numpy as np
from rich.console import Console
from rich.text import Text

from vortex.ui.dashboard import THEME, USE_UNICODE
from vortex.pipeline.sac_clipping import BULGE_ARRAY
from vortex.utils.vtk_compat import vtk, vtk_np

# Braille dot bit per (sub_col, sub_row) inside a 2×4 character cell.
_BIT = np.array([[0x01, 0x02, 0x04, 0x40],
                 [0x08, 0x10, 0x20, 0x80]], dtype=np.uint8)

# ASCII density ramp indexed by number of set dots (0–8) — fallback path.
_ASCII_RAMP = " .:-=+*#%"

# Continuous cold→hot ramp for the bulge field (matches dashboard._ramp_text).
_BAND_STYLES = ["vortex.accent", "vortex.ok", "vortex.warn", "vortex.hot"]


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def _poly_points(poly) -> np.ndarray:
    if poly is None or poly.GetNumberOfPoints() == 0:
        return np.empty((0, 3))
    return vtk_np.vtk_to_numpy(poly.GetPoints().GetData())


def _boundary_points(poly) -> np.ndarray:
    """Open-boundary ring of *poly* — for the sac this is the neck cut location."""
    if poly is None or poly.GetNumberOfPoints() == 0:
        return np.empty((0, 3))
    fe = vtk.vtkFeatureEdges()
    fe.SetInputData(poly)
    fe.BoundaryEdgesOn()
    fe.FeatureEdgesOff()
    fe.ManifoldEdgesOff()
    fe.NonManifoldEdgesOff()
    fe.Update()
    return _poly_points(fe.GetOutput())


def _fit(points: np.ndarray):
    """Centroid and bounding-sphere radius — frames stay stable under rotation."""
    if len(points) == 0:
        return np.zeros(3), 1.0
    centroid = points.mean(axis=0)
    radius = float(np.linalg.norm(points - centroid, axis=1).max())
    return centroid, max(radius, 1e-6)


class PreviewData:
    def __init__(self, dome, parent, ring, bulge_pts, bulge_vals, ratio, caps=None):
        self.dome = dome
        self.parent = parent
        self.ring = ring
        self.bulge_pts = bulge_pts
        self.bulge_vals = bulge_vals
        self.ratio = ratio
        self.has_bulge = bulge_vals is not None and len(bulge_pts) > 0
        # caps: list of (uid:int, points:ndarray(N,3), centroid:ndarray(3,))
        self.caps = caps or []
        self.has_caps = len(self.caps) > 0

        union = [p for p in (dome, parent, ring) if len(p)]
        self.dome_centroid, self.dome_radius = _fit(
            np.vstack(union) if union else np.empty((0, 3)))
        if self.has_bulge:
            self.bulge_centroid, self.bulge_radius = _fit(bulge_pts)
        else:
            self.bulge_centroid, self.bulge_radius = self.dome_centroid, self.dome_radius


def build_preview(session) -> "PreviewData | None":
    """Pull the clip-sac geometry off the session, or None if clip-sac hasn't run."""
    sac = getattr(session, "sac_surface", None)
    if sac is None:
        return None

    dome = _poly_points(sac)
    parent = _poly_points(getattr(session, "parent_vessel", None))
    ring = _boundary_points(sac)

    bulge_surf = getattr(session, "bulge_surface", None)
    bulge_pts, bulge_vals = np.empty((0, 3)), None
    if bulge_surf is not None:
        bulge_pts = _poly_points(bulge_surf)
        arr = bulge_surf.GetPointData().GetArray(BULGE_ARRAY)
        if arr is not None:
            bulge_vals = vtk_np.vtk_to_numpy(arr).astype(float)

    ratio = float(getattr(session.params, "sac_bulge_ratio", 1.4))

    # Caps (inlet/outlet openings) from the capped surface, for the 'c' overlay.
    caps = []
    final = getattr(session, "final_surface", None)
    if final is not None:
        try:
            from vortex.pipeline.exporter import iter_caps
            for uid, poly, centroid, _area in iter_caps(final):
                caps.append((uid, _poly_points(poly), np.asarray(centroid, float)))
        except Exception:
            caps = []

    return PreviewData(dome, parent, ring, bulge_pts, bulge_vals, ratio, caps=caps)


# ---------------------------------------------------------------------------
# Projection & rasterization
# ---------------------------------------------------------------------------

def _rotation(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    az, el = np.radians(azimuth_deg), np.radians(elevation_deg)
    ca, sa = np.cos(az), np.sin(az)
    ce, se = np.cos(el), np.sin(el)
    ry = np.array([[ca, 0, sa], [0, 1, 0], [-sa, 0, ca]])
    rx = np.array([[1, 0, 0], [0, ce, -se], [0, se, ce]])
    return rx @ ry


def _project_to_dots(points, R, centroid, radius, zoom, cols, rows):
    """Project 3-D points to integer braille-dot coordinates; return (dx, dy, mask)."""
    if len(points) == 0:
        return np.empty(0, int), np.empty(0, int), np.empty(0, bool)
    W, H = cols * 2, rows * 4
    rot = (points - centroid) @ R.T
    scale = zoom * 0.5 * min(W, H) / radius
    dx = np.round(W / 2 + rot[:, 0] * scale).astype(int)
    dy = np.round(H / 2 - rot[:, 1] * scale).astype(int)   # flip y so up is up
    mask = (dx >= 0) & (dx < W) & (dy >= 0) & (dy < H)
    return dx, dy, mask


def _project_cell(point, R, centroid, radius, zoom, cols, rows):
    """Project one 3-D point to a character cell (col, row), or None if off-screen."""
    W, H = cols * 2, rows * 4
    rot = (np.asarray(point, float) - centroid) @ R.T
    scale = zoom * 0.5 * min(W, H) / radius
    dx = int(round(W / 2 + rot[0] * scale))
    dy = int(round(H / 2 - rot[1] * scale))
    if 0 <= dx < W and 0 <= dy < H:
        return dx // 2, dy // 4
    return None


def _layers_for(data: PreviewData, mode: str):
    """Yield (points, per_point_style_index, priorities, style_table, centroid, radius)."""
    if mode == "bulge" and data.has_bulge:
        vals = data.bulge_vals
        vmax = max(float(vals.max()), 1.0 + 1e-3)
        t = np.clip((vals - 1.0) / (vmax - 1.0), 0.0, 1.0)
        band = np.minimum((t * 4).astype(int), 3)
        style_table = list(_BAND_STYLES) + ["vortex.bright"]   # last = cut contour
        idx = band.copy()
        eps = max(0.02 * (vmax - 1.0), 0.03)
        cut = np.abs(vals - data.ratio) < eps
        idx[cut] = 4
        prio = vals.copy()
        prio[cut] = 1e9
        return [(data.bulge_pts, idx, prio)], style_table, data.bulge_centroid, data.bulge_radius

    # dome/parent split (default; also the fallback when no bulge field)
    style_table = ["vortex.accent", "vortex.hot", "vortex.bright"]  # parent, dome, ring
    layers = []
    if len(data.parent):
        layers.append((data.parent, np.zeros(len(data.parent), int), np.zeros(len(data.parent))))
    if len(data.dome):
        layers.append((data.dome, np.full(len(data.dome), 1), np.full(len(data.dome), 1.0)))
    if len(data.ring):
        layers.append((data.ring, np.full(len(data.ring), 2), np.full(len(data.ring), 1e9)))
    return layers, style_table, data.dome_centroid, data.dome_radius


def render_frame(data: PreviewData, mode: str, az: float, el: float,
                 zoom: float, cols: int, rows: int, show_caps: bool = False) -> Text:
    """Rasterize the chosen view into a braille (or ASCII) rich.Text of cols×rows.

    When *show_caps* is set, cap openings are drawn as dots and each cap's
    CellEntityId is stamped as a digit at its projected centroid — so the user
    can read off which number is which before running 'cap_label'.
    """
    cols = max(10, cols)
    rows = max(5, rows)
    R = _rotation(az, el)
    layers, style_table, centroid, radius = _layers_for(data, mode)

    bits = np.zeros((rows, cols), dtype=np.uint8)
    style_idx = np.full((rows, cols), -1, dtype=int)
    prio = np.full((rows, cols), -np.inf)

    for points, idx, pr in layers:
        dx, dy, mask = _project_to_dots(points, R, centroid, radius, zoom, cols, rows)
        for k in np.nonzero(mask)[0]:
            c, r = dx[k] // 2, dy[k] // 4
            bits[r, c] |= _BIT[dx[k] % 2, dy[k] % 4]
            if pr[k] > prio[r, c]:
                prio[r, c] = pr[k]
                style_idx[r, c] = idx[k]

    # Build the character/style grid from the rasterized dots.
    chars = [[" "] * cols for _ in range(rows)]
    cell_styles = [[None] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            b = int(bits[r, c])
            if b == 0:
                continue
            chars[r][c] = chr(0x2800 + b) if USE_UNICODE else _ASCII_RAMP[bin(b).count("1")]
            cell_styles[r][c] = style_table[style_idx[r, c]]

    # Overlay caps: faint dots for each opening, then a numeric label at its centre.
    if show_caps and data.has_caps:
        for uid, pts, _cen in data.caps:
            dx, dy, mask = _project_to_dots(pts, R, centroid, radius, zoom, cols, rows)
            for k in np.nonzero(mask)[0]:
                c, r = dx[k] // 2, dy[k] // 4
                if chars[r][c] == " ":
                    chars[r][c] = chr(0x2800 + int(_BIT[dx[k] % 2, dy[k] % 4])) if USE_UNICODE else "·"
                    cell_styles[r][c] = "vortex.warn"
        for uid, _pts, cen in data.caps:
            cell = _project_cell(cen, R, centroid, radius, zoom, cols, rows)
            if cell is None:
                continue
            c0, r0 = cell
            for j, ch in enumerate(str(uid)):
                c = c0 + j
                if 0 <= c < cols and 0 <= r0 < rows:
                    chars[r0][c] = ch
                    cell_styles[r0][c] = "vortex.bright"

    out = Text()
    for r in range(rows):
        for c in range(cols):
            ch = chars[r][c]
            if ch == " ":
                out.append(" ")
            else:
                out.append(ch, style=cell_styles[r][c])
        if r < rows - 1:
            out.append("\n")
    return out


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------

def _footer(state, data: PreviewData) -> Text:
    f = Text()
    f.append(f"az {state['az']:.0f}° el {state['el']:.0f}° · ×{state['zoom']:.2f} · ",
             style="vortex.dim")
    f.append(f"{state['mode']}", style="vortex.accent")
    f.append(f" · ratio {data.ratio:.2f}", style="vortex.dim")
    if state.get("show_caps") and data.has_caps:
        f.append(f" · caps {sorted(uid for uid, _p, _c in data.caps)}", style="vortex.warn")
    f.append("\n", style="vortex.dim")
    caps_hint = " · c caps" if data.has_caps else ""
    f.append(f"←→/hl rotate · ↑↓/jk tilt · +/- zoom · t dome⇄bulge{caps_hint} · r reset · q quit",
             style="vortex.dim")
    return f


def _to_ansi(text: Text, width: int) -> str:
    buf = StringIO()
    tmp = Console(file=buf, force_terminal=True, color_system="truecolor",
                  theme=THEME, width=max(10, width))
    tmp.print(text, end="", soft_wrap=True)
    return buf.getvalue()


def run_viewer(console, session, mode: str = "dome") -> None:
    """Show the clip-sac preview. Interactive on a TTY, single static frame otherwise."""
    data = build_preview(session)
    if data is None:
        console.print("[vortex.dim]Nothing to preview — run 'clip-sac' first.[/vortex.dim]")
        return
    if mode == "bulge" and not data.has_bulge:
        mode = "dome"

    if not sys.stdout.isatty():
        cols = max(40, min(console.width, 100))
        frame = render_frame(data, mode, az=30, el=20, zoom=1.0, cols=cols, rows=24)
        console.print(frame)
        console.print(_footer({"az": 30, "el": 20, "zoom": 1.0, "mode": mode}, data))
        return

    _run_interactive(data, mode)


def _run_interactive(data: PreviewData, mode: str) -> None:
    from prompt_toolkit import Application
    from prompt_toolkit.application import get_app
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    state = {"az": 30.0, "el": 20.0, "zoom": 1.0, "mode": mode, "show_caps": False}

    def get_text():
        size = get_app().output.get_size()
        cols = max(20, size.columns)
        rows = max(8, size.rows - 2)        # reserve two lines for the footer
        frame = render_frame(data, state["mode"], state["az"], state["el"],
                             state["zoom"], cols, rows, show_caps=state["show_caps"])
        body = frame + Text("\n") + _footer(state, data)
        return ANSI(_to_ansi(body, cols))

    kb = KeyBindings()

    @kb.add("q")
    @kb.add("escape")
    def _(event): event.app.exit()

    @kb.add("left")
    @kb.add("h")
    def _(event): state["az"] -= 15

    @kb.add("right")
    @kb.add("l")
    def _(event): state["az"] += 15

    @kb.add("up")
    @kb.add("k")
    def _(event): state["el"] += 15

    @kb.add("down")
    @kb.add("j")
    def _(event): state["el"] -= 15

    @kb.add("+")
    @kb.add("=")
    def _(event): state["zoom"] *= 1.25

    @kb.add("-")
    def _(event): state["zoom"] = max(0.1, state["zoom"] / 1.25)

    @kb.add("t")
    def _(event):
        if data.has_bulge:
            state["mode"] = "bulge" if state["mode"] == "dome" else "dome"

    @kb.add("c")
    def _(event):
        if data.has_caps:
            state["show_caps"] = not state["show_caps"]

    @kb.add("r")
    def _(event):
        state.update(az=30.0, el=20.0, zoom=1.0)

    app = Application(
        layout=Layout(Window(content=FormattedTextControl(get_text))),
        key_bindings=kb,
        full_screen=True,
    )
    app.run()
