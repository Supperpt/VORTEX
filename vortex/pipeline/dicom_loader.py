"""DICOM and NIfTI loading utilities using SimpleITK.

Provides:
  list_series(path)            → list of series dicts (DICOM series + NIfTI files)
  load_series(path, uid)       → sitk.Image
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

# NIfTI volumes are listed alongside DICOM series. Their series_uid is this
# prefix + the absolute file path, so load_series can tell the two apart.
NIFTI_UID_PREFIX = "nifti:"
NIFTI_EXTENSIONS = (".nii", ".nii.gz")


def is_nifti_path(path: str) -> bool:
    """True if *path* has a NIfTI extension (.nii or .nii.gz)."""
    return path.lower().endswith(NIFTI_EXTENSIONS)


def _nifti_entry(file_path: str) -> Optional[dict]:
    """Build a series dict for one NIfTI file, or None if it is not a 3-D volume."""
    file_path = os.path.abspath(file_path)
    reader = sitk.ImageFileReader()
    reader.SetFileName(file_path)
    try:
        reader.ReadImageInformation()
    except RuntimeError as e:
        log.warning("Skipping unreadable NIfTI %s: %s", file_path, e)
        return None

    size = reader.GetSize()
    if len(size) < 3:
        log.warning("Skipping %s: %d-D image, need a 3-D volume", file_path, len(size))
        return None

    return dict(
        series_uid=NIFTI_UID_PREFIX + file_path,
        series_dir=os.path.dirname(file_path),
        description=os.path.basename(file_path),
        num_slices=int(size[2]),
        modality="NIfTI",
        patient_name="Unknown",
    )


# ---------------------------------------------------------------------------
# Series listing
# ---------------------------------------------------------------------------

def list_series(folder: str) -> list[dict]:
    """Scan *folder* for DICOM series and NIfTI files and return metadata dicts.

    *folder* may also be a single .nii / .nii.gz file, which yields one entry.

    Each dict contains:
      series_uid        : str  — DICOM SeriesInstanceUID, or "nifti:<abs path>"
      description       : str  — SeriesDescription (or file name for NIfTI)
      num_slices        : int
      modality          : str  — CT / MR / etc. ("NIfTI" for NIfTI files)
      patient_name      : str
    """
    if os.path.isfile(folder) and is_nifti_path(folder):
        entry = _nifti_entry(folder)
        return [entry] if entry else []

    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")

    # Collect all directories to scan (top-level + all subdirectories),
    # because many scanners store each series in its own subdirectory.
    dirs_to_scan = set()
    for root, dirs, files in os.walk(folder):
        dirs_to_scan.add(root)

    nifti_entries = []
    for root, dirs, files in os.walk(folder):
        for f in sorted(files):
            if is_nifti_path(f):
                entry = _nifti_entry(os.path.join(root, f))
                if entry:
                    nifti_entries.append(entry)

    # Map uid → (folder_path, file_names) — deduplicate by UID
    uid_map: dict = {}
    for d in sorted(dirs_to_scan):
        for uid in sitk.ImageSeriesReader.GetGDCMSeriesIDs(d):
            if uid not in uid_map:
                files_in_dir = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(d, uid)
                if files_in_dir:
                    uid_map[uid] = (d, files_in_dir)

    if not uid_map and not nifti_entries:
        log.warning("No DICOM series or NIfTI files found in %s (searched %d dirs)",
                    folder, len(dirs_to_scan))
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
    # NIfTI after DICOM so the stable sort keeps DICOM as the default on ties
    results.extend(nifti_entries)
    results.sort(key=lambda s: s["num_slices"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Series loading
# ---------------------------------------------------------------------------

def load_series(folder: str, series_uid: str) -> sitk.Image:
    """Load a DICOM series identified by *series_uid* from *folder*.

    NIfTI volumes (series_uid from list_series starting with "nifti:", or
    *folder* itself being a .nii / .nii.gz file) are delegated to load_nifti.

    Returns a 3-D SimpleITK image with correct spacing, origin, and direction.
    """
    if series_uid and series_uid.startswith(NIFTI_UID_PREFIX):
        return load_nifti(series_uid[len(NIFTI_UID_PREFIX):])
    if os.path.isfile(folder) and is_nifti_path(folder):
        return load_nifti(folder)

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


def load_nifti(file_path: str) -> sitk.Image:
    """Load a 3-D NIfTI volume (.nii / .nii.gz).

    SimpleITK maps the NIfTI sform/qform to the same LPS physical space it
    uses for DICOM, and applies scl_slope/scl_inter, so a CT NIfTI comes out
    in HU just like the DICOM path. A 4-D file keeps only its first volume.
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"NIfTI file not found: {file_path}")

    log.info("Loading NIfTI %s...", os.path.basename(file_path))
    image = sitk.ReadImage(file_path)

    if image.GetDimension() == 4:
        log.warning("%s is 4-D (%d volumes) — using the first volume only",
                    os.path.basename(file_path), image.GetSize()[3])
        size = list(image.GetSize())
        size[3] = 0
        image = sitk.Extract(image, size, [0, 0, 0, 0])
    elif image.GetDimension() != 3:
        raise ValueError(f"{file_path} is {image.GetDimension()}-D; need a 3-D volume.")

    # NIfTI often stores axes flipped/permuted relative to DICOM (e.g. RAS),
    # but sitk_to_vtk drops the direction matrix, so the mesh would come out
    # mirrored relative to world mm. Reorienting to LPS is a lossless axis
    # flip/permute that gives the identity direction a standard axial DICOM has.
    image = sitk.DICOMOrient(image, "LPS")
    if not np.allclose(image.GetDirection(), np.eye(3).ravel(), atol=1e-4):
        log.warning(
            "%s is oblique (direction %s); meshes will not align with world mm.",
            os.path.basename(file_path),
            np.round(image.GetDirection(), 3).tolist(),
        )

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


