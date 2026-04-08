"""Base QObject worker for background pipeline tasks.

Pattern:
    worker = SomeWorker(...)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(handler)
    worker.error.connect(error_handler)
    worker.progress.connect(progress_bar.update)
    thread.start()
"""

from PyQt5.QtCore import QObject, pyqtSignal


class BaseWorker(QObject):
    """Abstract base for all background workers.

    Signals
    -------
    progress(int, str)   — (percent 0-100, human-readable message)
    finished(object)     — result payload; type depends on subclass
    error(str)           — error message; emitted instead of finished on failure
    """

    progress = pyqtSignal(int, str)
    finished = pyqtSignal(object)
    error    = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation.  Workers should check _cancelled periodically."""
        self._cancelled = True

    def run(self) -> None:
        raise NotImplementedError("Subclasses must implement run()")

    def _emit_progress(self, pct: int, msg: str) -> None:
        self.progress.emit(pct, msg)
