"""Worker: export STL in a background thread.

finished signal carries: str (output path)
"""

import logging

from vortex.workers.base_worker import BaseWorker
from vortex.pipeline import exporter
from vortex.state.app_state import PipelineParams

log = logging.getLogger(__name__)


class ExportWorker(BaseWorker):
    def __init__(self, surface, path: str, params: PipelineParams, parent=None):
        super().__init__(parent)
        self._surface = surface
        self._path    = path
        self._params  = params

    def run(self) -> None:
        try:
            out_path = exporter.export_stl(
                self._surface,
                self._path,
                self._params,
                progress_cb=self._emit_progress,
            )
            self.finished.emit(out_path)
        except Exception as exc:
            log.error("ExportWorker failed: %s", exc)
            self.error.emit(str(exc))
