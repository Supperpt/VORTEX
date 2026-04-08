"""Main application window — VORTEX Aneurysm.

Layout (horizontal QSplitter, ratio ~1:3:1):
  LEFT    — DicomPanel + ParameterPanel + Run button
  CENTER  — QTabWidget (SliceViewerWidget | VtkViewerWidget)
  RIGHT   — Pipeline status + action buttons + measurements
"""

import logging
import os

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout,
    QPushButton, QGroupBox, QLabel, QTabWidget, QFileDialog,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
)
from PyQt5.QtCore import Qt, QThread, pyqtSlot

from vortex.state.app_state import AppState
from vortex.ui.dicom_panel       import DicomPanel
from vortex.ui.parameter_panel   import ParameterPanel
from vortex.ui.slice_viewer      import SliceViewerWidget
from vortex.ui.vtk_viewer        import VtkViewerWidget
from vortex.ui.status_bar        import StatusBar
from vortex.ui.flow_ext_dialog   import FlowExtDialog

from vortex.workers.load_worker       import LoadWorker
from vortex.workers.segment_worker    import SegmentWorker
from vortex.workers.mesh_worker       import MeshWorker
from vortex.workers.export_worker     import ExportWorker
from vortex.workers.centerline_worker import CenterlineWorker
from vortex.workers.flow_ext_worker   import FlowExtWorker

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VORTEX Aneurysm")
        self.resize(1500, 950)

        self._state   = AppState()
        self._threads = {}
        self._workers = {}

        self._build_ui()
        self._build_menu()
        self._update_action_states()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._status = StatusBar()
        self.setStatusBar(self._status)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)
        self.setCentralWidget(splitter)

        # ---- LEFT PANEL ----
        left   = QWidget()
        left.setFixedWidth(310)
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(4, 4, 4, 4)
        left_l.setSpacing(6)

        self._dicom_panel = DicomPanel()
        self._dicom_panel.series_selected.connect(self._on_series_selected)

        self._param_panel = ParameterPanel()
        self._param_panel.params_changed.connect(self._on_params_changed)

        self._run_btn = QPushButton("Run Segmentation")
        self._run_btn.setEnabled(False)
        self._run_btn.setFixedHeight(36)
        self._run_btn.setStyleSheet(
            "QPushButton{background:#1e6fc4;color:white;font-weight:bold;"
            "border-radius:4px;font-size:13px;}"
            "QPushButton:disabled{background:#333;color:#666;}"
            "QPushButton:hover{background:#2a7fd4;}"
        )
        self._run_btn.clicked.connect(self._on_run_segmentation)

        left_l.addWidget(self._dicom_panel)
        left_l.addWidget(self._param_panel)
        left_l.addWidget(self._run_btn)
        splitter.addWidget(left)

        # ---- CENTER PANEL ----
        self._tabs         = QTabWidget()
        self._slice_viewer = SliceViewerWidget()
        self._vtk_viewer   = VtkViewerWidget()
        self._tabs.addTab(self._slice_viewer, "Slices (MPR)")
        self._tabs.addTab(self._vtk_viewer,   "3D Mesh")
        splitter.addWidget(self._tabs)

        self._slice_viewer.seed_placed.connect(self._on_seed_placed)
        self._slice_viewer.measurement_done.connect(self._on_measurement_done)
        self._slice_viewer.slice_hovered.connect(
            lambda x, y, z, hu: self._status.set_cursor_info(x, y, z, hu)
        )

        # ---- RIGHT PANEL ----
        right   = QWidget()
        right.setFixedWidth(225)
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(4, 4, 4, 4)
        right_l.setSpacing(6)

        # Pipeline status
        pipe_group = QGroupBox("Pipeline")
        pipe_inner = QVBoxLayout(pipe_group)
        pipe_inner.setSpacing(2)
        self._step_labels = {}
        for step in ["DICOM", "Segmentation", "Mesh", "Centerlines", "Flow ext"]:
            row = QHBoxLayout()
            lbl = QLabel(step + ":")
            val = QLabel("—")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(lbl)
            row.addWidget(val, 1)
            pipe_inner.addLayout(row)
            self._step_labels[step] = val
        right_l.addWidget(pipe_group)

        # Actions
        act_group = QGroupBox("Actions")
        act_inner = QVBoxLayout(act_group)
        act_inner.setSpacing(4)

        self._centerline_btn = QPushButton("Compute Centerlines")
        self._centerline_btn.setEnabled(False)
        self._centerline_btn.clicked.connect(self._on_compute_centerlines)

        self._flow_ext_btn = QPushButton("Add Flow Extensions…")
        self._flow_ext_btn.setEnabled(False)
        self._flow_ext_btn.clicked.connect(self._on_add_flow_extensions)

        self._export_btn = QPushButton("Export STL…")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._on_export)

        act_inner.addWidget(self._centerline_btn)
        act_inner.addWidget(self._flow_ext_btn)
        act_inner.addWidget(self._export_btn)
        right_l.addWidget(act_group)

        # Seed info
        seed_group = QGroupBox("Seed Point")
        seed_inner = QVBoxLayout(seed_group)
        self._seed_label = QLabel("Not placed")
        self._seed_label.setWordWrap(True)
        self._seed_label.setStyleSheet("font-size: 10px;")
        seed_inner.addWidget(self._seed_label)
        right_l.addWidget(seed_group)

        # Measurements table
        meas_group = QGroupBox("Measurements")
        meas_inner = QVBoxLayout(meas_group)
        self._meas_table = QTableWidget(0, 2)
        self._meas_table.setHorizontalHeaderLabels(["Label", "mm"])
        self._meas_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._meas_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._meas_table.setMaximumHeight(130)
        self._meas_table.setEditTriggers(QTableWidget.NoEditTriggers)
        clr_btn = QPushButton("Clear")
        clr_btn.setFixedHeight(20)
        clr_btn.clicked.connect(self._meas_table.clearContents)
        clr_btn.clicked.connect(lambda: self._meas_table.setRowCount(0))
        meas_inner.addWidget(self._meas_table)
        meas_inner.addWidget(clr_btn)
        right_l.addWidget(meas_group)

        right_l.addStretch()
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)

    def _build_menu(self) -> None:
        bar = self.menuBar()

        file_menu = bar.addMenu("File")
        file_menu.addAction("Open DICOM Folder…", self._dicom_panel._on_open)
        file_menu.addSeparator()
        file_menu.addAction("Export STL…", self._on_export)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.close)

        seg_menu = bar.addMenu("Segmentation")
        seg_menu.addAction("Run Segmentation", self._on_run_segmentation)

        mesh_menu = bar.addMenu("Mesh")
        mesh_menu.addAction("Compute Centerlines", self._on_compute_centerlines)
        mesh_menu.addAction("Add Flow Extensions…", self._on_add_flow_extensions)

        view_menu = bar.addMenu("View")
        view_menu.addAction("Switch to Slices",    lambda: self._tabs.setCurrentIndex(0))
        view_menu.addAction("Switch to 3D Mesh",   lambda: self._tabs.setCurrentIndex(1))
        view_menu.addAction("Reset 3D Camera",     self._vtk_viewer.reset_camera)

        help_menu = bar.addMenu("Help")
        help_menu.addAction("About VORTEX Aneurysm", self._show_about)

    # ------------------------------------------------------------------
    # DICOM loading
    # ------------------------------------------------------------------

    @pyqtSlot(str, str, str)
    def _on_series_selected(self, folder: str, uid: str, description: str) -> None:
        print(f"[MainWindow] Series selected: {description}")
        log.info("Series selected: %s", description)
        self._state.dicom_folder       = folder
        self._state.series_uid         = uid
        self._state.series_description = description
        self._state.reset_pipeline()
        self._update_step("DICOM", "loading…", "orange")
        self._update_action_states()
        self._meas_table.setRowCount(0)

        print(f"[MainWindow] Starting LoadWorker for folder: {folder}")
        self._start_worker("load", LoadWorker(folder, uid),
                           on_finished=self._on_load_done)

    @pyqtSlot(object)
    def _on_load_done(self, result: dict) -> None:
        print(f"[MainWindow] LoadWorker finished for series: {result.get('series_uid')}")
        self._state.raw_image = result["image"]
        self._slice_viewer.set_image(result["image"], result["array"])
        self._update_step("DICOM", "ready", "green")
        self._run_btn.setEnabled(True)
        self._tabs.setCurrentIndex(0)
        self._status.show_message(
            "DICOM loaded — switch to Seed mode and click the aneurysm, then Run Segmentation."
        )

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def _on_params_changed(self, params) -> None:
        self._state.params = params

    # ------------------------------------------------------------------
    # Seed point
    # ------------------------------------------------------------------

    @pyqtSlot(tuple)
    def _on_seed_placed(self, ijk: tuple) -> None:
        from vortex.pipeline.dicom_loader import ijk_to_mm
        self._state.seed_point_ijk = ijk
        if self._state.raw_image:
            mm = ijk_to_mm(self._state.raw_image, ijk)
            self._state.seed_point_mm = mm
            self._seed_label.setText(
                f"i={ijk[0]}  j={ijk[1]}  k={ijk[2]}\n"
                f"x={mm[0]:.1f}  y={mm[1]:.1f}\n"
                f"z={mm[2]:.1f} mm"
            )
        self._status.show_message(f"Seed placed at {ijk}")

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    @pyqtSlot(float, tuple, tuple)
    def _on_measurement_done(self, dist_mm: float, p1_mm: tuple, p2_mm: tuple) -> None:
        row = self._meas_table.rowCount()
        self._meas_table.insertRow(row)
        self._meas_table.setItem(row, 0, QTableWidgetItem(f"Meas {row + 1}"))
        self._meas_table.setItem(row, 1, QTableWidgetItem(f"{dist_mm:.2f}"))
        self._state.measurements[f"measure_{row+1}"] = {
            "dist_mm": dist_mm, "p1_mm": p1_mm, "p2_mm": p2_mm
        }
        self._status.show_message(f"Measurement: {dist_mm:.2f} mm")

    # ------------------------------------------------------------------
    # Segmentation
    # ------------------------------------------------------------------

    def _on_run_segmentation(self) -> None:
        if not self._state.has_image():
            QMessageBox.warning(self, "No Image", "Load a DICOM series first.")
            return

        params = self._state.params.copy()
        self._update_step("Segmentation", "running…", "orange")
        self._update_step("Mesh",         "—",         "grey")
        self._update_step("Centerlines",  "—",         "grey")
        self._update_step("Flow ext",     "—",         "grey")
        self._run_btn.setEnabled(False)
        self._centerline_btn.setEnabled(False)
        self._flow_ext_btn.setEnabled(False)
        self._export_btn.setEnabled(False)

        self._start_worker("segment", SegmentWorker(self._state.raw_image, params),
                           on_finished=self._on_segment_done)

    @pyqtSlot(object)
    def _on_segment_done(self, vtk_image) -> None:
        self._state.vtk_image = self._state.levelset_image = vtk_image
        self._update_step("Segmentation", "done", "green")
        self._run_btn.setEnabled(True)

        params = self._state.params.copy()
        self._update_step("Mesh", "running…", "orange")
        self._start_worker("mesh", MeshWorker(vtk_image, params),
                           on_finished=self._on_mesh_done)

    @pyqtSlot(object)
    def _on_mesh_done(self, surface) -> None:
        self._state.surface = surface
        self._vtk_viewer.set_surface(surface)
        self._update_step("Mesh", "ready", "green")
        self._centerline_btn.setEnabled(True)
        self._export_btn.setEnabled(True)
        self._tabs.setCurrentIndex(1)
        n = surface.GetNumberOfCells()
        self._status.show_message(
            f"Mesh ready — {n:,} triangles.  "
            "Run Centerlines → Flow Extensions → Export STL."
        )

    # ------------------------------------------------------------------
    # Centerlines
    # ------------------------------------------------------------------

    def _on_compute_centerlines(self) -> None:
        if not self._state.has_surface():
            QMessageBox.warning(self, "No Mesh", "Generate a mesh first.")
            return

        self._update_step("Centerlines", "running…", "orange")
        self._centerline_btn.setEnabled(False)
        self._flow_ext_btn.setEnabled(False)

        self._start_worker("centerlines", CenterlineWorker(self._state.surface),
                           on_finished=self._on_centerlines_done)

    @pyqtSlot(object)
    def _on_centerlines_done(self, result: dict) -> None:
        self._state.centerlines       = result["centerlines"]
        self._state.boundary_profiles = result["profiles"]
        self._vtk_viewer.set_centerlines(result["centerlines"])
        n = len(result["profiles"])
        self._update_step("Centerlines", f"{n} profiles", "green")
        self._centerline_btn.setEnabled(True)
        self._flow_ext_btn.setEnabled(True)
        self._status.show_message(
            f"Centerlines computed — {n} boundary profiles. "
            "Click 'Add Flow Extensions…' to continue."
        )

    # ------------------------------------------------------------------
    # Flow extensions
    # ------------------------------------------------------------------

    def _on_add_flow_extensions(self) -> None:
        if not self._state.has_centerlines():
            QMessageBox.warning(self, "No Centerlines",
                                "Compute centerlines first.")
            return

        dlg = FlowExtDialog(
            self._state.boundary_profiles,
            default_ratio=self._state.params.flow_ext_ratio,
            parent=self,
        )
        if dlg.exec_() != FlowExtDialog.Accepted:
            return

        selected_ids = dlg.selected_profile_ids()
        if not selected_ids:
            QMessageBox.information(self, "No Profiles", "No profiles selected.")
            return

        params = self._state.params.copy()
        params.flow_ext_ratio    = dlg.extension_ratio()
        params.flow_ext_selected = selected_ids

        self._update_step("Flow ext", "running…", "orange")
        self._flow_ext_btn.setEnabled(False)
        self._export_btn.setEnabled(False)

        self._start_worker(
            "flow_ext",
            FlowExtWorker(self._state.surface, self._state.centerlines, params),
            on_finished=self._on_flow_ext_done,
        )

    @pyqtSlot(object)
    def _on_flow_ext_done(self, capped) -> None:
        self._state.capped_surface = capped
        self._vtk_viewer.set_capped_surface(capped)
        self._update_step("Flow ext", "done", "green")
        self._flow_ext_btn.setEnabled(True)
        self._export_btn.setEnabled(True)
        n = capped.GetNumberOfCells()
        self._status.show_message(
            f"Flow extensions applied — {n:,} triangles. "
            "Model is watertight. Export STL when ready."
        )

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export(self) -> None:
        surface = self._state.capped_surface or self._state.surface
        if surface is None:
            QMessageBox.warning(self, "No Mesh", "Generate a mesh first.")
            return

        if self._state.capped_surface is None:
            reply = QMessageBox.question(
                self, "No Flow Extensions",
                "Flow extensions have not been applied.\n"
                "Export the raw mesh without extensions?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export STL", "vortex_output.stl", "STL files (*.stl)"
        )
        if not path:
            return

        params = self._state.params.copy()
        self._start_worker("export", ExportWorker(surface, path, params),
                           on_finished=lambda p: self._on_export_done(p))

    def _on_export_done(self, path: str) -> None:
        self._status.show_message(f"Exported: {os.path.basename(path)}")
        QMessageBox.information(self, "Export Complete", f"STL saved to:\n{path}")

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------

    def _start_worker(self, step: str, worker, on_finished=None) -> None:
        print(f"[MainWindow] Starting worker for step: {step}")
        # Clean up existing worker/thread for this step
        if step in self._threads:
            old_thread = self._threads[step]
            if old_thread.isRunning():
                print(f"[MainWindow] Stopping old thread for step: {step}")
                old_thread.quit()
                old_thread.wait(500)
            del self._threads[step]
        if step in self._workers:
            del self._workers[step]

        thread = QThread()
        self._threads[step] = thread
        self._workers[step] = worker  # KEEP REFERENCE!

        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        
        # Connect cleanup slots
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        
        # Debug signals
        thread.started.connect(lambda: print(f"[MainWindow] Thread STARTED for step: {step}"))
        thread.finished.connect(lambda: print(f"[MainWindow] Thread FINISHED for step: {step}"))

        worker.error.connect(lambda msg: self._on_worker_error(step, msg))
        worker.progress.connect(lambda pct, msg: self._status.set_progress(pct, msg))
        if on_finished:
            worker.finished.connect(on_finished)
        
        worker.setParent(None)
        # We delete later when the thread finishes
        thread.finished.connect(worker.deleteLater)
        # And remove from our dictionary when worker is destroyed
        worker.destroyed.connect(lambda: self._workers.pop(step, None))
        
        thread.start()
        print(f"[MainWindow] Worker thread.start() called for step: {step}")

    def _on_worker_error(self, step: str, message: str) -> None:
        self._update_step(step, "error", "red")
        self._run_btn.setEnabled(True)
        self._centerline_btn.setEnabled(self._state.has_surface())
        self._flow_ext_btn.setEnabled(self._state.has_centerlines())
        self._export_btn.setEnabled(self._state.has_surface())
        self._status.show_message(f"Error in {step}: {message}")
        QMessageBox.critical(self, f"Error — {step}", message)
        log.error("Worker error [%s]: %s", step, message)

    # ------------------------------------------------------------------
    # UI state helpers
    # ------------------------------------------------------------------

    def _update_step(self, step: str, text: str, color: str) -> None:
        lbl = self._step_labels.get(step)
        if lbl:
            c = {"green":"#4caf50","orange":"#ff9800","red":"#f44336","grey":"#666"}.get(color,"#888")
            lbl.setText(f'<span style="color:{c}">{text}</span>')

    def _update_action_states(self) -> None:
        self._run_btn.setEnabled(self._state.has_image())
        self._centerline_btn.setEnabled(self._state.has_surface())
        self._flow_ext_btn.setEnabled(self._state.has_centerlines())
        self._export_btn.setEnabled(self._state.has_surface())

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "About VORTEX Aneurysm",
            "<b>VORTEX Aneurysm 0.2</b><br>"
            "<i>Vascular Output &amp; Real-time Thresholding EXtraction</i><br><br>"
            "Cerebral aneurysm 3D model pipeline<br>"
            "DICOM → segmentation → mesh → flow extensions → STL<br><br>"
            "Built for PhD research on cerebrovascular haemodynamics.<br>"
            "Uses VMTK · SimpleITK · VTK · PyQt5."
        )
