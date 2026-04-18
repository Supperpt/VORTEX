"""Surface mesh quality checks for CFD-ready vtkPolyData.

Entry point:
  check_mesh_quality(mesh, deep=False) → dict

Checks performed:
  - Basic stats (points, triangles, surface area, bounding box)
  - Non-manifold edges   (critical — CFD mesher will fail on these)
  - Open boundary loops  (how many holes / vessel openings)
  - Triangle quality     (aspect ratio + min angle via vtkMeshQuality)
  - Normal consistency   (inconsistent normals → wrong boundary conditions)
  - Self-intersections   (only with deep=True — slow)
"""

import logging
from typing import Any, Dict, Optional, Tuple

import numpy as np

from vortex.utils.vtk_compat import vtk, vtk_np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_mesh_quality(mesh: Any, deep: bool = False) -> Dict:
    """Run quality checks on a vtkPolyData surface mesh.

    Parameters
    ----------
    mesh : vtkPolyData
    deep : bool — if True, also run self-intersection detection (slow, O(n²))

    Returns
    -------
    dict with keys:
        stats              — basic mesh statistics
        non_manifold_edges — count of non-manifold edges
        boundary_loops     — count of open boundary loops
        triangle_quality   — aspect ratio and min-angle statistics
        normals_flipped    — estimated count of inconsistently oriented normals
        normals_total      — total points checked for normals
        self_intersections — count of intersecting cell pairs, or None if skipped
        issues             — list of ('error'|'warning'|'info', message) tuples
    """
    results: Dict[str, Any] = {}
    issues = []  # ('error' | 'warning' | 'info', message)

    # ── 1. Basic statistics ──────────────────────────────────────────────────
    n_pts  = mesh.GetNumberOfPoints()
    n_tris = mesh.GetNumberOfCells()
    bounds = mesh.GetBounds()   # (xmin, xmax, ymin, ymax, zmin, zmax)
    dx = round(bounds[1] - bounds[0], 1)
    dy = round(bounds[3] - bounds[2], 1)
    dz = round(bounds[5] - bounds[4], 1)

    results['stats'] = {
        'points':           n_pts,
        'triangles':        n_tris,
        'surface_area_mm2': _compute_surface_area(mesh),
        'bbox_mm':          (dx, dy, dz),
    }

    # ── 2. Non-manifold edges ────────────────────────────────────────────────
    n_nm = _count_non_manifold_edges(mesh)
    results['non_manifold_edges'] = n_nm
    if n_nm > 0:
        issues.append(('error',
            f"{n_nm} non-manifold edge(s) — CFD mesher will likely fail. "
            "Fix in MeshLab: Filters → Cleaning → Select Non-Manifold Edges."))

    # ── 3. Open boundary loops ───────────────────────────────────────────────
    n_loops = _count_boundary_loops(mesh)
    results['boundary_loops'] = n_loops
    if n_loops == 0:
        issues.append(('info',
            "Mesh is closed (watertight). Ready to export or run 'extend'."))
    elif n_loops >= 2:
        issues.append(('info',
            f"{n_loops} open boundary loop(s) — expected before 'extend'."))
    else:
        issues.append(('warning',
            "Only 1 open boundary loop. CFD needs ≥2 openings (inlet + outlet). "
            "Check segmentation or mesh editing."))

    # ── 4. Triangle quality ──────────────────────────────────────────────────
    q = _compute_triangle_quality(mesh)
    results['triangle_quality'] = q
    if q:
        if q['max_aspect_ratio'] > 20.0:
            issues.append(('error',
                f"Max aspect ratio {q['max_aspect_ratio']:.1f} — severely degenerate "
                "triangles present. Re-mesh or increase smoothing."))
        elif q['max_aspect_ratio'] > 5.0:
            issues.append(('warning',
                f"Max aspect ratio {q['max_aspect_ratio']:.1f} — some poor triangles "
                "may affect solver convergence."))
        if q.get('min_angle_deg') is not None and q['min_angle_deg'] < 5.0:
            issues.append(('warning',
                f"Min angle {q['min_angle_deg']:.1f}° — very acute triangles present."))

    # ── 5. Normal consistency ────────────────────────────────────────────────
    n_flipped, n_total = _count_flipped_normals(mesh)
    results['normals_flipped'] = n_flipped
    results['normals_total']   = n_total
    if n_total > 0 and (n_flipped / n_total) > 0.05:
        issues.append(('warning',
            f"~{n_flipped:,}/{n_total:,} normals appear inconsistently oriented. "
            "Re-run 'mesh', or fix in MeshLab: Filters → Normals → Unify Normals."))

    # ── 6. Self-intersections (optional) ────────────────────────────────────
    if deep:
        n_ix = _count_self_intersections(mesh)
        results['self_intersections'] = n_ix
        if n_ix is None:
            issues.append(('info',
                "Self-intersection check not available in this VTK build."))
        elif n_ix > 0:
            issues.append(('error',
                f"{n_ix} self-intersection(s) — mesh crosses itself. "
                "Fix in MeshLab: Filters → Cleaning → Select Self-Intersecting Faces."))
    else:
        results['self_intersections'] = None

    results['issues'] = issues
    return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_surface_area(mesh) -> Optional[float]:
    try:
        tf = vtk.vtkTriangleFilter()
        tf.SetInputData(mesh)
        tf.Update()
        mp = vtk.vtkMassProperties()
        mp.SetInputData(tf.GetOutput())
        mp.Update()
        area = mp.GetSurfaceArea()
        return round(area, 2) if area > 0 else None
    except Exception as e:
        log.debug("Surface area failed: %s", e)
        return None


