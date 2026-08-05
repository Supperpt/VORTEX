"""Surface mesh generation from a segmented vtkImageData.

Pipeline:
  1. Marching cubes  → raw surface (vtkPolyData)
  2. Taubin smoothing → volume-preserving noise removal
  3. Decimation       → optional triangle reduction (params.reduce_mesh)
  4. Subdivision      → optional mesh refinement (params.increase_mesh)

Entry points:
  generate_mesh(vtk_image, params, progress_cb)  → vtkPolyData
  remesh_surface(surface, params, progress_cb)   → vtkPolyData
"""

import logging
from typing import Callable, Optional

import numpy as np

from vortex.state.app_state import PipelineParams
from vortex.utils.vtk_compat import vtk, vtk_np
from vortex.pipeline.segmentation import get_iso_value

log = logging.getLogger(__name__)

# Point-data array vmtkSurfaceCurvature writes (name is not configurable).
# In adaptive mode it carries the per-point target edge length in mm.
_SIZE_FIELD_ARRAY = "Curvature"


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

    # Recompute normals for correct rendering.
    # SplittingOff is essential: with VTK's default SplittingOn, points are
    # duplicated along every feature edge (>30 deg), which tears the mesh
    # topologically and makes vtkFeatureEdges report each seam as an open
    # boundary loop.  See remesh_surface() for the same guard.
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(surface)
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.SplittingOff()
    normals.Update()
    surface = normals.GetOutput()

    _progress(100, f"Mesh ready: {surface.GetNumberOfCells():,} triangles")
    log.info("Final mesh: %d points, %d cells",
             surface.GetNumberOfPoints(), surface.GetNumberOfCells())
    return surface


