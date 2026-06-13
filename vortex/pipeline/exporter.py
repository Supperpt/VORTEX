"""STL export for VORTEX Aneurysm.

Three output modes:
  - CFD (default): single inner-lumen surface for rigid-wall CFD (OpenFOAM, ANSYS)
  - FSI (build_wall=True): offset outer wall + inner lumen, connected at inlets/outlets
  - Solid (solid=True): watertight filled solid for 3D printing slicers

Entry point:
  export_stl(surface, path, params, progress_cb) → path (str)
"""

import json
import logging
import os

import numpy as np

from vortex.state.app_state import PipelineParams
from vortex.utils.vtk_compat import vtk, vtk_np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_stl(
    surface: "vtk.vtkPolyData",
    path: str,
    params: PipelineParams,
    progress_cb=None,
    sac_surface=None,
    parent_surface=None,
    neck_plane=None,
    cap_labels=None,
) -> str:
    """Write *surface* to an STL file at *path*.

    Mode is determined by params:
      - default (build_wall=False, solid=False) → single-surface CFD STL
      - build_wall=True  → FSI: inner lumen + offset outer wall
      - solid=True       → watertight solid for 3D printing

    Returns the output path.
    """
    def _progress(pct: int, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    if os.path.isdir(path) or path.endswith('/') or path.endswith(os.sep):
        os.makedirs(path, exist_ok=True)
        path = os.path.join(path, "output.stl")
    elif not os.path.splitext(path)[1]:
        path = path + ".stl"
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)

    if params.solid:
        _export_solid(surface, path, _progress)
    elif params.build_wall:
        _export_fsi_wall(surface, path, params.wall_thickness, _progress)
    else:
        _export_cfd(surface, path, params, _progress,
                    sac_surface=sac_surface,
                    parent_surface=parent_surface,
                    neck_plane=neck_plane,
                    cap_labels=cap_labels)

    log.info("Exported STL: %s", path)
    return path


# ---------------------------------------------------------------------------
# Cap extraction (shared by export, the terminal viewer, and cap_label)
# ---------------------------------------------------------------------------

def _extract_patch(surface: "vtk.vtkPolyData", uid: int) -> "vtk.vtkPolyData":
    """Return the sub-surface of *surface* whose CellEntityIds == uid."""
    threshold = vtk.vtkThreshold()
    threshold.SetInputData(surface)
    threshold.SetInputArrayToProcess(
        0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS, "CellEntityIds"
    )
    threshold.SetLowerThreshold(uid)
    threshold.SetUpperThreshold(uid)
    threshold.Update()

    geom = vtk.vtkGeometryFilter()
    geom.SetInputData(threshold.GetOutput())
    geom.Update()
    return geom.GetOutput()


def _patch_area(poly: "vtk.vtkPolyData") -> float:
    """Total triangle area of a patch (mm²). 0.0 if empty."""
    if poly.GetNumberOfCells() == 0:
        return 0.0
    tri = vtk.vtkTriangleFilter()
    tri.SetInputData(poly)
    tri.Update()
    mp = vtk.vtkMassProperties()
    mp.SetInputData(tri.GetOutput())
    return float(mp.GetSurfaceArea())


def iter_caps(surface: "vtk.vtkPolyData"):
    """Yield (uid, patch_poly, centroid, area) for each cap of *surface*.

    Caps are cells with CellEntityIds >= 2 (uid == 1 is the lumen wall and is
    skipped; uid < 2 too). *surface* must carry the 'CellEntityIds' cell array
    written by the capping step (i.e. session.final_surface). Yields nothing if
    the array is absent. Centroid is the mean of the patch points (mm).
    """
    entity_ids = surface.GetCellData().GetArray("CellEntityIds")
    if entity_ids is None:
        return
    ids_array = vtk_np.vtk_to_numpy(entity_ids)
    for uid in np.unique(ids_array):
        uid = int(uid)
        if uid < 2:
            continue  # lumen wall / background — not a cap
        patch_poly = _extract_patch(surface, uid)
        if patch_poly.GetNumberOfCells() == 0:
            continue
        if patch_poly.GetNumberOfPoints():
            pts = vtk_np.vtk_to_numpy(patch_poly.GetPoints().GetData())
            centroid = pts.mean(axis=0)
        else:
            centroid = np.zeros(3)
        yield uid, patch_poly, centroid, _patch_area(patch_poly)