def _count_non_manifold_edges(mesh) -> int:
    fe = vtk.vtkFeatureEdges()
    fe.SetInputData(mesh)
    fe.BoundaryEdgesOff()
    fe.FeatureEdgesOff()
    fe.ManifoldEdgesOff()
    fe.NonManifoldEdgesOn()
    fe.Update()
    return fe.GetOutput().GetNumberOfCells()


def _count_boundary_loops(mesh) -> int:
    fe = vtk.vtkFeatureEdges()
    fe.SetInputData(mesh)
    fe.BoundaryEdgesOn()
    fe.FeatureEdgesOff()
    fe.ManifoldEdgesOff()
    fe.NonManifoldEdgesOff()
    fe.Update()
    boundary = fe.GetOutput()
    if boundary.GetNumberOfCells() == 0:
        return 0
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(boundary)
    conn.SetExtractionModeToAllRegions()
    conn.Update()
    return conn.GetNumberOfExtractedRegions()


def _compute_triangle_quality(mesh) -> Optional[Dict]:
    try:
        tf = vtk.vtkTriangleFilter()
        tf.SetInputData(mesh)
        tf.Update()
        tri = tf.GetOutput()
        if tri.GetNumberOfCells() == 0:
            return None

        # Aspect ratio
        mq = vtk.vtkMeshQuality()
        mq.SetInputData(tri)
        mq.SetTriangleQualityMeasureToAspectRatio()
        mq.Update()
        ar_arr = mq.GetOutput().GetCellData().GetArray("Quality")
        if ar_arr is None:
            return None
        ar_vals = vtk_np.vtk_to_numpy(ar_arr)
        mean_ar = float(np.mean(ar_vals))
        max_ar  = float(np.max(ar_vals))

        # Min angle
        mq2 = vtk.vtkMeshQuality()
        mq2.SetInputData(tri)
        mq2.SetTriangleQualityMeasureToMinAngle()
        mq2.Update()
        ma_arr = mq2.GetOutput().GetCellData().GetArray("Quality")
        min_angle = float(np.min(vtk_np.vtk_to_numpy(ma_arr))) if ma_arr is not None else None

        return {
            'mean_aspect_ratio': round(mean_ar, 2),
            'max_aspect_ratio':  round(max_ar,  2),
            'min_angle_deg':     round(min_angle, 1) if min_angle is not None else None,
        }
    except Exception as e:
        log.debug("Triangle quality failed: %s", e)
        return None


