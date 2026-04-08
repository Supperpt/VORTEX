"""Slice viewer widget — 3-plane MPR display with crosshairs.

Three orthogonal planes displayed side by side:
  Axial    (z = const) — (x, y)
  Coronal  (y = const) — (x, z)
  Sagittal (x = const) — (y, z)

Interaction modes:
  Scroll  — advance slice on the hovered plane; click to navigate crosshairs
  Seed    — click to place a 3D seed point (crosshairs update in all planes)
  Measure — two clicks define a line; distance shown in mm

Signals
-------
seed_placed(tuple)                             — (i, j, k) image index
measurement_done(float, tuple, tuple)          — (dist_mm, p1_mm, p2_mm)
slice_hovered(float, float, float, float)      — x_mm, y_mm, z_mm, hu
"""

import logging
import numpy as np

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSizePolicy, QButtonGroup, QDoubleSpinBox,
)
from PyQt5.QtCore import pyqtSignal, Qt

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

log = logging.getLogger(__name__)

# Plane definitions
# slice_axis: numpy axis used as the slice index (0=z, 1=y, 2=x)
# h_ax / v_ax: numpy axes displayed as horizontal / vertical on the canvas
PLANES = [
    {"name": "Axial",    "slice_axis": 0, "h_ax": 2, "v_ax": 1},
    {"name": "Coronal",  "slice_axis": 1, "h_ax": 2, "v_ax": 0},
    {"name": "Sagittal", "slice_axis": 2, "h_ax": 1, "v_ax": 0},
]