# ---------------------------------------------------------------------------
# CFD single-surface mode
# ---------------------------------------------------------------------------

def _export_cfd(surface: "vtk.vtkPolyData", path: str, params: PipelineParams,
                progress_cb, sac_surface=None, parent_surface=None, neck_plane=None,
                cap_labels=None) -> None:
    """Write a clean single-surface STL suitable for OpenFOAM snappyHexMesh.

    If 'CellEntityIds' cell data is present (from capping) and params.split_patches
    is True, it splits the surface into separate STLs for boundary condition
    assignment in CFD solvers.

    When *sac_surface* is provided (i.e., clip-sac was run), the wall patch
    (EntityId == 1) is replaced by two files. Files are written with the bare
    names vortex-cfd's default naming scheme expects, directly in the output
    directory (no basename prefix):
      aneurysm.stl    — the aneurysm dome           (sac_surface)
      wall.stl        — the parent vessel / lumen wall
      neck_plane.json — neck plane (when *neck_plane* is set)
      <label>.stl     — each cap, named from *cap_labels* (inlet.stl /
                        outlet_1.stl / ...); caps with no entry fall back to
                        cap_<uid>.stl so vortex-cfd can prompt for them.
    """
    cap_labels = cap_labels or {}
    progress_cb(10, "Preparing surface...")
    ready = _clean_and_orient(surface)

    entity_ids = ready.GetCellData().GetArray("CellEntityIds")

    if entity_ids is not None and params.split_patches:
        progress_cb(30, "Found patch labels (CellEntityIds). Splitting into separate STLs...")

        out_dir = os.path.dirname(path) or "."
        ext = os.path.splitext(path)[1] or ".stl"

        # --- Wall (EntityId == 1): dome + parent vessel when clip-sac was run,
        #     otherwise a single wall.stl.
        if sac_surface is not None:
            dome_path = os.path.join(out_dir, f"aneurysm{ext}")
            _write_stl(sac_surface, dome_path)
            log.info("Exported aneurysm dome: %d triangles → %s",
                     sac_surface.GetNumberOfCells(), dome_path)
            if parent_surface is not None:
                parent_path = os.path.join(out_dir, f"wall{ext}")
                _write_stl(parent_surface, parent_path)
                log.info("Exported parent vessel: %d triangles → %s",
                         parent_surface.GetNumberOfCells(), parent_path)
            if neck_plane is not None:
                json_path = os.path.join(out_dir, "neck_plane.json")
                with open(json_path, "w") as fh:
                    json.dump(neck_plane, fh, indent=2)
                log.info("Exported neck plane → %s", json_path)
        else:
            wall_poly = _extract_patch(ready, 1)
            if wall_poly.GetNumberOfCells():
                wall_path = os.path.join(out_dir, f"wall{ext}")
                _write_stl(wall_poly, wall_path)
                log.info("Exported wall: %d triangles → %s",
                         wall_poly.GetNumberOfCells(), wall_path)

        # --- Caps (EntityId >= 2): inlet/outlet from cap_labels, else cap_<uid>.
        for uid, patch_poly, _centroid, _area in iter_caps(ready):
            label = cap_labels.get(uid, f"cap_{uid}")
            cap_path = os.path.join(out_dir, f"{label}{ext}")
            _write_stl(patch_poly, cap_path)
            log.info("Exported cap %d as '%s': %d triangles → %s",
                     uid, label, patch_poly.GetNumberOfCells(), cap_path)

        progress_cb(100, "Patched export complete.")

    else:
        progress_cb(60, f"Writing {os.path.basename(path)}...")
        _write_stl(ready, path)
        progress_cb(100, "Export complete.")
        log.info("CFD STL: %d triangles → %s", ready.GetNumberOfCells(), path)


