"""Flow extension placement and vessel capping for CFD.

Adds cylindrical flow extensions to all (or selected) vessel inlets/outlets,
aligned with the local vessel axis via VMTK's vmtkFlowExtensions.
Then caps all open ends to produce a watertight model for OpenFOAM.

Entry point:
  add_flow_extensions(surface, centerlines, params, progress_cb) → vtkPolyData
"""

import logging
from typing import Callable, Optional

import numpy as np

from vortex.state.app_state import PipelineParams
from vortex.utils.vtk_compat import vtk, vtk_np

log = logging.getLogger(__name__)


def add_flow_extensions(
    surface: "vtk.vtkPolyData",
    centerlines: "vtk.vtkPolyData",
    params: PipelineParams,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> "vtk.vtkPolyData":
    """Add flow extensions and cap all inlets/outlets.

    Parameters
    ----------
    surface      : vtkPolyData — pre-flow-ext surface (output of meshing)
    centerlines  : vtkPolyData — output of centerlines.compute_centerlines()
    params       : PipelineParams (flow_ext_ratio, flow_ext_selected)
    progress_cb  : optional callable(percent, message)

    Returns
    -------
    vtkPolyData — watertight capped surface with flow extensions
    """
    def _progress(pct: int, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    try:
        from vmtk import vmtkscripts
    except ImportError as exc:
        raise ImportError(
            "VMTK is required for flow extensions. "
            "Make sure vmtk is installed in the venv."
        ) from exc

    _progress(0, "Preparing flow extensions...")

    # ------------------------------------------------------------------
    # 0. Selective capping (if only some vessels are selected)
    # ------------------------------------------------------------------
    working_surface = surface
    if params.flow_ext_selected is not None:
        _progress(5, f"Capping non-selected vessels (keeping {len(params.flow_ext_selected)})...")
        working_surface = _cap_excluded_boundaries(surface, params.flow_ext_selected)

    # ------------------------------------------------------------------
    # 1. Flow extension placement via VMTK
    # ------------------------------------------------------------------
    _progress(10, f"Adding flow extensions (ratio={params.flow_ext_ratio}×)...")

    flow_ext = vmtkscripts.vmtkFlowExtensions()
    flow_ext.Surface                      = working_surface
    flow_ext.Centerlines                  = centerlines
    flow_ext.AdaptiveExtensionLength      = 1
    flow_ext.AdaptiveExtensionRatio       = params.flow_ext_ratio
    flow_ext.ExtensionMode                = "centerlinedirection"  # stable; boundarynormal can explode mesh
    flow_ext.AdaptiveNumberOfBoundaryPoints = 0  # don't subdivide the extension cylinder mesh
    flow_ext.Interactive                  = 0
    flow_ext.Execute()

    extended = flow_ext.Surface
    n_after  = extended.GetNumberOfPoints()
    n_before = surface.GetNumberOfPoints()
    log.info("Flow extensions: %d points (was %d)", n_after, n_before)

    if n_after > n_before * 50:
        log.warning(
            "Flow extension point count exploded (%d → %d). "
            "The result may be degenerate — try a smaller flow_ext_ratio or check "
            "that all vessel openings are clean closed loops.",
            n_before, n_after,
        )

    _progress(50, "Flow extensions placed")

    # ------------------------------------------------------------------
    # 2. Cap open ends (watertight model)
    # ------------------------------------------------------------------
    _progress(55, "Capping open ends...")

    # vmtkSurfaceCapper is the correct name in VMTK 1.4 (conda-forge).
    # Older builds may use vmtkCapper. Fall back to vtkFillHolesFilter if neither exists.
    capped = _cap_surface(vmtkscripts, extended)
    _progress(75, "Surface capped")

    # ------------------------------------------------------------------
    # 3. Keep only the largest connected component (discard stray fragments)
    # ------------------------------------------------------------------
    _progress(80, "Cleaning up stray fragments...")
    capped = _largest_region(capped)

    # ------------------------------------------------------------------
    # 4. Final normal recomputation
    # ------------------------------------------------------------------
    _progress(90, "Recomputing surface normals...")
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(capped)
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.SplittingOff()
    
    # We must ensure the CellEntityIds array is passed through the normals filter
    # but vtkPolyDataNormals often strips cell data or interpolates it. 
    # We need to tell it to pass cell data.
    normals.ComputeCellNormalsOn()
    normals.Update()
    result = normals.GetOutput()

    n_cells = result.GetNumberOfCells()
    _progress(100, f"Flow extensions complete: {n_cells:,} triangles")
    log.info("Capped surface: %d points, %d cells", result.GetNumberOfPoints(), n_cells)

    return result


def _cap_surface(vmtkscripts, surface: "vtk.vtkPolyData") -> "vtk.vtkPolyData":
    """Cap open boundaries, trying VMTK first then falling back to VTK."""
    # Try vmtkSurfaceCapper (VMTK 1.4 / conda-forge)
    for cls_name in ("vmtkSurfaceCapper", "vmtkCapper"):
        cls = getattr(vmtkscripts, cls_name, None)
        if cls is not None:
            try:
                capper = cls()
                capper.Surface = surface
                capper.Method  = "simple"
                capper.CellEntityIdsArrayName = "CellEntityIds"
                capper.Interactive = 0
                capper.Execute()
                log.info("Capped with %s", cls_name)
                return capper.Surface
            except Exception as e:
                log.warning("%s failed (%s) — trying next option", cls_name, e)

    # VTK fallback — no CellEntityIds labelling, but still watertight
    log.warning("No VMTK capper found — using vtkFillHolesFilter (no patch labels)")
    filler = vtk.vtkFillHolesFilter()
    filler.SetInputData(surface)
    filler.SetHoleSize(1e6)   # large enough to fill any vessel opening
    filler.Update()
    return filler.GetOutput()


def _cap_excluded_boundaries(surface: "vtk.vtkPolyData", selected_ids: list) -> "vtk.vtkPolyData":
    """Cap any boundary loop NOT in the selected_ids list."""
    # 1. Detect boundary edges
    boundary_filter = vtk.vtkFeatureEdges()
    boundary_filter.SetInputData(surface)
    boundary_filter.BoundaryEdgesOn()
    boundary_filter.FeatureEdgesOff()
    boundary_filter.ManifoldEdgesOff()
    boundary_filter.NonManifoldEdgesOff()
    boundary_filter.Update()

    boundary_poly = boundary_filter.GetOutput()
    if boundary_poly.GetNumberOfPoints() == 0:
        return surface

    # 2. Separate boundary loops by connectivity
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(boundary_poly)
    conn.SetExtractionModeToAllRegions()
    conn.ColorRegionsOn()
    conn.Update()

    n_regions = conn.GetNumberOfExtractedRegions()
    loops = conn.GetOutput()
    
    # Get RegionId array
    region_ids = vtk_np.vtk_to_numpy(loops.GetPointData().GetArray("RegionId"))

    # 3. For each loop, if it's NOT selected, triangulate a cap
    append = vtk.vtkAppendPolyData()
    append.AddInputData(surface)
    
    any_capped = False
    for i in range(n_regions):
        if i not in selected_ids:
            # Extract this specific loop properly using vtkThreshold
            thresh = vtk.vtkThreshold()
            thresh.SetInputData(loops)
            # Find boundary points matching this RegionId
            thresh.ThresholdBetween(i, i)
            thresh.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, "RegionId")
            thresh.Update()
            
            # Convert UnstructuredGrid from threshold to PolyData
            geom = vtk.vtkGeometryFilter()
            geom.SetInputData(thresh.GetOutput())
            geom.Update()
            
            # Triangulate the open loop
            triangulator = vtk.vtkContourTriangulator()
            triangulator.SetInputData(geom.GetOutput())
            triangulator.Update()
            
            append.AddInputData(triangulator.GetOutput())
            any_capped = True
            log.info("Manually capped vessel ID %d before flow extensions", i)

    if not any_capped:
        return surface

    append.Update()
    
    # Merge cap vertices with surface vertices
    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(append.GetOutput())
    cleaner.Update()
    
    return cleaner.GetOutput()


def _largest_region(poly: "vtk.vtkPolyData") -> "vtk.vtkPolyData":
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(poly)
    conn.SetExtractionModeToLargestRegion()
    conn.Update()
    return conn.GetOutput()
