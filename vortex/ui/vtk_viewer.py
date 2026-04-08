"""3D mesh preview widget.

Tries QVTKRenderWindowInteractor for native VTK interaction first.
Falls back to a QOpenGLWidget + vtkGenericOpenGLRenderWindow if the import
fails.  This avoids vtkEGLRenderWindow (the default in vmtk's VTK build)
which crashes when no EGL display is present even though X11/GLX is fine.

Public API
----------
set_surface(poly_data)        — replace displayed surface mesh
set_centerlines(poly_data)    — overlay centerline tubes
set_capped_surface(poly_data) — replace with post-flow-ext surface
reset_camera()                — fit scene to view
clear()                       — remove all actors
"""

import importlib
import logging

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QSizePolicy, QHBoxLayout, QPushButton,
    QOpenGLWidget,
)
from PyQt5.QtCore import Qt, QPoint, QTimer

from vortex.utils.vtk_compat import vtk, vtk_np

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# We always use the QOpenGLWidget + vtkGenericOpenGLRenderWindow path.
# QVTKRenderWindowInteractor may be importable but its GetRenderWindow()
# creates vtkEGLRenderWindow in vmtk's VTK build, which crashes when no EGL
# display is present even though X11/GLX is available via Qt.
# _VtkGLWidget below provides equivalent rotate/zoom/pan interaction.
# ---------------------------------------------------------------------------
_INTERACTIVE = False
_QVTKClass   = None
log.info("Using QOpenGLWidget + vtkGenericOpenGLRenderWindow renderer.")

# ---------------------------------------------------------------------------
# Import vtkGenericOpenGLRenderWindow for the fallback path.
# This render window has no GL context of its own; it renders into a
# Qt-managed OpenGL context (QOpenGLWidget), which avoids the EGL path
# entirely.
# ---------------------------------------------------------------------------
try:
    from vtkmodules.vtkRenderingOpenGL2 import vtkGenericOpenGLRenderWindow as _GenericRW
    log.info("vtkGenericOpenGLRenderWindow available.")
except ImportError:
    _GenericRW = None
    log.warning("vtkGenericOpenGLRenderWindow not found — 3D viewer may not work.")


# ---------------------------------------------------------------------------
# QOpenGLWidget that drives VTK via vtkGenericOpenGLRenderWindow
# ---------------------------------------------------------------------------

class _VtkGLWidget(QOpenGLWidget):
    """QOpenGLWidget that lets VTK render into Qt's OpenGL context."""

    def __init__(self, renderer, parent=None):
        super().__init__(parent)
        self._renderer = renderer
        self._rotating = False
        self._zooming  = False
        self._last_pos = QPoint()

        if _GenericRW is not None:
            self._rw = _GenericRW()
        else:
            self._rw = vtk.vtkRenderWindow()
        self._rw.AddRenderer(renderer)

        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(80, 80)

    def get_render_window(self):
        return self._rw

    # ------------------------------------------------------------------
    # OpenGL lifecycle
    # ------------------------------------------------------------------

    def initializeGL(self):
        w, h = max(self.width(), 1), max(self.height(), 1)
        self._rw.SetSize(w, h)
        self._rw.InitializeFromCurrentContext()

    def resizeGL(self, w, h):
        self._rw.SetSize(max(w, 1), max(h, 1))
        self._rw.Modified()
        self.update()

    def paintGL(self):
        self._rw.Render()

    # ------------------------------------------------------------------
    # Mouse / keyboard
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        self._last_pos = event.pos()
        self._rotating = event.button() == Qt.LeftButton
        self._zooming  = event.button() == Qt.RightButton

    def mouseMoveEvent(self, event):
        dx = event.x() - self._last_pos.x()
        dy = event.y() - self._last_pos.y()
        self._last_pos = event.pos()
        cam = self._renderer.GetActiveCamera()
        if self._rotating:
            cam.Azimuth(-dx * 0.5)
            cam.Elevation(dy * 0.5)
            cam.OrthogonalizeViewUp()
        elif self._zooming:
            factor = 1.0 + dy * 0.006
            fp  = list(cam.GetFocalPoint())
            pos = list(cam.GetPosition())
            cam.SetPosition(*[fp[i] + (pos[i] - fp[i]) * factor for i in range(3)])
        self.update()

    def mouseReleaseEvent(self, event):
        self._rotating = self._zooming = False
        self.update()

    def wheelEvent(self, event):
        factor = 0.88 if event.angleDelta().y() > 0 else 1.14
        cam = self._renderer.GetActiveCamera()
        fp  = list(cam.GetFocalPoint())
        pos = list(cam.GetPosition())
        cam.SetPosition(*[fp[i] + (pos[i] - fp[i]) * factor for i in range(3)])
        self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_R:
            self._renderer.ResetCamera()
            self.update()


