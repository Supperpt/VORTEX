"""Worker: run segmentation in a background thread.

finished signal carries: vtkImageData
"""

import logging

from vortex.workers.base_worker import BaseWorker
from vortex.pipeline import segmentation
from vortex.state.app_state import PipelineParams

log = logging.getLogger(__name__)


class SegmentWorker(BaseWorker):
    def __init__(self, sitk_image, params: PipelineParams, parent=None):
        super().__init__(parent)
        self._image  = sitk_image
        self._params = params  # already a copy — safe to read from background thread

    def run(self) -> None:
        try:
            vtk_image = segmentation.segment(
                self._image,
                self._params,
                progress_cb=self._emit_progress,
            )
            self.finished.emit(vtk_image)
        except Exception as exc:
            log.error("SegmentWorker failed: %s", exc)
            self.error.emit(str(exc))
