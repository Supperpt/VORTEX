"""VORTEX Aneurysm — application entry point.

Run via:
    python -m vortex.main
or:
    bash run.sh
"""

import sys
import traceback

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import Qt

from vortex.utils.logging_config import setup_logging, get_logger
from vortex.ui.main_window import MainWindow

log = get_logger(__name__)


def _install_exception_hook(app: QApplication) -> None:
    """Show a QMessageBox for unhandled Python exceptions instead of crashing silently."""
    def handler(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical("Unhandled exception:\n%s", msg)
        QMessageBox.critical(
            None,
            "Unhandled Error",
            f"<b>{exc_type.__name__}</b>: {exc_value}<br><br>"
            f"<pre style='font-size:10px'>{msg[-2000:]}</pre>",
        )
    sys.excepthook = handler


def main() -> int:
    setup_logging()

    # High-DPI support
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,    True)

    app = QApplication(sys.argv)
    app.setApplicationName("VORTEX Aneurysm")
    app.setApplicationVersion("0.1.0")
    app.setStyle("Fusion")   # consistent cross-platform look

    _install_exception_hook(app)

    window = MainWindow()
    window.show()

    log.info("VORTEX Aneurysm started.")
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
