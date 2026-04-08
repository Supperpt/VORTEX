"""Custom status bar widget: progress bar + step label + cursor info."""

from PyQt5.QtWidgets import QStatusBar, QLabel, QProgressBar
from PyQt5.QtCore import Qt


class StatusBar(QStatusBar):
    def __init__(self, parent=None):
        super().__init__(parent)

        self._cursor_label = QLabel("  x=-- y=-- z=--  HU=--  ")
        self._cursor_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._progress = QProgressBar()
        self._progress.setFixedWidth(220)
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.hide()

        self._step_label = QLabel()
        self._step_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.addWidget(self._cursor_label, 1)
        self.addPermanentWidget(self._progress)
        self.addPermanentWidget(self._step_label)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_cursor_info(self, x: float, y: float, z: float, hu: float) -> None:
        self._cursor_label.setText(
            f"  x={x:.1f}  y={y:.1f}  z={z:.1f}  HU={hu:.0f}  "
        )

    def set_progress(self, percent: int, message: str) -> None:
        self._progress.show()
        self._progress.setValue(percent)
        self._step_label.setText(message + "  ")
        if percent >= 100:
            self._progress.hide()

    def set_step(self, message: str) -> None:
        self._step_label.setText(message + "  ")

    def clear_progress(self) -> None:
        self._progress.hide()
        self._progress.setValue(0)

    def show_message(self, message: str, timeout_ms: int = 3000) -> None:
        self.showMessage(message, timeout_ms)
