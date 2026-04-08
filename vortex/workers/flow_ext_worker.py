"""Worker: add flow extensions and cap the surface in a background thread.

finished signal carries: vtkPolyData (watertight capped surface)
"""

import logging

from vortex.workers.base_worker import BaseWorker
from vortex.pipeline import flow_extensions as fe_pipeline
from vortex.state.app_state import PipelineParams

log = logging.getLogger(__name__)


class FlowExtWorker(BaseWorker):
    def __init__(self, surface, centerlines, params: PipelineParams, parent=None):
        super().__init__(parent)
        self._surface     = surface
        self._centerlines = centerlines
        self._params      = params

    def run(self) -> None:
        try:
            capped = fe_pipeline.add_flow_extensions(
                self._surface,
                self._centerlines,
                self._params,
                progress_cb=self._emit_progress,
            )
            self.finished.emit(capped)
        except Exception as exc:
            log.error("FlowExtWorker failed: %s", exc)
            self.error.emit(str(exc))
