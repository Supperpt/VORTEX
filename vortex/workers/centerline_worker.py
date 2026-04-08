"""Worker: compute vessel centerlines in a background thread.

finished signal carries a dict:
  {
    'centerlines'  : vtkPolyData,
    'profiles'     : list[dict]   # [{id, center_mm, radius_mm}, ...]
  }
"""

import logging

from vortex.workers.base_worker import BaseWorker
from vortex.pipeline import centerlines as cl_pipeline

log = logging.getLogger(__name__)


class CenterlineWorker(BaseWorker):
    def __init__(self, surface, parent=None):
        super().__init__(parent)
        self._surface = surface

    def run(self) -> None:
        try:
            centerlines_poly, profiles = cl_pipeline.compute_centerlines(
                self._surface,
                progress_cb=self._emit_progress,
            )
            self.finished.emit({
                "centerlines": centerlines_poly,
                "profiles":    profiles,
            })
        except Exception as exc:
            log.error("CenterlineWorker failed: %s", exc)
            self.error.emit(str(exc))
