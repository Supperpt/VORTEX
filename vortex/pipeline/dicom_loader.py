"""DICOM loading utilities using SimpleITK.

Provides:
  list_series(folder)          → list of series dicts
  load_series(folder, uid)     → sitk.Image
  image_to_numpy(sitk_image)   → np.ndarray (z, y, x)
  ijk_to_mm(sitk_image, ijk)   → (x, y, z) in world mm
"""

import logging
import os
from typing import Optional

import numpy as np
import SimpleITK as sitk

# Disable ITK global warnings to prevent GDCM warning spam when scanning folders
sitk.ProcessObject_SetGlobalWarningDisplay(False)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Series listing
# ---------------------------------------------------------------------------

def list_series(folder: str) -> list[dict]:
    """Scan *folder* for DICOM series and return a list of metadata dicts.

    Each dict contains:
      series_uid        : str  — DICOM SeriesInstanceUID
      description       : str  — SeriesDescription (or fallback label)
      num_slices        : int
      modality          : str  — CT / MR / etc.
      patient_name      : str
    """
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")

    # Collect all directories to scan (top-level + all subdirectories),
    # because many scanners store each series in its own subdirectory.
    dirs_to_scan = set()
    for root, dirs, files in os.walk(folder):
        dirs_to_scan.add(root)

    # Map uid → (folder_path, file_names) — deduplicate by UID
    uid_map: dict = {}
    for d in sorted(dirs_to_scan):
        for uid in sitk.ImageSeriesReader.GetGDCMSeriesIDs(d):
            if uid not in uid_map:
                files_in_dir = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(d, uid)
                if files_in_dir:
                    uid_map[uid] = (d, files_in_dir)

    if not uid_map:
        log.warning("No DICOM series found in %s (searched %d dirs)", folder, len(dirs_to_scan))
        return []

    results = []
    for uid, (series_dir, file_names) in uid_map.items():
        # Read only the first file's metadata for speed
        reader = sitk.ImageFileReader()
        reader.SetFileName(file_names[0])
        reader.LoadPrivateTagsOn()
        reader.ReadImageInformation()

        def tag(key: str, default: str = "") -> str:
            try:
                val = reader.GetMetaData(key).strip()
                # DICOM strings in non-ASCII locales (like PT-BR 'Relatório') often break the console
                # We encode to ascii ignoring errors to ensure console printing never crashes
                return val.encode('ascii', 'ignore').decode('ascii')
            except Exception:
                return default

        description = tag("0008|103e") or tag("0008|1030") or f"Series {uid[-6:]}"
        modality    = tag("0008|0060", "??")
        patient     = tag("0010|0010", "Unknown")

        results.append(
            dict(
                series_uid=uid,
                series_dir=series_dir,   # actual directory containing the files
                description=description,
                num_slices=len(file_names),
                modality=modality,
                patient_name=patient,
            )
        )
        log.debug("Found series: %s — %s (%d slices)", uid[-8:], description, len(file_names))

    # Sort by number of slices descending (largest series first — most likely the vessel)
    results.sort(key=lambda s: s["num_slices"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Series loading
# ---------------------------------------------------------------------------

def load_series(folder: str, series_uid: str) -> sitk.Image:
    """Load a DICOM series identified by *series_uid* from *folder*.

    Returns a 3-D SimpleITK image with correct spacing, origin, and direction.
    """
    file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(folder, series_uid)
    
    # If not found directly in folder, we might need to look in subdirectories
    if not file_names:
        # Search subdirectories (similar to list_series)
        for root, dirs, files in os.walk(folder):
            if root == folder:
                continue # already checked
            file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(root, series_uid)
            if file_names:
                break
                
    if not file_names:
        raise ValueError(f"No files for series {series_uid} in {folder} or its subdirectories.")

    log.info("Loading %d slices for series %s...", len(file_names), series_uid[-8:])

    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(file_names)
    reader.MetaDataDictionaryArrayUpdateOn()
    reader.LoadPrivateTagsOn()
    image = reader.Execute()

    log.info(
        "Loaded: size=%s  spacing=%.2f×%.2f×%.2f mm",
        image.GetSize(),
        *image.GetSpacing(),
    )
    return image


# ---------------------------------------------------------------------------
# Coordinate conversion
# ---------------------------------------------------------------------------

def ijk_to_mm(sitk_image: sitk.Image, ijk: tuple) -> tuple:
    """Convert image index coordinates (i, j, k) to world coordinates (mm).

    SimpleITK uses (x, y, z) ordering for physical points but (i, j, k)
    ordering for index points, both as (column, row, slice).
    """
    return sitk_image.TransformIndexToPhysicalPoint(
        (int(ijk[0]), int(ijk[1]), int(ijk[2]))
    )


def mm_to_ijk(sitk_image: sitk.Image, mm: tuple) -> tuple:
    """Convert world coordinates (mm) to image index coordinates."""
    pt = sitk_image.TransformPhysicalPointToIndex(
        (float(mm[0]), float(mm[1]), float(mm[2]))
    )
    return tuple(pt)


# ---------------------------------------------------------------------------
# Numpy helpers
# ---------------------------------------------------------------------------

def image_to_numpy(sitk_image: sitk.Image) -> np.ndarray:
    """Return a numpy view of the image in (z, y, x) / (slice, row, col) order."""
    return sitk.GetArrayViewFromImage(sitk_image)


def resample_image(sitk_image: sitk.Image, factor: float) -> sitk.Image:
    """Isotropically upsample *sitk_image* by *factor* using B-spline interpolation.

    Used to improve segmentation quality on thick-slice acquisitions.
    factor=2 doubles the resolution in all three axes.
    """
    if factor <= 1.0:
        return sitk_image

    orig_spacing = np.array(sitk_image.GetSpacing())
    orig_size    = np.array(sitk_image.GetSize())

    new_spacing = orig_spacing / factor
    new_size    = (orig_size * factor).astype(int).tolist()

    log.info("Resampling: %s → %s (factor %.1f×)", list(orig_size), new_size, factor)

    resampler = sitk.ResampleImageFilter()
    resampler.SetOutputSpacing(new_spacing.tolist())
    resampler.SetSize(new_size)
    resampler.SetOutputDirection(sitk_image.GetDirection())
    resampler.SetOutputOrigin(sitk_image.GetOrigin())
    resampler.SetTransform(sitk.Transform())
    resampler.SetInterpolator(sitk.sitkBSpline)
    resampler.SetDefaultPixelValue(sitk_image.GetPixelIDValue())

    return resampler.Execute(sitk_image)
