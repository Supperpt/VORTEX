"""Worker: generate surface mesh in a background thread.

finished signal carries: vtkPolyData
"""

import logging

from vortex.workers.base_worker import BaseWorker
from vortex.pipeline import meshing
from vortex.state.app_state import PipelineParams

log = logging.getLogger(__name__)


class MeshWorker(BaseWorker):
    def __init__(self, vtk_image, params: PipelineParams, parent=None):
        super().__init__(parent)
        self._vtk_image = vtk_image
        self._params    = params

    def run(self) -> None:
        try:
            surface = meshing.generate_mesh(
                self._vtk_image,
                self._params,
                progress_cb=self._emit_progress,
            )
            self.finished.emit(surface)
        except Exception as exc:
            log.error("MeshWorker failed: %s", exc)
            self.error.emit(str(exc))