def remesh_surface(
    surface: "vtk.vtkPolyData",
    params: PipelineParams,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> "vtk.vtkPolyData":
    """Improve surface triangle quality for CFD: Taubin smoothing + uniform remeshing.

    Produces near-equilateral, evenly-sized triangles via vmtkSurfaceRemeshing,
    which removes the irregular/skewed triangulation that forces snappyHexMesh
    into skewed boundary-layer cells downstream.  Operates on the open lumen
    surface and MUST be run before capping/flow extensions — it does not touch
    cap CellEntityIds (there are none yet) and preserves the open boundary loops
    so vmtkFlowExtensions still works.

    Controlled by params.remesh_edge_length (mm, 0 = skip remeshing) and
    params.remesh_smooth_iterations (0 = skip smoothing).

    Returns the remeshed vtkPolyData.
    """
    def _progress(pct: int, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    from vortex.pipeline.mesh_quality import _count_boundary_loops

    n_before = surface.GetNumberOfCells()
    loops_before = _count_boundary_loops(surface)
    _progress(0, f"Preparing surface ({n_before:,} triangles, {loops_before} opening(s))...")

    # 1. Taubin smoothing pass (volume-preserving noise removal).
    if params.remesh_smooth_iterations > 0:
        _progress(20, f"Smoothing surface (Taubin, {params.remesh_smooth_iterations} iter)...")
        surface = _taubin_smooth(surface, iterations=params.remesh_smooth_iterations, pass_band=0.1)

    # 2. Isotropic remeshing via VMTK.
    if params.remesh_edge_length > 0.0:
        try:
            from vmtk import vmtkscripts
        except ImportError as exc:
            raise ImportError(
                "VMTK is required for surface remeshing. "
                "Make sure vmtk is installed in the vortex-aneurysm env."
            ) from exc

        # vmtkSurfaceRemeshing needs pure triangles and merged points — STLs
        # loaded via `load-mesh` routinely carry unmerged duplicates, which
        # make the remesher punch holes.  Same pre-VMTK guard as centerlines.
        _progress(35, "Cleaning input for remesher...")
        surface = _triangulate_and_clean(surface)

        remesher = vmtkscripts.vmtkSurfaceRemeshing()
        remesher.PreserveBoundaryEdges = 1   # keep vessel openings as clean loops for capping

        if params.remesh_adaptive:
            min_edge = params.remesh_min_edge_length
            max_edge = params.remesh_edge_length
            if min_edge <= 0.0 or min_edge >= max_edge:
                raise ValueError(
                    f"Adaptive remeshing needs 0 < remesh_min_edge_length "
                    f"({min_edge}) < remesh_edge_length ({max_edge}). "
                    "Set 'min_edge' below 'edge' via the params command, "
                    "or turn 'adaptive' off for uniform remeshing."
                )
            _progress(40, f"Computing curvature size field ({min_edge}–{max_edge} mm)...")
            surface = _curvature_size_field(surface, min_edge, max_edge)

            _progress(45, f"Remeshing (adaptive, {min_edge}–{max_edge} mm edges)...")
            remesher.Surface = surface
            remesher.ElementSizeMode = "edgelengtharray"
            remesher.TargetEdgeLengthArrayName = _SIZE_FIELD_ARRAY
            remesher.TargetEdgeLengthFactor = 1.0
        else:
            _progress(45, f"Remeshing to uniform {params.remesh_edge_length} mm edges...")
            remesher.Surface = surface
            remesher.ElementSizeMode = "edgelength"
            remesher.TargetEdgeLength = params.remesh_edge_length

        remesher.Execute()
        surface = remesher.Surface

        cleaner = vtk.vtkCleanPolyData()
        cleaner.SetInputData(surface)
        cleaner.Update()
        surface = cleaner.GetOutput()

    # 3. Keep the largest region and recompute normals.
    _progress(80, "Cleaning up...")
    surface = _largest_region(surface)

    # SplittingOff is NOT optional here.  VTK defaults to SplittingOn with a
    # 30 deg feature angle, which duplicates points along every feature edge.
    # That tears the surface topologically, so vtkFeatureEdges then counts each
    # seam as an open boundary -> vmtkCapper caps them -> `cap_label` enumerates
    # dozens of phantom caps (issue #4).  Measured on Test.stl: 3 real openings
    # became 12 with splitting on, 3 with it off.
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(surface)
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.SplittingOff()
    normals.Update()
    surface = normals.GetOutput()

    # 4. Post-condition: remeshing must preserve the opening count.  Anything
    #    else means the surface was torn and capping/CFD downstream will be wrong.
    n_after = surface.GetNumberOfCells()
    loops_after = _count_boundary_loops(surface)
    if loops_after > loops_before:
        msg = (f"Remesh changed the opening count ({loops_before} → {loops_after}) — "
               "the surface was likely torn. Inspect with 'check' before capping.")
        log.warning(msg)
        _progress(95, f"WARNING: {msg}")

    _progress(100, f"Remesh done: {n_before:,} → {n_after:,} triangles, "
                   f"{loops_after} opening(s)")
    log.info("Remesh: %d → %d triangles, %d → %d openings (edge=%.3f mm, smooth=%d iter)",
             n_before, n_after, loops_before, loops_after,
             params.remesh_edge_length, params.remesh_smooth_iterations)
    return surface


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _curvature_size_field(
    surface: "vtk.vtkPolyData",
    min_edge: float,
    max_edge: float,
) -> "vtk.vtkPolyData":
    """Attach a per-point target edge length driven by local surface curvature.

    vmtkSurfaceCurvature with BoundedReciprocal+Offset writes

        size = Offset + 1 / (Epsilon + |curvature|)

    which is bounded by [Offset, Offset + 1/Epsilon].  Mapping that onto the
    caller's millimetre range is therefore exact:

        Offset  = min_edge                    (size as curvature → ∞)
        Epsilon = 1 / (max_edge - min_edge)   (size as curvature → 0)

    so flat parent vessel gets *max_edge* triangles while the dome, blebs and
    neck — where curvature is high and CFD accuracy matters — keep *min_edge*
    ones.  Uniform sizing had to use min_edge everywhere, which is what made
    the meshes too heavy to solve (issue #3).

    The array is named 'Curvature' because that is the (non-configurable) name
    vmtkSurfaceCurvature writes; despite the name it now holds edge lengths in mm.
    """
    from vmtk import vmtkscripts

    curv = vmtkscripts.vmtkSurfaceCurvature()
    curv.Surface = surface
    curv.CurvatureType = "mean"
    curv.AbsoluteCurvature = 1     # bulges and dents both deserve fine triangles
    curv.MedianFiltering = 1       # suppress per-triangle curvature noise
    curv.BoundedReciprocal = 1
    curv.Epsilon = 1.0 / (max_edge - min_edge)
    curv.Offset = min_edge
    curv.Execute()
    out = curv.Surface

    arr = out.GetPointData().GetArray(_SIZE_FIELD_ARRAY)
    if arr is None:
        raise RuntimeError(
            f"vmtkSurfaceCurvature did not produce a '{_SIZE_FIELD_ARRAY}' array; "
            "cannot remesh adaptively. Turn 'adaptive' off to use uniform sizing."
        )
    sizes = vtk_np.vtk_to_numpy(arr)
    log.info("Curvature size field: min %.3f  p50 %.3f  p99 %.3f  max %.3f mm",
             float(sizes.min()), float(np.percentile(sizes, 50)),
             float(np.percentile(sizes, 99)), float(sizes.max()))
    return out


def _triangulate_and_clean(poly: "vtk.vtkPolyData") -> "vtk.vtkPolyData":
    """Force pure triangles and merge duplicate points (pre-VMTK hygiene)."""
    tri = vtk.vtkTriangleFilter()
    tri.SetInputData(poly)
    tri.Update()
    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(tri.GetOutput())
    cleaner.Update()
    return cleaner.GetOutput()


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
