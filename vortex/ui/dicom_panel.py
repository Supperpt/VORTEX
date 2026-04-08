"""Left panel: DICOM folder browser and series selector."""

import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QGroupBox, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QLabel,
)
from PyQt5.QtCore import pyqtSignal, Qt

from vortex.pipeline.dicom_loader import list_series


class DicomPanel(QWidget):
    """DICOM folder browser + series list.

    Signals
    -------
    series_selected(folder: str, series_uid: str, description: str)
        Emitted when the user clicks a series in the list.
    """

    series_selected = pyqtSignal(str, str, str)  # folder, uid, description

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folder   = ""
        self._series   = []  # list of dicts from dicom_loader.list_series()
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        group = QGroupBox("DICOM")
        inner = QVBoxLayout(group)
        inner.setSpacing(4)

        self._open_btn = QPushButton("Open DICOM Folder…")
        self._open_btn.clicked.connect(self._on_open)

        self._folder_label = QLabel("No folder selected")
        self._folder_label.setWordWrap(True)
        self._folder_label.setStyleSheet("color: grey; font-size: 10px;")

        self._series_list = QListWidget()
        self._series_list.setAlternatingRowColors(True)
        # Handle all types of selection/activation
        self._series_list.itemClicked.connect(self._on_item_clicked)
        self._series_list.itemDoubleClicked.connect(self._on_item_clicked)
        self._series_list.itemActivated.connect(self._on_item_clicked)
        self._series_list.currentRowChanged.connect(self._on_row_changed)

        self._load_btn = QPushButton("Load Selected Series")
        self._load_btn.setEnabled(False)
        self._load_btn.clicked.connect(self._on_load_btn_clicked)

        inner.addWidget(self._open_btn)
        inner.addWidget(self._folder_label)
        inner.addWidget(QLabel("Series:"))
        inner.addWidget(self._series_list)
        inner.addWidget(self._load_btn)

        layout.addWidget(group)
        layout.addStretch()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_open(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select DICOM Folder", os.path.expanduser("~")
        )
        if not folder:
            return

        print(f"[DicomPanel] Opening folder: {folder}")
        self._folder = folder
        self._folder_label.setText(os.path.basename(folder))
        self._series_list.clear()
        self._series = []
        self._load_btn.setEnabled(False)

        try:
            self._series = list_series(folder)
        except Exception as exc:
            print(f"[DicomPanel] Error listing series: {exc}")
            item = QListWidgetItem(f"Error: {exc}")
            item.setFlags(Qt.NoItemFlags)
            self._series_list.addItem(item)
            return

        if not self._series:
            print("[DicomPanel] No DICOM series found.")
            item = QListWidgetItem("No DICOM series found.")
            item.setFlags(Qt.NoItemFlags)
            self._series_list.addItem(item)
            return

        print(f"[DicomPanel] Found {len(self._series)} series.")
        for s in self._series:
            label = (
                f"{s['description']}\n"
                f"  {s['modality']} · {s['num_slices']} slices"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, s["series_uid"])
            self._series_list.addItem(item)

        self._load_btn.setEnabled(True)
        # Auto-select the first (largest) series
        self._series_list.setCurrentRow(0)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        row = self._series_list.row(item)
        print(f"[DicomPanel] Item clicked/activated: row {row}")
        self._emit_selection(row)

    def _on_row_changed(self, row: int) -> None:
        if row >= 0:
            self._load_btn.setEnabled(True)

    def _on_load_btn_clicked(self) -> None:
        row = self._series_list.currentRow()
        print(f"[DicomPanel] Load button clicked: row {row}")
        self._emit_selection(row)

    def _emit_selection(self, row: int) -> None:
        if 0 <= row < len(self._series):
            s = self._series[row]
            series_dir = s.get("series_dir", self._folder)
            print(f"[DicomPanel] Emitting series_selected: {s['description']}")
            self.series_selected.emit(series_dir, s["series_uid"], s["description"])