class SliceViewerWidget(QWidget):
    seed_placed      = pyqtSignal(tuple)
    measurement_done = pyqtSignal(float, tuple, tuple)
    slice_hovered    = pyqtSignal(float, float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode        = "scroll"
        self._array       = None
        self._spacing     = (1.0, 1.0, 1.0)
        self._sitk_image = None

        # Slice indices [z_idx, y_idx, x_idx]
        self._slice_idx   = [0, 0, 0]
        self._seed_ijk    = None
        self._cursor_ijk  = None
        self._measure_pts = []   # 0, 1 or 2 (i,j,k) tuples

        # Pan/Zoom state
        self._drag_state = None
        self._drag_start_pos = None
        self._drag_start_lims = None

        # Window/level
        self._wl   = 175.0
        self._ww   = 400.0
        self._vmin = -25.0
        self._vmax = 375.0

        self._img_artists = [None, None, None]
        self._hline_art   = [None, None, None]
        self._vline_art   = [None, None, None]
        self._seed_art    = [None, None, None]
        self._meas_art    = [[], [], []]

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(4)

        # ---- Toolbar ----
        tb = QHBoxLayout()
        tb.setSpacing(4)

        self._mode_btns = {}
        self._mode_group = QButtonGroup(self)
        for label, mode in [("Scroll", "scroll"), ("Pan/Zoom", "panzoom"), ("Seed", "seed"), ("Measure", "measure")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.clicked.connect(lambda _checked, m=mode: self._set_mode(m))
            self._mode_group.addButton(btn)
            self._mode_btns[mode] = btn
            tb.addWidget(btn)

        self._mode_btns["scroll"].setChecked(True)

        btn_reset = QPushButton("Reset View")
        btn_reset.setFixedHeight(24)
        btn_reset.clicked.connect(self._reset_views)
        tb.addWidget(btn_reset)

        tb.addSpacing(12)
        tb.addWidget(QLabel("WL:"))
        self._wl_spin = QDoubleSpinBox()
        self._wl_spin.setRange(-1000, 3000)
        self._wl_spin.setSingleStep(10)
        self._wl_spin.setValue(self._wl)
        self._wl_spin.setFixedWidth(72)
        self._wl_spin.valueChanged.connect(self._on_wl_changed)
        tb.addWidget(self._wl_spin)

        tb.addWidget(QLabel("WW:"))
        self._ww_spin = QDoubleSpinBox()
        self._ww_spin.setRange(1, 5000)
        self._ww_spin.setSingleStep(10)
        self._ww_spin.setValue(self._ww)
        self._ww_spin.setFixedWidth(72)
        self._ww_spin.valueChanged.connect(self._on_ww_changed)
        tb.addWidget(self._ww_spin)

        tb.addStretch()
        self._info_label = QLabel()
        self._info_label.setStyleSheet("color: #ffdd44;")
        tb.addWidget(self._info_label)
        root.addLayout(tb)

        # ---- Three canvases ----
        row = QHBoxLayout()
        row.setSpacing(2)
        self._canvases = []
        self._figs     = []
        self._axes     = []

        for idx, plane in enumerate(PLANES):
            fig = Figure(figsize=(4, 4), tight_layout=True)
            fig.patch.set_facecolor("#111111")
            ax  = fig.add_subplot(111)
            ax.set_facecolor("#000000")
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_title(plane["name"], color="#888888", fontsize=9, pad=2)

            canvas = FigureCanvas(fig)
            canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            canvas.setMinimumSize(150, 150)
            canvas._plane_idx = idx

            canvas.mpl_connect("scroll_event",        self._on_scroll)
            canvas.mpl_connect("button_press_event",  self._on_click)
            canvas.mpl_connect("button_release_event", self._on_release)
            canvas.mpl_connect("motion_notify_event", self._on_motion)

            self._figs.append(fig)
            self._axes.append(ax)
            self._canvases.append(canvas)
            row.addWidget(canvas)

        root.addLayout(row, stretch=1)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        for i, ax in enumerate(self._axes):
            ax.cla()
            ax.set_facecolor("#000000")
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_title(PLANES[i]["name"], color="#888888", fontsize=9, pad=2)
            if i == 1:
                ax.text(0.5, 0.5, "Open a DICOM folder\nto view slices",
                        ha="center", va="center", color="#444444",
                        transform=ax.transAxes, fontsize=10)
        for c in self._canvases:
            c.draw_idle()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_image(self, sitk_image, array: np.ndarray) -> None:
        self._sitk_image = sitk_image
        self._array      = array.astype(np.float32)
        self._spacing    = sitk_image.GetSpacing()
        nz, ny, nx       = array.shape

        self._slice_idx   = [nz // 2, ny // 2, nx // 2]
        self._cursor_ijk  = (nx // 2, ny // 2, nz // 2)
        self._seed_ijk    = None
        self._measure_pts = []
        self._img_artists = [None, None, None]
        self._hline_art   = [None, None, None]
        self._vline_art   = [None, None, None]
        self._seed_art    = [None, None, None]
        self._meas_art    = [[], [], []]

        flat = self._array[self._array > -999]
        if len(flat):
            self._wl = float(np.percentile(flat, 50))
            self._ww = float(np.percentile(flat, 98) - np.percentile(flat, 2))
            self._ww = max(100.0, self._ww)

        self._wl_spin.blockSignals(True)
        self._ww_spin.blockSignals(True)
        self._wl_spin.setValue(self._wl)
        self._ww_spin.setValue(self._ww)
        self._wl_spin.blockSignals(False)
        self._ww_spin.blockSignals(False)
        self._update_vmin_vmax()
        self._render_all()

    def get_seed_ijk(self):
        return self._seed_ijk

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_all(self) -> None:
        for i in range(3):
            self._render_plane(i)

    def _render_plane(self, pi: int) -> None:
        if self._array is None:
            return

        p      = PLANES[pi]
        ax     = self._axes[pi]
        canvas = self._canvases[pi]
        sl_ax  = p["slice_axis"]
        h_ax   = p["h_ax"]
        v_ax   = p["v_ax"]
        idx    = self._slice_idx[sl_ax]
        nz, ny, nx = self._array.shape
        sizes  = [nz, ny, nx]

        # Extract 2D slice
        if sl_ax == 0:
            data = self._array[idx, :, :]
        elif sl_ax == 1:
            data = self._array[:, idx, :][::-1, :]
        else:
            data = self._array[:, :, idx][::-1, :]

        # Pixel aspect ratio
        sx, sy, sz = self._spacing
        aspect_map = {0: sy / sx, 1: sz / sx, 2: sz / sy}
        aspect = aspect_map[sl_ax]

        if self._img_artists[pi] is None:
            ax.cla()
            ax.set_facecolor("#000000")
            ax.set_xticks([])
            ax.set_yticks([])
            im = ax.imshow(
                data, cmap="gray", vmin=self._vmin, vmax=self._vmax,
                origin="upper", aspect=aspect, interpolation="nearest",
            )
            self._img_artists[pi] = im
            self._hline_art[pi]   = None
            self._vline_art[pi]   = None
        else:
            self._img_artists[pi].set_data(data)
            self._img_artists[pi].set_clim(vmin=self._vmin, vmax=self._vmax)
            ax.set_aspect(aspect)

        # Crosshair
        if self._cursor_ijk is not None:
            ci, cj, ck = self._cursor_ijk
            axes_vals = [ck, cj, ci]   # [z, y, x]
            h_pos = axes_vals[h_ax]
            vr    = axes_vals[v_ax]
            v_pos = (sizes[v_ax] - 1 - vr) if sl_ax in (1, 2) else vr

            if self._hline_art[pi] is not None:
                self._hline_art[pi].set_ydata([v_pos, v_pos])
            else:
                self._hline_art[pi] = ax.axhline(
                    v_pos, color="#00ccff", lw=0.8, alpha=0.6, ls="--"
                )
            if self._vline_art[pi] is not None:
                self._vline_art[pi].set_xdata([h_pos, h_pos])
            else:
                self._vline_art[pi] = ax.axvline(
                    h_pos, color="#00ccff", lw=0.8, alpha=0.6, ls="--"
                )

        # Seed marker
        if self._seed_art[pi] is not None:
            try:
                self._seed_art[pi].remove()
            except Exception:
                pass
            self._seed_art[pi] = None

        if self._seed_ijk is not None:
            si, sj, sk = self._seed_ijk
            sax = [sk, sj, si]
            sh  = sax[h_ax]
            svr = sax[v_ax]
            sv  = (sizes[v_ax] - 1 - svr) if sl_ax in (1, 2) else svr
            if abs(sax[sl_ax] - idx) <= 1:
                (self._seed_art[pi],) = ax.plot(
                    sh, sv, "r+", ms=16, mew=2.5, zorder=10
                )

        # Measure points/line
        for art in self._meas_art[pi]:
            try:
                art.remove()
            except Exception:
                pass
        self._meas_art[pi] = []

        for pt in self._measure_pts:
            pax = [pt[2], pt[1], pt[0]]
            ph  = pax[h_ax]
            pvr = pax[v_ax]
            pv  = (sizes[v_ax] - 1 - pvr) if sl_ax in (1, 2) else pvr
            (dot,) = ax.plot(ph, pv, "yo", ms=6, zorder=11)
            self._meas_art[pi].append(dot)

        if len(self._measure_pts) == 2:
            p1, p2 = self._measure_pts
            p1ax, p2ax = [p1[2], p1[1], p1[0]], [p2[2], p2[1], p2[0]]
            ph1, ph2 = p1ax[h_ax], p2ax[h_ax]
            pv1r, pv2r = p1ax[v_ax], p2ax[v_ax]
            if sl_ax in (1, 2):
                s = sizes[v_ax] - 1
                pv1, pv2 = s - pv1r, s - pv2r
            else:
                pv1, pv2 = pv1r, pv2r
            (ln,) = ax.plot([ph1, ph2], [pv1, pv2], "y-", lw=1.2, zorder=10)
            self._meas_art[pi].append(ln)

        ax.set_title(
            f"{p['name']}  {idx+1}/{sizes[sl_ax]}",
            color="#888888", fontsize=9, pad=2,
        )
        canvas.draw_idle()

    # ------------------------------------------------------------------
    # Window/level
    # ------------------------------------------------------------------

    def _update_vmin_vmax(self) -> None:
        self._vmin = self._wl - self._ww / 2
        self._vmax = self._wl + self._ww / 2

    def _on_wl_changed(self, v: float) -> None:
        self._wl = v
        self._update_vmin_vmax()
        if self._array is not None:
            self._render_all()

    def _on_ww_changed(self, v: float) -> None:
        self._ww = max(1.0, v)
        self._update_vmin_vmax()
        if self._array is not None:
            self._render_all()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _display_to_ijk(self, pi: int, col, row):
        if self._array is None or col is None or row is None:
            return None
        p      = PLANES[pi]
        sl_ax  = p["slice_axis"]
        h_ax   = p["h_ax"]
        v_ax   = p["v_ax"]
        nz, ny, nx = self._array.shape
        sizes  = [nz, ny, nx]

        col = int(round(col))
        row = int(round(row))
        v_idx = (sizes[v_ax] - 1 - row) if sl_ax in (1, 2) else row
        h_idx = col

        axes_idx = list(self._slice_idx)   # [z, y, x]
        axes_idx[h_ax] = h_idx
        axes_idx[v_ax] = v_idx
        zz, yy, xx = axes_idx

        if not (0 <= xx < nx and 0 <= yy < ny and 0 <= zz < nz):
            return None
        return (xx, yy, zz)   # (i=x, j=y, k=z)

    def _ijk_to_mm(self, ijk: tuple) -> tuple:
        if self._sitk_image:
            return self._sitk_image.TransformIndexToPhysicalPoint(
                (int(ijk[0]), int(ijk[1]), int(ijk[2]))
            )
        sx, sy, sz = self._spacing
        return (ijk[0] * sx, ijk[1] * sy, ijk[2] * sz)

    def _hu_at(self, ijk: tuple) -> float:
        i, j, k = ijk
        nz, ny, nx = self._array.shape
        if 0 <= i < nx and 0 <= j < ny and 0 <= k < nz:
            return float(self._array[k, j, i])
        return 0.0

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _reset_views(self):
        for pi, ax in enumerate(self._axes):
            if self._array is None:
                continue
            sl_ax = PLANES[pi]["slice_axis"]
            v_ax = PLANES[pi]["v_ax"]
            h_ax = PLANES[pi]["h_ax"]
            sizes = self._array.shape # z, y, x
            
            # Reset limits to original image dimensions
            ax.set_xlim(-0.5, sizes[h_ax] - 0.5)
            ax.set_ylim(sizes[v_ax] - 0.5, -0.5) # Because origin="upper"
            
            self._canvases[pi].draw_idle()

    def _on_scroll(self, event) -> None:
        if self._array is None:
            return
        pi   = event.canvas._plane_idx
        sl_ax = PLANES[pi]["slice_axis"]
        nz, ny, nx = self._array.shape
        sizes = [nz, ny, nx]
        delta = 1 if event.button == "up" else -1
        self._slice_idx[sl_ax] = max(
            0, min(self._slice_idx[sl_ax] + delta, sizes[sl_ax] - 1)
        )
        self._render_plane(pi)

    def _on_click(self, event) -> None:
        if self._array is None or event.inaxes is None:
            return
            
        # Handle Pan/Zoom initiation
        if self._mode == "panzoom":
            if event.button == 1:
                self._drag_state = "pan"
            elif event.button == 3:
                self._drag_state = "zoom"
            else:
                return
            self._drag_start_pos = (event.x, event.y)
            self._drag_start_lims = (event.inaxes.get_xlim(), event.inaxes.get_ylim())
            return
            
        # If right click in ANY mode, also allow zoom
        if event.button == 3:
            self._drag_state = "zoom"
            self._drag_start_pos = (event.x, event.y)
            self._drag_start_lims = (event.inaxes.get_xlim(), event.inaxes.get_ylim())
            return
            
        # Only left click proceeds to scroll/seed/measure
        if event.button != 1:
            return
            
        pi  = event.canvas._plane_idx
        ijk = self._display_to_ijk(pi, event.xdata, event.ydata)
        if ijk is None:
            return

        if self._mode in ("scroll", "seed"):
            i, j, k = ijk
            self._cursor_ijk = ijk
            self._slice_idx  = [k, j, i]

            if self._mode == "seed":
                self._seed_ijk = ijk
                log.info("Seed: ijk=%s", ijk)
                self.seed_placed.emit(ijk)

            self._render_all()
            mm = self._ijk_to_mm(ijk)
            self.slice_hovered.emit(mm[0], mm[1], mm[2], self._hu_at(ijk))

        elif self._mode == "measure":
            self._measure_pts.append(ijk)
            if len(self._measure_pts) > 2:
                self._measure_pts = [ijk]

            self._render_all()

            if len(self._measure_pts) == 2:
                p1_mm = self._ijk_to_mm(self._measure_pts[0])
                p2_mm = self._ijk_to_mm(self._measure_pts[1])
                from vortex.pipeline.measurement import measure_line
                dist  = measure_line(p1_mm, p2_mm)
                self._info_label.setText(f"  {dist:.2f} mm")
                self.measurement_done.emit(dist, p1_mm, p2_mm)

    def _on_release(self, event) -> None:
        self._drag_state = None

    def _on_motion(self, event) -> None:
        if self._array is None or event.inaxes is None:
            return
            
        if self._drag_state is not None:
            dx = event.x - self._drag_start_pos[0]
            dy = event.y - self._drag_start_pos[1]
            ax = event.inaxes
            xlim = self._drag_start_lims[0]
            ylim = self._drag_start_lims[1]
            
            if self._drag_state == "pan":
                inv = ax.transData.inverted()
                p0 = inv.transform(self._drag_start_pos)
                p1 = inv.transform((event.x, event.y))
                ddx = p1[0] - p0[0]
                ddy = p1[1] - p0[1]
                ax.set_xlim(xlim[0] - ddx, xlim[1] - ddx)
                ax.set_ylim(ylim[0] - ddy, ylim[1] - ddy)
                event.canvas.draw_idle()
                
            elif self._drag_state == "zoom":
                # dy determines zoom factor (pull down to zoom out, push up to zoom in)
                scale = 1.0 - dy * 0.005
                scale = max(0.01, min(scale, 10.0))
                
                # Zoom around the center of the drag start point
                inv = ax.transData.inverted()
                p0 = inv.transform(self._drag_start_pos)
                cx, cy = p0[0], p0[1]
                
                new_xlim = [cx + (xlim[0] - cx) * scale, cx + (xlim[1] - cx) * scale]
                new_ylim = [cy + (ylim[0] - cy) * scale, cy + (ylim[1] - cy) * scale]
                
                ax.set_xlim(new_xlim)
                ax.set_ylim(new_ylim)
                event.canvas.draw_idle()
            return

        pi  = event.canvas._plane_idx
        ijk = self._display_to_ijk(pi, event.xdata, event.ydata)
        if ijk is None:
            return
        mm = self._ijk_to_mm(ijk)
        self.slice_hovered.emit(mm[0], mm[1], mm[2], self._hu_at(ijk))

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        for m, btn in self._mode_btns.items():
            btn.setChecked(m == mode)

        cursors = {"scroll": Qt.ArrowCursor, "panzoom": Qt.OpenHandCursor, "seed": Qt.CrossCursor, "measure": Qt.SizeHorCursor}
        for c in self._canvases:
            c.setCursor(cursors.get(mode, Qt.ArrowCursor))

        if mode == "measure":
            self._measure_pts = []
            self._info_label.setText("  Click two points to measure")
            self._render_all()
        elif mode == "panzoom":
            self._info_label.setText("  Left-drag to Pan | Right-drag to Zoom")
            self._render_all()
        else:
            self._info_label.setText("")
