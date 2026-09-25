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
    cull_tears_mm: float = 0.0,
) -> tuple:
    """Compute vessel centerlines and identify open boundary profiles.

    Uses VMTK's vmtkCenterlines with automatic endpoint detection from
    open boundary profiles (no manual seed selection required).

    Two seed selectors are tried, in this order:

      1. ``profileidlist`` with SourceIds/TargetIds — the original path, kept
         byte-for-byte so any surface that works today keeps working.
      2. ``pointlist`` with SourcePoints/TargetPoints in mm — tried ONLY if (1)
         came back empty.

    Why the retry exists: the ids handed to ``profileidlist`` are indices into
    *our* radius-sorted profile list, which is not VMTK's internal cap
    numbering.  On surfaces with many boundary loops the two disagree, VMTK
    fails internally with "Seed id exceeds input number of points!", and
    returns empty geometry with no exception.  Feeding it the profile centres
    in world mm removes the id-mapping assumption entirely.  Because the retry
    only runs where the first attempt already produced nothing, it can rescue a
    broken case but can never change a working one.

    Parameters
    ----------
    surface       : vtkPolyData — cleaned, closed-boundary-free surface
    progress_cb   : optional callable(percent, message)
    cull_tears_mm : if > 0, fill boundary loops smaller than this (mm) before
                    detecting profiles.  Off by default so existing callers are
                    unaffected; `isolate` switches it on.

    Returns
    -------
    (centerlines: vtkPolyData, profiles: list[dict])

    Raises
    ------
    RuntimeError if fewer than 2 open profiles exist, or if both seed
    selectors produce empty centerlines.
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
    # 1b. Optional tear culling (opt-in; off for existing callers)
    # ------------------------------------------------------------------
    if cull_tears_mm > 0.0:
        _progress(8, f"Filling tear loops under {cull_tears_mm:.2f} mm...")
        clean_surface = _cull_tear_loops(clean_surface, cull_tears_mm)

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

    # Attempt 1 — the original path, unchanged.  SeedSelectorName="profileidlist"
    # bypasses VMTK's interactive prompts.  SourceIds/TargetIds must be plain
    # Python lists — vtkIdList objects are not iterable and crash VMTK's seed
    # selector.
    centerlines = _run_vmtk_centerlines(
        clean_surface, vmtkscripts,
        seed_selector="profileidlist",
        source_ids=[0],                              # largest profile = source
        target_ids=list(range(1, len(profiles))),
    )

    if _is_empty(centerlines):
        # Attempt 2 — same computation addressed by world coordinates instead
        # of profile ids.  Only reached when attempt 1 produced nothing, so it
        # cannot alter the result of a surface that already worked.
        log.warning(
            "profileidlist produced empty centerlines on %d profiles — "
            "retrying with the pointlist seed selector",
            len(profiles),
        )
        _progress(55, "Retrying centerlines with coordinate seeds...")
        centerlines = _run_vmtk_centerlines(
            clean_surface, vmtkscripts,
            seed_selector="pointlist",
            source_points=list(profiles[0]["center_mm"]),
            target_points=[c for p in profiles[1:] for c in p["center_mm"]],
        )
        if not _is_empty(centerlines):
            log.info("pointlist retry succeeded: %d lines, %d points",
                     centerlines.GetNumberOfLines(), centerlines.GetNumberOfPoints())

    if _is_empty(centerlines):
        raise RuntimeError(
            f"Centerline computation produced no geometry from {len(profiles)} "
            f"boundary profiles (both the profileidlist and pointlist seed "
            f"selectors returned empty).\n"
            f"The surface most likely carries many small tear loops that are "
            f"being mistaken for vessel openings. Check with 'check-mesh', and "
            f"note that 'isolate' fills tears automatically."
        )

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
# VMTK invocation helpers
# ---------------------------------------------------------------------------

def _is_empty(centerlines) -> bool:
    """True when VMTK handed back no usable centerline geometry.

    VMTK does not raise when its internal seed selection fails; it logs to
    stderr and returns an empty polydata. Without this check the caller gets
    empty centerlines and only discovers the problem much further downstream.
    """
    return (centerlines is None
            or centerlines.GetNumberOfLines() == 0
            or centerlines.GetNumberOfPoints() == 0)


def _run_vmtk_centerlines(
    surface,
    vmtkscripts,
    seed_selector: str,
    source_ids=None,
    target_ids=None,
    source_points=None,
    target_points=None,
):
    """Run vmtkCenterlines with one seed selector. Returns polydata (maybe empty).

    Never raises on a VMTK-internal failure — the caller inspects the result
    with _is_empty() and decides whether to fall back.
    """
    f = vmtkscripts.vmtkCenterlines()
    f.Surface = surface
    f.SeedSelectorName = seed_selector
    if source_ids is not None:
        f.SourceIds = source_ids
    if target_ids is not None:
        f.TargetIds = target_ids
    if source_points is not None:
        f.SourcePoints = source_points
    if target_points is not None:
        f.TargetPoints = target_points
    f.AppendEndPoints = True
    f.Resampling = True
    f.ResamplingStepLength = 0.5  # mm
    try:
        f.Execute()
    except Exception as exc:
        log.warning("vmtkCenterlines(%s) raised: %s", seed_selector, exc)
        return None
    return f.Centerlines


def _cull_tear_loops(surface: "vtk.vtkPolyData", min_radius_mm: float) -> "vtk.vtkPolyData":
    """Fill boundary loops smaller than *min_radius_mm* so only real openings remain.

    Marching cubes on a noisy threshold mask leaves many pinhole tears, and a
    box clip shaves slivers where the wall is tangent to the crop boundary.
    Both register as open boundary profiles and corrupt the source/target seed
    lists. vtkFillHolesFilter sizes holes by diameter, so the radius is doubled.

    SplittingOff on the normals pass is essential: VTK's default duplicates
    points along every feature edge, which tears the mesh topologically and
    makes vtkFeatureEdges report each seam as a fresh boundary loop. Same guard
    as meshing.generate_mesh() and remesh_surface().
    """
    before = len(_detect_boundary_profiles(surface))

    fill = vtk.vtkFillHolesFilter()
    fill.SetInputData(surface)
    fill.SetHoleSize(2.0 * float(min_radius_mm))
    fill.Update()

    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(fill.GetOutput())
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.SplittingOff()
    normals.Update()

    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(normals.GetOutput())
    cleaner.Update()
    out = cleaner.GetOutput()

    after = len(_detect_boundary_profiles(out))
    log.info("Tear culling at %.2f mm: %d boundary loops -> %d", min_radius_mm, before, after)
    return out


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