def _count_flipped_normals(mesh) -> Tuple[int, int]:
    """Compare existing normals against consistently recomputed ones.

    Returns (n_flipped, n_total). Returns (0, 0) if normals are absent.
    """
    existing_vtk = mesh.GetPointData().GetNormals()
    if existing_vtk is None:
        return 0, 0

    n_filter = vtk.vtkPolyDataNormals()
    n_filter.SetInputData(mesh)
    n_filter.ConsistencyOn()
    n_filter.AutoOrientNormalsOn()
    n_filter.SplittingOff()
    n_filter.ComputePointNormalsOn()
    n_filter.ComputeCellNormalsOff()
    n_filter.Update()

    recomputed_vtk = n_filter.GetOutput().GetPointData().GetNormals()
    if recomputed_vtk is None:
        return 0, 0

    try:
        old = vtk_np.vtk_to_numpy(existing_vtk)    # shape (N, 3)
        new = vtk_np.vtk_to_numpy(recomputed_vtk)  # shape (N, 3)
        n   = min(len(old), len(new))
        dots = np.einsum('ij,ij->i', old[:n], new[:n])
        n_flipped = int(np.sum(dots < 0))
        return n_flipped, n
    except Exception as e:
        log.debug("Normal comparison failed: %s", e)
        return 0, 0


def extract_bad_triangles(mesh, ar_threshold: float = 20.0):
    """Extract triangles whose aspect ratio exceeds *ar_threshold*.

    Returns
    -------
    bad_poly : vtkPolyData | None
        PolyData containing only the bad triangles (with "Quality" cell array).
        None if no bad triangles found or quality computation failed.
    worst : list of (float, tuple)
        [(ar_value, (cx, cy, cz)), ...] sorted by AR descending.
    """
    try:
        tf = vtk.vtkTriangleFilter()
        tf.SetInputData(mesh)
        tf.Update()
        tri = tf.GetOutput()
        if tri.GetNumberOfCells() == 0:
            return None, []

        mq = vtk.vtkMeshQuality()
        mq.SetInputData(tri)
        mq.SetTriangleQualityMeasureToAspectRatio()
        mq.Update()
        tri_with_quality = mq.GetOutput()

        thresh = vtk.vtkThreshold()
        thresh.SetInputData(tri_with_quality)
        thresh.SetInputArrayToProcess(
            0, 0, 0,
            vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS,
            "Quality",
        )
        thresh.ThresholdByUpper(ar_threshold)
        thresh.Update()
        ug = thresh.GetOutput()

        if ug.GetNumberOfCells() == 0:
            return None, []

        geom = vtk.vtkGeometryFilter()
        geom.SetInputData(ug)
        geom.Update()
        bad_poly = geom.GetOutput()

        cc = vtk.vtkCellCenters()
        cc.SetInputData(bad_poly)
        cc.Update()
        centers = cc.GetOutput()

        ar_arr = bad_poly.GetCellData().GetArray("Quality")
        results = []
        for i in range(centers.GetNumberOfPoints()):
            pt = centers.GetPoint(i)
            ar = float(ar_arr.GetValue(i)) if ar_arr else 0.0
            results.append((ar, (round(pt[0], 2), round(pt[1], 2), round(pt[2], 2))))
        results.sort(key=lambda x: x[0], reverse=True)

        return bad_poly, results
    except Exception as e:
        log.debug("extract_bad_triangles failed: %s", e)
        return None, []


def _count_self_intersections(mesh) -> Optional[int]:
    """Try to detect self-intersections. Returns None if unavailable."""
    try:
        if not hasattr(vtk, 'vtkPolyDataSelfIntersectionFilter'):
            return None
        f = vtk.vtkPolyDataSelfIntersectionFilter()
        f.SetInputData(mesh)
        f.Update()
        return f.GetOutput().GetNumberOfCells()
    except Exception as e:
        log.debug("Self-intersection check failed: %s", e)
        return None
