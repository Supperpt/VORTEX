"""Worker: load a DICOM series in a background thread.

finished signal carries a dict:
  {
    'image'       : sitk.Image,
    'array'       : np.ndarray  (z, y, x),
    'spacing'     : (sx, sy, sz),
    'series_uid'  : str,
  }
"""

import logging
import traceback

import SimpleITK as sitk
import numpy as np

from vortex.workers.base_worker import BaseWorker
from vortex.pipeline import dicom_loader

log = logging.getLogger(__name__)


class LoadWorker(BaseWorker):
    def __init__(self, folder: str, series_uid: str, parent=None):
        super().__init__(parent)
        self._folder     = folder
        self._series_uid = series_uid

    def run(self) -> None:
        try:
            print(f"[LoadWorker] Started for folder: {self._folder}, UID: {self._series_uid}")
            self._emit_progress(0, "Loading DICOM series...")
            image = dicom_loader.load_series(self._folder, self._series_uid)

            print(f"[LoadWorker] Image loaded, size: {image.GetSize()}")
            self._emit_progress(80, "Converting to numpy array...")
            array = dicom_loader.image_to_numpy(image)

            print(f"[LoadWorker] Array converted, shape: {array.shape}")
            self._emit_progress(100, "DICOM loaded.")
            self.finished.emit({
                "image":      image,
                "array":      array,
                "spacing":    image.GetSpacing(),
                "series_uid": self._series_uid,
            })
        except Exception as exc:
            print(f"[LoadWorker] Error: {exc}")
            log.error("LoadWorker failed: %s", exc)
            self.error.emit(str(exc))