# ---------------------------------------------------------------------------
# Public viewer widget
# ---------------------------------------------------------------------------

class VtkViewerWidget(QWidget):
    """3D surface viewer with interactive or QOpenGLWidget-based rendering."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._surface_actor    = None
        self._centerline_actor = None

        self._build_vtk()
        if _INTERACTIVE:
            self._build_interactive_ui()
        else:
            self._build_gl_widget_ui()

    # ------------------------------------------------------------------
    # VTK renderer setup (shared)
    # ------------------------------------------------------------------

    def _build_vtk(self) -> None:
        self._renderer = vtk.vtkRenderer()
        self._renderer.SetBackground(0.10, 0.10, 0.12)
        self._renderer.GradientBackgroundOn()
        self._renderer.SetBackground2(0.03, 0.03, 0.05)

        self._renderer.RemoveAllLights()
        headlight = vtk.vtkLight()
        headlight.SetLightTypeToHeadlight()
        headlight.SetIntensity(0.85)
        self._renderer.AddLight(headlight)

        fill = vtk.vtkLight()
        fill.SetLightTypeToSceneLight()
        fill.SetPosition(-1, -1, 0)
        fill.SetIntensity(0.25)
        fill.SetAmbientColor(0.8, 0.8, 1.0)
        self._renderer.AddLight(fill)

    # ------------------------------------------------------------------
    # Interactive UI (QVTKRenderWindowInteractor)
    # ------------------------------------------------------------------

    def _build_interactive_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        tb = QHBoxLayout()
        btn = QPushButton("Reset View")
        btn.setFixedHeight(22)
        btn.clicked.connect(self.reset_camera)
        tb.addWidget(btn)
        tb.addStretch()
        tb.addWidget(QLabel("Drag·rotate  RDrag·zoom  MMB·pan"))
        layout.addLayout(tb)

        self._vtk_widget = _QVTKClass(self)
        self._vtk_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._render_window = self._vtk_widget.GetRenderWindow()
        self._render_window.AddRenderer(self._renderer)

        style = vtk.vtkInteractorStyleTrackballCamera()
        self._vtk_widget.SetInteractorStyle(style)
        self._vtk_widget.Initialize()

        layout.addWidget(self._vtk_widget, stretch=1)

    # ------------------------------------------------------------------
    # QOpenGLWidget UI (fallback — no EGL)
    # ------------------------------------------------------------------

    def _build_gl_widget_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        tb = QHBoxLayout()
        btn = QPushButton("Reset View (R)")
        btn.setFixedHeight(22)
        btn.clicked.connect(self.reset_camera)
        tb.addWidget(btn)
        tb.addStretch()
        tb.addWidget(QLabel("LDrag·rotate  RDrag/Scroll·zoom  R·reset"))
        layout.addLayout(tb)

        self._gl_widget = _VtkGLWidget(self._renderer, self)
        self._render_window = self._gl_widget.get_render_window()
        layout.addWidget(self._gl_widget, stretch=1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_surface(self, poly_data: "vtk.vtkPolyData") -> None:
        if self._surface_actor:
            self._renderer.RemoveActor(self._surface_actor)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly_data)
        mapper.ScalarVisibilityOff()

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        p = actor.GetProperty()
        p.SetColor(0.85, 0.50, 0.35)
        p.SetAmbient(0.12)
        p.SetDiffuse(0.75)
        p.SetSpecular(0.35)
        p.SetSpecularPower(40)

        self._surface_actor = actor
        self._renderer.AddActor(actor)
        self._renderer.ResetCamera()
        self._render()

    def set_centerlines(self, poly_data: "vtk.vtkPolyData") -> None:
        if self._centerline_actor:
            self._renderer.RemoveActor(self._centerline_actor)

        tube = vtk.vtkTubeFilter()
        tube.SetInputData(poly_data)
        tube.SetRadius(0.25)
        tube.SetNumberOfSides(10)
        tube.Update()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(tube.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(0.20, 0.85, 0.30)

        self._centerline_actor = actor
        self._renderer.AddActor(actor)
        self._render()

    def set_capped_surface(self, poly_data: "vtk.vtkPolyData") -> None:
        """Replace the surface with the post-flow-extension capped mesh."""
        self.set_surface(poly_data)

    def reset_camera(self) -> None:
        self._renderer.ResetCamera()
        self._render()

    def clear(self) -> None:
        self._renderer.RemoveAllActors()
        self._surface_actor    = None
        self._centerline_actor = None
        self._render()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> None:
        if _INTERACTIVE:
            self._render_window.Render()
        elif hasattr(self, "_gl_widget"):
            self._gl_widget.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