# ---------------------------------------------------------------------------
# Solid mode (3D printing)
# ---------------------------------------------------------------------------

def _export_solid(surface: "vtk.vtkPolyData", path: str, progress_cb) -> None:
    """Fill holes and produce a watertight solid mesh for slicer software."""
    progress_cb(5, "Filling holes for solid export...")

    # Fill any remaining holes
    fill = vtk.vtkFillHolesFilter()
    fill.SetInputData(surface)
    fill.SetHoleSize(1000.0)   # large enough to close any inlet/outlet caps
    fill.Update()

    progress_cb(35, "Cleaning solid mesh...")
    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(fill.GetOutput())
    cleaner.Update()

    # All normals outward — required by most slicers
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(cleaner.GetOutput())
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.SplittingOff()
    normals.Update()
    solid = normals.GetOutput()

    progress_cb(70, f"Writing solid STL ({solid.GetNumberOfCells():,} triangles)...")
    _write_stl(solid, path)
    progress_cb(100, "Solid export complete.")
    log.info("Solid STL: %d triangles → %s", solid.GetNumberOfCells(), path)


# ---------------------------------------------------------------------------
# FSI wall-thickness mode
# ---------------------------------------------------------------------------

def _export_fsi_wall(
    surface: "vtk.vtkPolyData",
    path: str,
    wall_thickness: float,
    progress_cb,
) -> None:
    """Build an outer wall shell at *wall_thickness* mm from the lumen surface.

    The output STL contains:
      - The original inner surface (lumen boundary, inward normals)
      - The offset outer surface (outward normals)
      - Connecting annular strips at each open boundary profile

    This produces a hollow shell representing the vessel wall for FSI solvers.
    The lumen diameter is preserved (wall grows outward).
    """
    progress_cb(5, "Building FSI wall shell...")

    inner = _clean_and_orient(surface)

    # Offset outer surface: move each vertex along its outward normal
    progress_cb(20, f"Offsetting outer wall by {wall_thickness} mm...")
    outer = _offset_surface(inner, wall_thickness)

    # Reverse inner surface normals (inside of shell faces inward)
    progress_cb(40, "Orienting inner surface normals...")
    inner_rev = _reverse_normals(inner)

    # Detect boundary edges on inner surface, build connecting strips
    progress_cb(55, "Connecting inner/outer surfaces at boundaries...")
    strips = _build_connecting_strips(inner, outer)

    # Combine all pieces
    progress_cb(70, "Merging wall components...")
    appender = vtk.vtkAppendPolyData()
    appender.AddInputData(inner_rev)
    appender.AddInputData(outer)
    for strip in strips:
        appender.AddInputData(strip)
    appender.Update()

    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(appender.GetOutput())
    cleaner.Update()
    shell = cleaner.GetOutput()

    progress_cb(85, f"Writing FSI wall STL ({shell.GetNumberOfCells():,} triangles)...")
    _write_stl(shell, path)
    progress_cb(100, "FSI wall export complete.")
    log.info("FSI wall STL: %d triangles, thickness=%.2f mm → %s",
             shell.GetNumberOfCells(), wall_thickness, path)


def _offset_surface(surface: "vtk.vtkPolyData", offset: float) -> "vtk.vtkPolyData":
    """Move each vertex *offset* mm along its outward normal."""
    # Ensure outward normals are computed
    normals_filter = vtk.vtkPolyDataNormals()
    normals_filter.SetInputData(surface)
    normals_filter.ConsistencyOn()
    normals_filter.AutoOrientNormalsOn()
    normals_filter.ComputePointNormalsOn()
    normals_filter.SplittingOff()
    normals_filter.Update()
    poly = normals_filter.GetOutput()

    pts    = vtk_np.vtk_to_numpy(poly.GetPoints().GetData()).copy()
    norms  = vtk_np.vtk_to_numpy(poly.GetPointData().GetNormals())

    new_pts = pts + norms * offset

    vtk_pts = vtk.vtkPoints()
    vtk_pts.SetData(vtk_np.numpy_to_vtk(new_pts, deep=True))

    result = vtk.vtkPolyData()
    result.ShallowCopy(poly)
    result.SetPoints(vtk_pts)
    return result


