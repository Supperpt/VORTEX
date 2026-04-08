"""Flow extension endpoint selection dialog.

Shows the detected open boundary profiles and lets the user:
  - See each profile's position and estimated radius
  - Toggle which profiles get flow extensions (all selected by default)
  - Adjust the extension length ratio

Emits the selected profile IDs on accept.
"""

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QListWidget, QListWidgetItem, QGroupBox,
    QDoubleSpinBox, QFormLayout, QCheckBox, QPushButton,
)
from PyQt5.QtCore import Qt


class FlowExtDialog(QDialog):
    """Modal dialog for selecting which vessel boundaries get flow extensions.

    Parameters
    ----------
    profiles : list[dict]
        Each dict: { id: int, center_mm: (x,y,z), radius_mm: float }
    default_ratio : float
        Default extension length ratio (extension = ratio × vessel radius)
    parent : QWidget
    """

    def __init__(self, profiles: list, default_ratio: float = 5.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Flow Extension Setup")
        self.setMinimumWidth(420)
        self._profiles = profiles
        self._build_ui(default_ratio)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, default_ratio: float) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel(
            "Select which vessel openings should receive flow extensions.\n"
            "All openings are selected by default. Extensions are aligned\n"
            "with the vessel centreline and capped for CFD watertightness."
        ))

        # Profile list
        profile_group = QGroupBox("Detected Boundary Profiles")
        profile_inner = QVBoxLayout(profile_group)

        sel_row = QHBoxLayout()
        sel_all = QPushButton("Select All")
        sel_none = QPushButton("Deselect All")
        sel_all.setFixedHeight(22)
        sel_none.setFixedHeight(22)
        sel_all.clicked.connect(self._select_all)
        sel_none.clicked.connect(self._deselect_all)
        sel_row.addWidget(sel_all)
        sel_row.addWidget(sel_none)
        sel_row.addStretch()
        profile_inner.addLayout(sel_row)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setMinimumHeight(140)

        for p in self._profiles:
            cx, cy, cz = p["center_mm"]
            label = (
                f"Profile {p['id']}   "
                f"r ≈ {p['radius_mm']:.1f} mm   "
                f"({cx:.1f}, {cy:.1f}, {cz:.1f}) mm"
            )
            item = QListWidgetItem(label)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, p["id"])
            self._list.addItem(item)

        profile_inner.addWidget(self._list)
        layout.addWidget(profile_group)

        # Extension parameters
        param_group = QGroupBox("Extension Parameters")
        param_form  = QFormLayout(param_group)

        self._ratio_spin = QDoubleSpinBox()
        self._ratio_spin.setRange(1.0, 20.0)
        self._ratio_spin.setSingleStep(0.5)
        self._ratio_spin.setDecimals(1)
        self._ratio_spin.setValue(default_ratio)
        self._ratio_spin.setSuffix(" × radius")
        param_form.addRow("Extension length:", self._ratio_spin)

        hint = QLabel(
            "Recommended: 5–10× radius for OpenFOAM. "
            "Longer extensions improve inlet flow development."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: grey; font-size: 10px;")
        param_form.addRow("", hint)

        layout.addWidget(param_group)

        # OK / Cancel
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _select_all(self) -> None:
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(Qt.Checked)

    def _deselect_all(self) -> None:
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(Qt.Unchecked)

    # ------------------------------------------------------------------
    # Result accessors (call after exec_() == QDialog.Accepted)
    # ------------------------------------------------------------------

    def selected_profile_ids(self) -> list:
        """Return list of profile IDs that the user selected."""
        ids = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.Checked:
                ids.append(item.data(Qt.UserRole))
        return ids

    def extension_ratio(self) -> float:
        return self._ratio_spin.value()
