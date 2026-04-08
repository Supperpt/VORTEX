"""Surface mesh generation from a segmented vtkImageData.

Pipeline:
  1. Marching cubes  → raw surface (vtkPolyData)
  2. Taubin smoothing → volume-preserving noise removal
  3. Decimation       → optional triangle reduction (params.reduce_mesh)
  4. Subdivision      → optional mesh refinement (params.increase_mesh)

Entry point:
  generate_mesh(vtk_image, params, progress_cb) → vtkPolyData
"""

import logging
from typing import Callable, Optional

from vortex.state.app_state import PipelineParams
from vortex.utils.vtk_compat import vtk
from vortex.pipeline.segmentation import get_iso_value

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_mesh(
    vtk_image: "vtk.vtkImageData",
    params: PipelineParams,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> "vtk.vtkPolyData":
    """Convert segmented *vtk_image* to a cleaned surface mesh.

    Parameters
    ----------
    vtk_image   : vtkImageData — output of segmentation.segment()
    params      : PipelineParams
    progress_cb : optional callable(percent: int, message: str)

    Returns
    -------
    vtk.vtkPolyData
    """
    def _progress(pct: int, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    _progress(0, "Running marching cubes...")

    # ------------------------------------------------------------------
    # 1. Marching cubes — iso-value depends on segmentation mode:
    #    0.5 for binary threshold mask, 0.0 for level-set signed distance
    # ------------------------------------------------------------------
    iso = get_iso_value(params)
    mc = vtk.vtkMarchingCubes()
    mc.SetInputData(vtk_image)
    mc.SetValue(0, iso)
    mc.ComputeNormalsOn()
    mc.ComputeGradientsOff()
    mc.Update()
    surface = mc.GetOutput()

    n_cells = surface.GetNumberOfCells()
    log.info("Marching cubes: %d triangles", n_cells)
    _progress(25, f"Marching cubes done ({n_cells:,} triangles)")

    if n_cells == 0:
        raise RuntimeError(
            "Marching cubes produced no surface. "
            "Check that the HU thresholds match the scan type."
        )

    # ------------------------------------------------------------------
    # 2. Keep only the largest connected region
    #    (removes stray fragments from noise/bone)
    # ------------------------------------------------------------------
    _progress(30, "Extracting largest surface region...")
    surface = _largest_region(surface)

    # ------------------------------------------------------------------
    # 3. Taubin smoothing — volume-preserving, better than Laplacian
    # ------------------------------------------------------------------
    _progress(40, "Smoothing surface (Taubin)...")
    surface = _taubin_smooth(surface, iterations=30, pass_band=0.1)

    # ------------------------------------------------------------------
    # 4. Decimation (optional)
    # ------------------------------------------------------------------
    if params.reduce_mesh > 0.0:
        _progress(65, f"Decimating mesh ({params.reduce_mesh*100:.0f}% reduction)...")
        surface = _decimate(surface, params.reduce_mesh)
        log.info("After decimation: %d triangles", surface.GetNumberOfCells())

    # ------------------------------------------------------------------
    # 5. Subdivision (optional)
    # ------------------------------------------------------------------
    if params.increase_mesh > 0:
        _progress(80, f"Subdividing mesh ({params.increase_mesh} passes)...")
        surface = _subdivide(surface, params.increase_mesh)
        log.info("After subdivision: %d triangles", surface.GetNumberOfCells())

    # ------------------------------------------------------------------
    # 6. Clean up duplicate points / degenerate cells
    # ------------------------------------------------------------------
    _progress(90, "Cleaning mesh...")
    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(surface)
    cleaner.Update()
    surface = cleaner.GetOutput()

    # Recompute normals for correct rendering
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(surface)
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.Update()
    surface = normals.GetOutput()

    _progress(100, f"Mesh ready: {surface.GetNumberOfCells():,} triangles")
    log.info("Final mesh: %d points, %d cells",
             surface.GetNumberOfPoints(), surface.GetNumberOfCells())
    return surface


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _largest_region(poly: "vtk.vtkPolyData") -> "vtk.vtkPolyData":
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(poly)
    conn.SetExtractionModeToLargestRegion()
    conn.Update()
    return conn.GetOutput()


def _taubin_smooth(poly: "vtk.vtkPolyData", iterations: int, pass_band: float) -> "vtk.vtkPolyData":
    smoother = vtk.vtkWindowedSincPolyDataFilter()
    smoother.SetInputData(poly)
    smoother.SetNumberOfIterations(iterations)
    smoother.SetPassBand(pass_band)
    smoother.BoundarySmoothingOff()
    smoother.NonManifoldSmoothingOn()
    smoother.NormalizeCoordinatesOn()
    smoother.Update()
    return smoother.GetOutput()


def _decimate(poly: "vtk.vtkPolyData", reduction: float) -> "vtk.vtkPolyData":
    dec = vtk.vtkDecimatePro()
    dec.SetInputData(poly)
    dec.SetTargetReduction(reduction)
    dec.PreserveTopologyOn()
    dec.Update()
    return dec.GetOutput()


def _subdivide(poly: "vtk.vtkPolyData", passes: int) -> "vtk.vtkPolyData":
    sub = vtk.vtkLoopSubdivisionFilter()
    sub.SetInputData(poly)
    sub.SetNumberOfSubdivisions(passes)
    sub.Update()
    return sub.GetOutput()