def _reverse_normals(surface: "vtk.vtkPolyData") -> "vtk.vtkPolyData":
    """Reverse all face orientations and normals."""
    reverse = vtk.vtkReverseSense()
    reverse.SetInputData(surface)
    reverse.ReverseCellsOn()
    reverse.ReverseNormalsOn()
    reverse.Update()
    return reverse.GetOutput()


def _build_connecting_strips(
    inner: "vtk.vtkPolyData",
    outer: "vtk.vtkPolyData",
) -> list:
    """Build quad strips connecting inner and outer boundary loops."""
    strips = []

    for src in [inner]:
        boundary = vtk.vtkFeatureEdges()
        boundary.SetInputData(src)
        boundary.BoundaryEdgesOn()
        boundary.FeatureEdgesOff()
        boundary.ManifoldEdgesOff()
        boundary.NonManifoldEdgesOff()
        boundary.Update()

        if boundary.GetOutput().GetNumberOfPoints() == 0:
            continue

        conn = vtk.vtkPolyDataConnectivityFilter()
        conn.SetInputData(boundary.GetOutput())
        conn.SetExtractionModeToAllRegions()
        conn.ColorRegionsOn()
        conn.Update()

        n_regions = conn.GetNumberOfExtractedRegions()
        region_ids = vtk_np.vtk_to_numpy(
            conn.GetOutput().GetPointData().GetArray("RegionId")
        )
        inner_pts = vtk_np.vtk_to_numpy(inner.GetPoints().GetData())
        outer_pts = vtk_np.vtk_to_numpy(outer.GetPoints().GetData())
        all_pts   = vtk_np.vtk_to_numpy(conn.GetOutput().GetPoints().GetData())

        for rid in range(n_regions):
            loop_pts = all_pts[region_ids == rid]
            if len(loop_pts) < 3:
                continue

            # Find closest outer points for each inner boundary point
            # Build simple quad strip (approximate — not exact topology)
            strip = _make_annular_strip(loop_pts, outer_pts)
            if strip is not None:
                strips.append(strip)

    return strips


def _make_annular_strip(
    inner_loop: np.ndarray,
    outer_pts: np.ndarray,
) -> "vtk.vtkPolyData | None":
    """Build a triangulated strip connecting inner_loop to the nearest outer points."""
    from scipy.spatial import cKDTree

    tree = cKDTree(outer_pts)
    _, idx = tree.query(inner_loop)

    n = len(inner_loop)
    if n < 3:
        return None

    points = vtk.vtkPoints()
    triangles = vtk.vtkCellArray()

    for i, (ip, oi) in enumerate(zip(inner_loop, idx)):
        points.InsertNextPoint(*ip)
        points.InsertNextPoint(*outer_pts[oi])

    for i in range(n):
        ni = (i + 1) % n
        i0 = i * 2;  i1 = i * 2 + 1
        n0 = ni * 2; n1 = ni * 2 + 1

        t1 = vtk.vtkTriangle()
        t1.GetPointIds().SetId(0, i0)
        t1.GetPointIds().SetId(1, i1)
        t1.GetPointIds().SetId(2, n0)
        triangles.InsertNextCell(t1)

        t2 = vtk.vtkTriangle()
        t2.GetPointIds().SetId(0, i1)
        t2.GetPointIds().SetId(1, n1)
        t2.GetPointIds().SetId(2, n0)
        triangles.InsertNextCell(t2)

    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetPolys(triangles)
    return poly


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _clean_and_orient(surface: "vtk.vtkPolyData") -> "vtk.vtkPolyData":
    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(surface)
    cleaner.Update()

    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(cleaner.GetOutput())
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.SplittingOff()
    normals.Update()
    return normals.GetOutput()


def _write_stl(surface: "vtk.vtkPolyData", path: str) -> None:
    writer = vtk.vtkSTLWriter()
    writer.SetFileName(path)
    writer.SetInputData(surface)
    writer.SetFileTypeToBinary()
    writer.Write()
