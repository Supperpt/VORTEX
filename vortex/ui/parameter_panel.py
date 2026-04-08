"""Left-panel parameter controls, wired to PipelineParams."""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QGroupBox, QFormLayout,
    QDoubleSpinBox, QSpinBox, QCheckBox, QLabel,
)
from PyQt5.QtCore import pyqtSignal

from vortex.state.app_state import PipelineParams


class ParameterPanel(QWidget):
    """Editable parameter controls.

    Emits `params_changed(PipelineParams)` whenever any control changes.
    """

    params_changed = pyqtSignal(object)  # PipelineParams

    def __init__(self, parent=None):
        super().__init__(parent)
        self._building = False
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # --- Segmentation group ---
        seg_group = QGroupBox("Segmentation")
        seg_form  = QFormLayout(seg_group)
        seg_form.setSpacing(4)

        self._lower    = self._make_dspin(min_=-1000, max_=3000, step=10, decimals=0, val=150)
        self._upper    = self._make_dspin(min_=-1000, max_=3000, step=10, decimals=0, val=400)
        self._roi      = self._make_dspin(min_=1, max_=200, step=1, decimals=1, val=20)
        self._resample = self._make_dspin(min_=1, max_=4, step=0.5, decimals=1, val=2)

        self._use_levelset = QCheckBox()
        self._use_levelset.toggled.connect(self._on_levelset_toggled)

        self._ls_iters = QSpinBox()
        self._ls_iters.setRange(100, 2000)
        self._ls_iters.setSingleStep(100)
        self._ls_iters.setValue(500)
        self._ls_iters.setEnabled(False)
        self._ls_iters.valueChanged.connect(self._on_change)

        self._ls_curve = self._make_dspin(min_=0, max_=2, step=0.1, decimals=2, val=0.5)
        self._ls_curve.setEnabled(False)

        seg_form.addRow("HU min:", self._lower)
        seg_form.addRow("HU max:", self._upper)
        seg_form.addRow("ROI radius (mm):", self._roi)
        seg_form.addRow("Resample ×:", self._resample)
        seg_form.addRow("Level-set refine:", self._use_levelset)
        seg_form.addRow("LS iterations:", self._ls_iters)
        seg_form.addRow("LS curvature:", self._ls_curve)

        # --- Mesh group ---
        mesh_group = QGroupBox("Mesh")
        mesh_form  = QFormLayout(mesh_group)
        mesh_form.setSpacing(4)

        self._reduce   = self._make_dspin(min_=0, max_=0.99, step=0.05, decimals=2, val=0)
        self._increase = QSpinBox()
        self._increase.setRange(0, 4)
        self._increase.setValue(0)
        self._increase.valueChanged.connect(self._on_change)

        mesh_form.addRow("Decimate (0–1):", self._reduce)
        mesh_form.addRow("Subdivide passes:", self._increase)

        # --- Output mode group ---
        out_group = QGroupBox("Output Mode")
        out_form  = QFormLayout(out_group)
        out_form.setSpacing(4)

        self._build_wall    = QCheckBox()
        self._wall_thick    = self._make_dspin(min_=0.1, max_=5, step=0.1, decimals=2, val=0.2)
        self._solid         = QCheckBox()

        self._build_wall.toggled.connect(self._on_wall_toggled)
        self._solid.toggled.connect(self._on_change)

        self._wall_thick.setEnabled(False)

        out_form.addRow("Build wall (FSI):", self._build_wall)
        out_form.addRow("Wall thickness (mm):", self._wall_thick)
        out_form.addRow("Solid (3D print):", self._solid)

        layout.addWidget(seg_group)
        layout.addWidget(mesh_group)
        layout.addWidget(out_group)
        layout.addStretch()

    def _make_dspin(self, min_, max_, step, decimals, val) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(min_, max_)
        w.setSingleStep(step)
        w.setDecimals(decimals)
        w.setValue(val)
        w.valueChanged.connect(self._on_change)
        return w

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_levelset_toggled(self, checked: bool) -> None:
        self._ls_iters.setEnabled(checked)
        self._ls_curve.setEnabled(checked)
        self._on_change()

    def _on_wall_toggled(self, checked: bool) -> None:
        self._wall_thick.setEnabled(checked)
        self._on_change()

    def _on_change(self, *_) -> None:
        if not self._building:
            self.params_changed.emit(self.get_params())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_params(self) -> PipelineParams:
        return PipelineParams(
            lower_threshold=self._lower.value(),
            upper_threshold=self._upper.value(),
            roi_radius=self._roi.value(),
            resample=self._resample.value(),
            use_levelset=self._use_levelset.isChecked(),
            levelset_iterations=self._ls_iters.value(),
            levelset_curvature=self._ls_curve.value(),
            reduce_mesh=self._reduce.value(),
            increase_mesh=self._increase.value(),
            build_wall=self._build_wall.isChecked(),
            wall_thickness=self._wall_thick.value(),
            solid=self._solid.isChecked(),
        )

    def set_params(self, params: PipelineParams) -> None:
        self._building = True
        self._lower.setValue(params.lower_threshold)
        self._upper.setValue(params.upper_threshold)
        self._roi.setValue(params.roi_radius)
        self._resample.setValue(params.resample)
        self._use_levelset.setChecked(params.use_levelset)
        self._ls_iters.setValue(params.levelset_iterations)
        self._ls_iters.setEnabled(params.use_levelset)
        self._ls_curve.setValue(params.levelset_curvature)
        self._ls_curve.setEnabled(params.use_levelset)
        self._reduce.setValue(params.reduce_mesh)
        self._increase.setValue(params.increase_mesh)
        self._build_wall.setChecked(params.build_wall)
        self._wall_thick.setValue(params.wall_thickness)
        self._wall_thick.setEnabled(params.build_wall)
        self._solid.setChecked(params.solid)
        self._building = False
