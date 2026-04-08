"""Vessel centerline extraction using VMTK.

Entry point:
  compute_centerlines(surface, progress_cb) → (centerlines_poly, boundary_profiles)

boundary_profiles is a list of dicts:
  [{ id: int, center_mm: (x,y,z), radius_mm: float }, ...]

These are the open boundary profiles on the vessel surface — the locations where
flow extensions will be placed.
"""

import logging
from typing import Callable, Optional

import numpy as np

from vortex.utils.vtk_compat import vtk, vtk_np

log = logging.getLogger(__name__)


def compute_centerlines(
    surface: "vtk.vtkPolyData",
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> tuple:
    """Compute vessel centerlines and identify open boundary profiles.

    Uses VMTK's vmtkCenterlines with automatic endpoint detection from
    open boundary profiles (no manual seed selection required).

    Parameters
    ----------
    surface     : vtkPolyData — cleaned, closed-boundary-free surface
    progress_cb : optional callable(percent, message)

    Returns
    -------
    (centerlines: vtkPolyData, profiles: list[dict])
    """
    def _progress(pct: int, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    _progress(0, "Preparing surface for centerline extraction...")

    try:
        from vmtk import vmtkscripts
    except ImportError as exc:
        raise ImportError(
            "VMTK is required for centerline computation. "
            "Make sure vmtk is installed in the venv."
        ) from exc

    # ------------------------------------------------------------------
    # 1. Surface preparation — ensure clean manifold
    # ------------------------------------------------------------------
    _progress(5, "Cleaning surface...")
    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(surface)
    cleaner.Update()
    clean_surface = cleaner.GetOutput()

    # ------------------------------------------------------------------
    # 2. Identify open boundary profiles
    # ------------------------------------------------------------------
    _progress(10, "Detecting vessel boundaries...")
    profiles = _detect_boundary_profiles(clean_surface)
    log.info("Found %d open boundary profiles", len(profiles))
    _progress(20, f"Found {len(profiles)} vessel boundary profiles")

    if len(profiles) < 2:
        raise RuntimeError(
            "Fewer than 2 open boundary profiles found. "
            "The surface may be missing vessel openings — check segmentation."
        )

    # ------------------------------------------------------------------
    # 3. Centerline computation via VMTK
    # ------------------------------------------------------------------
    _progress(25, "Computing centerlines (this may take 1–3 minutes)...")

    # SeedSelectorName="profileidlist" bypasses VMTK's interactive prompts.
    # SourceIds/TargetIds must be plain Python lists — vtkIdList objects are
    # not iterable and cause a crash inside VMTK's seed selector.
    centerline_filter = vmtkscripts.vmtkCenterlines()
    centerline_filter.Surface = clean_surface
    centerline_filter.SeedSelectorName = "profileidlist"
    centerline_filter.SourceIds = [0]                        # largest profile = source
    centerline_filter.TargetIds = list(range(1, len(profiles)))
    centerline_filter.AppendEndPoints = True
    centerline_filter.Resampling = True
    centerline_filter.ResamplingStepLength = 0.5  # mm
    centerline_filter.Execute()

    centerlines = centerline_filter.Centerlines
    _progress(85, "Centerlines computed")

    # ------------------------------------------------------------------
    # 4. Compute Voronoi diagram radius (stored on centerlines) — used
    #    by flow extensions to estimate the vessel radius at each outlet
    # ------------------------------------------------------------------
    _progress(90, "Computing radius array...")
    try:
        radius_array = vmtkscripts.vmtkCenterlineGeometry()
        radius_array.Centerlines = centerlines
        radius_array.Execute()
        centerlines = radius_array.Centerlines
    except Exception:
        log.warning("vmtkCenterlineGeometry failed — radius array may be missing")

    n_lines = centerlines.GetNumberOfLines() if centerlines else 0
    _progress(100, f"Centerlines ready: {n_lines} paths")
    log.info("Centerlines: %d paths, %d profiles", n_lines, len(profiles))

    return centerlines, profiles


# ---------------------------------------------------------------------------
# Boundary profile detection
# ---------------------------------------------------------------------------

def _detect_boundary_profiles(surface: "vtk.vtkPolyData") -> list:
    """Detect open boundary profiles (free edges) on the surface.

    Returns a list of dicts: {id, center_mm, radius_mm}
    """
    # Extract boundary edges — free edges have only 1 neighbouring cell
    boundary_filter = vtk.vtkFeatureEdges()
    boundary_filter.SetInputData(surface)
    boundary_filter.BoundaryEdgesOn()
    boundary_filter.FeatureEdgesOff()
    boundary_filter.ManifoldEdgesOff()
    boundary_filter.NonManifoldEdgesOff()
    boundary_filter.ColoringOff()
    boundary_filter.Update()

    boundary_poly = boundary_filter.GetOutput()

    if boundary_poly.GetNumberOfPoints() == 0:
        log.warning("No open boundaries detected on surface")
        return []

    # Group boundary edges into separate loops using connectivity filter
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(boundary_poly)
    conn.SetExtractionModeToAllRegions()
    conn.ColorRegionsOn()
    conn.Update()

    n_regions = conn.GetNumberOfExtractedRegions()
    profiles  = []

    region_ids = vtk_np.vtk_to_numpy(
        conn.GetOutput().GetPointData().GetArray("RegionId")
    )
    pts_array = vtk_np.vtk_to_numpy(conn.GetOutput().GetPoints().GetData())

    for region_id in range(n_regions):
        mask  = region_ids == region_id
        group = pts_array[mask]
        if len(group) < 3:
            continue

        center = group.mean(axis=0)
        radius = float(np.sqrt(((group - center) ** 2).sum(axis=1)).mean())

        profiles.append({
            "id":        region_id,
            "center_mm": tuple(float(v) for v in center),
            "radius_mm": round(radius, 2),
        })

    profiles.sort(key=lambda p: p["radius_mm"], reverse=True)
    return profiles
