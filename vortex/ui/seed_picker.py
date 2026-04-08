"""Minimal seed picker window for VORTEX Aneurysm."""

import sys
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QLabel, QApplication, QMessageBox
)
from PyQt5.QtCore import Qt

from vortex.ui.slice_viewer import SliceViewerWidget
from vortex.pipeline.dicom_loader import list_series, load_series, image_to_numpy
from vortex.utils.logging_config import setup_logging


class SeedPickerWindow(QMainWindow):
    def __init__(self, folder: str, series_uid: str = None):
        super().__init__()
        self.setWindowTitle("VORTEX Aneurysm — Seed Picker")
        self.resize(1000, 700)

        self.folder = folder
        self.series_uid = series_uid
        self.selected_ijk = None

        self._build_ui()
        self._load_data()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.info_label = QLabel("Loading DICOM series...")
        self.info_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(self.info_label)

        self.viewer = SliceViewerWidget()
        self.viewer.seed_placed.connect(self._on_seed_placed)
        layout.addWidget(self.viewer, stretch=1)

        bottom = QHBoxLayout()
        self.coords_label = QLabel("No seed selected. Switch to 'Seed' mode and click.")
        bottom.addWidget(self.coords_label)
        bottom.addStretch()

        self.btn_confirm = QPushButton("Confirm & Close")
        self.btn_confirm.setEnabled(False)
        self.btn_confirm.clicked.connect(self.close)
        bottom.addWidget(self.btn_confirm)
        
        layout.addLayout(bottom)

    def _load_data(self):
        try:
            series = list_series(self.folder)
            if not series:
                QMessageBox.critical(self, "Error", f"No DICOM series found in {self.folder}")
                sys.exit(1)
            
            uid = self.series_uid or series[0]["series_uid"]
            desc = next((s["description"] for s in series if s["series_uid"] == uid), "Unknown")
            
            self.info_label.setText(f"Series: {desc} ({uid[-8:]})")
            
            sitk_img = load_series(self.folder, uid)
            array = image_to_numpy(sitk_img)
            self.viewer.set_image(sitk_img, array)
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load DICOM: {str(e)}")
            sys.exit(1)

    def _on_seed_placed(self, ijk):
        self.selected_ijk = ijk
        self.coords_label.setText(f"Selected Seed: <b>{ijk[0]},{ijk[1]},{ijk[2]}</b>")
        self.coords_label.setStyleSheet("color: #00ff00;")
        self.btn_confirm.setEnabled(True)


def pick_seed(folder: str, series_uid: str = None):
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    # Ensure we use XCB/offscreen as requested by environment, but here we WANT a window
    # If the user specifically ran seed-picker, they likely have a display.
    
    win = SeedPickerWindow(folder, series_uid)
    win.show()
    app.exec_()
    
    if win.selected_ijk:
        return win.selected_ijk
    return None