def sample_hu_sphere(sitk_image: sitk.Image, seed_mm: tuple,
                     radius_mm: float = 3.0) -> Optional[dict]:
    """Sample Hounsfield Unit values inside a physical sphere around *seed_mm*.

    Used by the 'sample-hu' shell command so the user can pick lower/upper HU
    thresholds from the real lumen intensity distribution instead of guessing.

    The sphere is built using physical (mm) distances weighted by the image
    spacing, so it stays isotropic even on anisotropic voxels. *sitk_image* must
    be the raw HU volume (before threshold/resample), which is what the pipeline
    keeps in session.sitk_image.

    Parameters
    ----------
    sitk_image : raw HU volume
    seed_mm    : (x, y, z) world coordinate, expected inside the vessel lumen
    radius_mm  : sphere radius in mm (default 3.0)

    Returns
    -------
    dict with count, radius_mm and HU percentiles (min/p5/median/mean/p95/p99/max),
    or None if the seed falls outside the image or the sphere captures no voxels.
    """
    size = sitk_image.GetSize()            # (nx, ny, nz)
    seed_ijk = sitk_image.TransformPhysicalPointToIndex(
        (float(seed_mm[0]), float(seed_mm[1]), float(seed_mm[2]))
    )
    if not all(0 <= seed_ijk[a] < size[a] for a in range(3)):
        return None  # seed is outside the volume

    arr = sitk.GetArrayViewFromImage(sitk_image)   # (z, y, x)
    spacing = sitk_image.GetSpacing()              # (sx, sy, sz)

    # Radius in voxels per axis (ceil so the sphere is fully covered).
    ri = [int(np.ceil(radius_mm / s)) for s in spacing]   # (rx, ry, rz)
    i0, j0, k0 = seed_ijk                                  # (x, y, z) indices
    x_lo, x_hi = max(0, i0 - ri[0]), min(size[0] - 1, i0 + ri[0])
    y_lo, y_hi = max(0, j0 - ri[1]), min(size[1] - 1, j0 + ri[1])
    z_lo, z_hi = max(0, k0 - ri[2]), min(size[2] - 1, k0 + ri[2])

    # Sub-block in numpy (z, y, x) order.
    sub = arr[z_lo:z_hi + 1, y_lo:y_hi + 1, x_lo:x_hi + 1]

    # Physical-distance spherical mask: offset of each voxel from the seed, in mm.
    zz, yy, xx = np.mgrid[z_lo:z_hi + 1, y_lo:y_hi + 1, x_lo:x_hi + 1]
    dx = (xx - i0) * spacing[0]
    dy = (yy - j0) * spacing[1]
    dz = (zz - k0) * spacing[2]
    mask = (dx * dx + dy * dy + dz * dz) <= radius_mm * radius_mm

    vals = np.asarray(sub[mask], dtype=np.float64)
    if vals.size == 0:
        return None

    p = np.percentile(vals, [5, 50, 95, 99])
    return {
        "count":     int(vals.size),
        "radius_mm": float(radius_mm),
        "min":       float(vals.min()),
        "p5":        float(p[0]),
        "median":    float(p[1]),
        "mean":      float(vals.mean()),
        "p95":       float(p[2]),
        "p99":       float(p[3]),
        "max":       float(vals.max()),
    }


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
