"""Image segmentation pipeline.

Two modes:
  - Threshold only (fast, default):   HU threshold + morphological cleanup
  - Level-set refinement (accurate):  threshold → signed distance init → ITK level-set

Entry point:
  segment(sitk_image, params, progress_cb) → vtkImageData
"""

import logging
from typing import Callable, Optional

import numpy as np
import SimpleITK as sitk

from vortex.state.app_state import PipelineParams
from vortex.utils.vtk_compat import vtk, sitk_to_vtk

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def segment(
    sitk_image: sitk.Image,
    params: PipelineParams,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> "vtk.vtkImageData":
    """Segment *sitk_image* and return a vtkImageData ready for marching cubes.

    Mode depends on params.use_levelset:
      False (default) → fast HU threshold segmentation, iso at 0.5
      True            → threshold + ITK level-set refinement, iso at 0.0

    The returned vtkImageData scalar value convention:
      Threshold mode : 0.0 outside, 1.0 inside  → marching cubes at 0.5
      Level-set mode : negative inside, positive outside → marching cubes at 0.0
    """
    def _progress(pct: int, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    _progress(0, "Starting segmentation...")

    if not params.seed_point_ijk:
        raise ValueError(
            "A seed point is required for segmentation. "
            "Use the 'seed' command to pick a point inside the vessel first."
        )

    # Convert seed IJK to physical coordinates so it survives any grid changes
    seed_physical_mm = sitk_image.TransformIndexToPhysicalPoint(
        (int(params.seed_point_ijk[0]), int(params.seed_point_ijk[1]), int(params.seed_point_ijk[2]))
    )

    # ------------------------------------------------------------------
    # Optional ROI cropping at ORIGINAL resolution (before upsampling)
    # ------------------------------------------------------------------
    if params.roi_radius > 0:
        _progress(5, f"Cropping ROI around seed (radius={params.roi_radius} mm)...")
        seed_ijk_orig = sitk_image.TransformPhysicalPointToIndex(seed_physical_mm)
        sitk_image = _crop_roi(sitk_image, seed_ijk_orig, params.roi_radius)

    # ------------------------------------------------------------------
    # Resample HU image first, then threshold + filter.
    # BSpline interpolation of smooth HU values gives sub-voxel boundary
    # accuracy at vessel edges.  Component filtering on the (already
    # cropped) upsampled volume is safe — the ROI keeps it small.
    # ------------------------------------------------------------------
    if params.resample > 1.0:
        from vortex.pipeline.dicom_loader import resample_image
        _progress(15, f"Resampling ×{params.resample:.1f}...")
        sitk_image = resample_image(sitk_image, params.resample)

    _progress(35, "Applying HU threshold...")
    binary_mask = _threshold(sitk_image, params.lower_threshold, params.upper_threshold)

    local_seed_ijk = sitk_image.TransformPhysicalPointToIndex(seed_physical_mm)
    _progress(55, f"Extracting component at seed {local_seed_ijk}...")
    binary_mask = _keep_seed_component(binary_mask, local_seed_ijk)

    _progress(70, "Morphological closing...")
    binary_mask = sitk.BinaryMorphologicalClosing(binary_mask, kernelRadius=(2, 2, 2))
    binary_mask = sitk.Cast(binary_mask, sitk.sitkFloat32)

    # ------------------------------------------------------------------
    # Level-set refinement (optional)
    # ------------------------------------------------------------------
    if params.use_levelset:
        _progress(60, "Initialising level-set from threshold...")
        # Re-binarise for level-set init (needs clean 0/1 mask)
        binary_mask_for_ls = sitk.BinaryThreshold(
            binary_mask, lowerThreshold=0.5, upperThreshold=1e9,
            insideValue=1, outsideValue=0,
        )

        # Sanity check: if the binary mask fills the vast majority of the ROI,
        # the signed distance map will have its zero-crossing at the image
        # boundary and the level-set will produce a featureless cube instead of
        # the vessel surface. This happens when roi_radius is too small and the
        # entire crop is inside the vessel/aneurysm.
        mask_arr   = sitk.GetArrayViewFromImage(binary_mask_for_ls)
        fill_ratio = float(mask_arr.sum()) / float(mask_arr.size)
        if fill_ratio > 0.85:
            log.warning(
                "Level-set init mask fills %.0f%% of the ROI — the entire crop is "
                "inside the vessel. Level-set would produce a cube, not a vessel surface. "
                "Falling back to threshold result. Increase roi_radius to ≥25 mm or "
                "disable use_levelset.",
                fill_ratio * 100,
            )
            _progress(80, "ROI too small for level-set — using threshold result")
            float_mask = sitk.Cast(binary_mask, sitk.sitkFloat32)
            vtk_image  = sitk_to_vtk(float_mask)
            log.info("Threshold segmentation done (level-set skipped): [%.0f, %.0f] HU",
                     params.lower_threshold, params.upper_threshold)
            _progress(100, "Segmentation complete.")
            return vtk_image

        levelset_image = _run_levelset(sitk_image, binary_mask_for_ls, params, _progress)
        _progress(90, "Converting level-set to VTK...")
        float_img = sitk.Cast(levelset_image, sitk.sitkFloat32)
        vtk_image = sitk_to_vtk(float_img)
        log.info(
            "Level-set segmentation done: threshold=[%.0f,%.0f] HU, iterations=%d",
            params.lower_threshold, params.upper_threshold, params.levelset_iterations,
        )
    else:
        _progress(80, "Converting to VTK...")
        float_mask = sitk.Cast(binary_mask, sitk.sitkFloat32)
        vtk_image  = sitk_to_vtk(float_mask)
        log.info(
            "Threshold segmentation done: [%.0f, %.0f] HU",
            params.lower_threshold, params.upper_threshold,
        )

    _progress(100, "Segmentation complete.")
    return vtk_image


def get_iso_value(params: PipelineParams) -> float:
    """Return the correct iso-value for marching cubes given the segmentation mode."""
    return 0.0 if params.use_levelset else 0.5


# ---------------------------------------------------------------------------
# Threshold helper
# ---------------------------------------------------------------------------

def _threshold(image: sitk.Image, lower: float, upper: float) -> sitk.Image:
    f = sitk.BinaryThresholdImageFilter()
    f.SetLowerThreshold(lower)
    f.SetUpperThreshold(upper)
    f.SetInsideValue(1)
    f.SetOutsideValue(0)
    return f.Execute(image)


# ---------------------------------------------------------------------------
# Level-set refinement using SimpleITK ThresholdSegmentationLevelSet
# ---------------------------------------------------------------------------

def _run_levelset(
    image: sitk.Image,
    binary_mask: sitk.Image,
    params: PipelineParams,
    progress_cb: Callable,
) -> sitk.Image:
    """Run ITK threshold-based level-set segmentation.

    Initialises from the binary mask (signed distance map) and evolves
    the level-set boundary using the original intensity image as the
    feature map.  Negative = inside vessel, positive = outside.
    """
    progress_cb(58, "Computing signed distance map...")

    signed_dist = sitk.SignedMaurerDistanceMap(
        binary_mask,
        insideIsPositive=False,
        squaredDistance=False,
        useImageSpacing=True,
    )

    progress_cb(63, "Running level-set evolution (may take 1–2 minutes)...")

    feature = sitk.Cast(image, sitk.sitkFloat32)

    ls_filter = sitk.ThresholdSegmentationLevelSetImageFilter()
    ls_filter.SetLowerThreshold(params.lower_threshold)
    ls_filter.SetUpperThreshold(params.upper_threshold)
    ls_filter.SetMaximumRMSError(0.005)
    ls_filter.SetNumberOfIterations(params.levelset_iterations)
    ls_filter.SetCurvatureScaling(params.levelset_curvature)
    ls_filter.SetPropagationScaling(params.levelset_propagation)
    ls_filter.ReverseExpansionDirectionOn()

    init   = sitk.Cast(signed_dist, sitk.sitkFloat32)
    result = ls_filter.Execute(init, feature)

    iters_done = ls_filter.GetElapsedIterations()
    rms        = ls_filter.GetRMSChange()
    log.info("Level-set: %d iterations, final RMS=%.6f", iters_done, rms)
    progress_cb(88, f"Level-set done ({iters_done} iters, RMS={rms:.5f})")

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _keep_largest_component(binary_image: sitk.Image) -> sitk.Image:
    labeled   = sitk.ConnectedComponent(binary_image)
    relabeled = sitk.RelabelComponent(labeled, sortByObjectSize=True)
    return sitk.BinaryThreshold(relabeled, lowerThreshold=1, upperThreshold=1,
                                 insideValue=1, outsideValue=0)


def _keep_seed_component(binary_image: sitk.Image, seed_ijk: tuple) -> sitk.Image:
    """Keep only the connected component containing the seed point."""
    try:
        # ConnectedThreshold using the seed
        connected = sitk.ConnectedThreshold(
            binary_image,
            seedList=[(int(seed_ijk[0]), int(seed_ijk[1]), int(seed_ijk[2]))],
            lower=1,
            upper=1,
            replaceValue=1
        )
        # If seed was not in a 1 region, connected will be empty
        if sitk.GetArrayFromImage(connected).sum() == 0:
            log.warning("Seed point %s is not in a foreground region (HU threshold too high?)", seed_ijk)
            return _keep_largest_component(binary_image)
        return connected
    except Exception as e:
        log.warning("Seed-based extraction failed: %s. Falling back to largest component.", e)
        return _keep_largest_component(binary_image)


def _crop_roi(image: sitk.Image, seed_ijk: tuple, radius_mm: float) -> sitk.Image:
    """Crop image to a box of *radius_mm* around the seed point."""
    spacing = image.GetSpacing()
    # Convert radius from mm to index counts for each axis
    radius_ijk = [int(radius_mm / s) for s in spacing]

    size = image.GetSize()
    lower = [max(0, int(seed_ijk[i] - radius_ijk[i])) for i in range(3)]
    upper = [min(size[i] - 1, int(seed_ijk[i] + radius_ijk[i])) for i in range(3)]

    crop_size = [upper[i] - lower[i] + 1 for i in range(3)]

    log.info("ROI crop: lower=%s, upper=%s, size=%s", lower, upper, crop_size)

    return sitk.RegionOfInterest(image, crop_size, lower)
