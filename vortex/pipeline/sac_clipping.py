"""Aneurysm sac clipping for VORTEX.

Splits the vessel wall surface into an aneurysm dome patch and a parent vessel
patch using a **centerline bulge-field** method.

Key geometric fact: the parent-vessel centerline runs opening-to-opening through
the lumen and never enters the dome (the dome is a dead-end bulge with no
opening). VMTK centerlines carry a MaximumInscribedSphereRadius (MISR) array — the
local vessel radius. So for every surface point we can compute a dimensionless
bulge ratio:

    bulge(p) = distance(p, nearest_centerline_point) / MISR(that point)

Healthy vessel wall sits at ~1.0; the aneurysm dome bulges to ~1.5-2.5.
Thresholding this smoothed field cleanly separates the dome from the vessel. The
seed point is only used to choose which high-bulge region is the real dome, so it
does not need to be precise.

Entry points:
  compute_bulge_field(surface, centerlines) → (surface_with_scalar, stats)
  clip_aneurysm_sac(surface, centerlines, seed_mm, ratio, progress_cb) → dict
  export_bulge_heatmap(bulge_surface, path)
"""

import logging
import numpy as np

from vortex.utils.vtk_compat import vtk, vtk_np

log = logging.getLogger(__name__)

BULGE_ARRAY = "BulgeRatio"
_MISR_ARRAY = "MaximumInscribedSphereRadius"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_bulge_field(surface, centerlines):
    """Compute the per-point bulge ratio and attach it to the surface.

    bulge(p) = distance(p, nearest centerline point) / MISR(that point)

    Parameters
    ----------
    surface     : vtkPolyData — vessel wall mesh (pre-extension)
    centerlines : vtkPolyData — VMTK centerlines (must carry MISR)

    Returns
    -------
    (surface_with_scalar : vtkPolyData, stats : dict)
        stats has keys: median, p90, p99, max
    """
    from scipy.spatial import cKDTree

    mesh_pts = vtk_np.vtk_to_numpy(surface.GetPoints().GetData())
    cl_pts   = vtk_np.vtk_to_numpy(centerlines.GetPoints().GetData())

    if len(mesh_pts) == 0 or len(cl_pts) == 0:
        raise RuntimeError("Surface or centerlines have no points")

    cl_tree = cKDTree(cl_pts)
    distances, nearest_idx = cl_tree.query(mesh_pts, k=1)

    misr_array = centerlines.GetPointData().GetArray(_MISR_ARRAY)
    if misr_array is not None:
        misr = vtk_np.vtk_to_numpy(misr_array)
        local_radii = np.maximum(misr[nearest_idx], 1e-3)
        bulge = distances / local_radii
    else:
        # No MISR: normalise by the median wall distance so the field is still
        # dimensionless and centred near 1.0 on the healthy vessel.
        log.warning("MISR array absent on centerlines — normalising by median distance")
        med = max(float(np.median(distances)), 1e-3)
        bulge = distances / med

    bulge = bulge.astype(np.float64)

    out = vtk.vtkPolyData()
    out.DeepCopy(surface)
    arr = vtk_np.numpy_to_vtk(bulge, deep=True)
    arr.SetName(BULGE_ARRAY)
    out.GetPointData().AddArray(arr)
    out.GetPointData().SetActiveScalars(BULGE_ARRAY)

    stats = {
        "median": float(np.median(bulge)),
        "p90":    float(np.percentile(bulge, 90)),
        "p99":    float(np.percentile(bulge, 99)),
        "max":    float(bulge.max()),
    }
    log.info("Bulge field: median=%.2f p90=%.2f p99=%.2f max=%.2f",
             stats["median"], stats["p90"], stats["p99"], stats["max"])
    return out, stats


def clip_aneurysm_sac(surface, centerlines, seed_mm, ratio=1.4, progress_cb=None):
    """Clip the aneurysm sac from the parent vessel wall via the bulge field.

    Parameters
    ----------
    surface     : vtkPolyData — pre-extension vessel wall mesh
    centerlines : vtkPolyData — VMTK centerlines with MaximumInscribedSphereRadius
    seed_mm     : (x, y, z) in world mm — a point on/inside the aneurysm dome
    ratio       : float — bulge threshold; surface points with bulge > ratio are
                  dome candidates. Healthy wall ~1.0, dome ~1.5-2.5.
    progress_cb : optional callable(pct, msg)

    Returns
    -------
    dict with keys:
      'sac'           : vtkPolyData — aneurysm dome (open surface)
      'parent'        : vtkPolyData — parent vessel wall (open surface)
      'neck_plane'    : {'origin': [...], 'normal': [...]} or None
      'bulge_surface' : vtkPolyData — surface with the BulgeRatio scalar (heatmap)
      'stats'         : dict — bulge field statistics
      'ratio'         : float — the threshold actually used
    """
    def _prog(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    _prog(10, "Computing centerline bulge field...")
    bulge_surface, stats = compute_bulge_field(surface, centerlines)

    _prog(30, "Smoothing bulge field for a clean neck...")
    _smooth_point_scalar(bulge_surface, BULGE_ARRAY, iterations=5)

    if stats["max"] < ratio:
        raise RuntimeError(
            f"No surface bulge reaches the threshold {ratio:.2f} "
            f"(max bulge here is {stats['max']:.2f}). "
            f"Lower the threshold, e.g. clip-sac --ratio {max(stats['max'] - 0.1, 1.05):.2f}"
        )

    _prog(50, f"Clipping at bulge ratio {ratio:.2f}...")
    bulge_surface.GetPointData().SetActiveScalars(BULGE_ARRAY)
    clipper = vtk.vtkClipPolyData()
    clipper.SetInputData(bulge_surface)
    clipper.SetValue(ratio)
    clipper.GenerateClippedOutputOn()   # both sides
    clipper.Update()

    high_side = clipper.GetOutput()         # bulge > ratio → dome candidates
    low_side  = clipper.GetClippedOutput()  # bulge <= ratio → parent vessel

    if high_side.GetNumberOfCells() == 0:
        raise RuntimeError(
            f"Clip at ratio {ratio:.2f} produced no dome region. "
            "Lower the threshold and try again."
        )

    _prog(70, "Selecting the dome region nearest the seed...")
    sac, leftover_high = _select_seed_region(high_side, seed_mm)

    # Parent = low side + any high-bulge blobs that are NOT the dome (e.g. a
    # bulge at a tight bend) so nothing is lost from the vessel.
    parent = _append([low_side, leftover_high])

    if sac is None or sac.GetNumberOfCells() == 0:
        raise RuntimeError("Dome selection produced empty geometry — check the seed.")

    _prog(85, "Extracting neck plane geometry...")
    neck_plane = _extract_neck_plane(sac)

    _prog(95, "Cleaning clipped surfaces...")
    sac    = _clean(sac)
    parent = _clean(parent)

    _prog(100, "Done.")
    log.info("Sac: %d cells  |  Parent: %d cells  |  ratio=%.2f  |  Neck plane: %s",
             sac.GetNumberOfCells(), parent.GetNumberOfCells(), ratio, neck_plane)
    return {
        'sac': sac,
        'parent': parent,
        'neck_plane': neck_plane,
        'bulge_surface': bulge_surface,
        'stats': stats,
        'ratio': ratio,
    }


def export_bulge_heatmap(bulge_surface, path):
    """Write a colour-mapped PLY of the bulge field for visual inspection.

    BulgeRatio is mapped blue (low, ~1.0) → red (high, dome) into a per-vertex
    RGB array. PLY with vertex colours displays directly in MeshLab, Meshmixer
    and 3D Slicer with no filtering step.
    """
    arr = bulge_surface.GetPointData().GetArray(BULGE_ARRAY)
    if arr is None:
        raise RuntimeError("Surface has no BulgeRatio array to export")

    values = vtk_np.vtk_to_numpy(arr)
    # Map [1.0 .. p99] → [0 .. 1] so the colour range isn't dominated by a few
    # extreme apex points.
    lo = 1.0
    hi = max(float(np.percentile(values, 99)), lo + 1e-3)
    t = np.clip((values - lo) / (hi - lo), 0.0, 1.0)

    # Simple blue→cyan→green→yellow→red ramp.
    colors = np.zeros((len(t), 3), dtype=np.uint8)
    colors[:, 0] = np.clip(255 * (1.5 - np.abs(4 * t - 3)), 0, 255)  # R
    colors[:, 1] = np.clip(255 * (1.5 - np.abs(4 * t - 2)), 0, 255)  # G
    colors[:, 2] = np.clip(255 * (1.5 - np.abs(4 * t - 1)), 0, 255)  # B

    col_arr = vtk_np.numpy_to_vtk(colors, deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
    col_arr.SetName("Colors")
    col_arr.SetNumberOfComponents(3)

    colored = vtk.vtkPolyData()
    colored.DeepCopy(bulge_surface)
    colored.GetPointData().SetScalars(col_arr)

    writer = vtk.vtkPLYWriter()
    writer.SetFileName(path)
    writer.SetInputData(colored)
    writer.SetFileTypeToBinary()
    writer.SetArrayName("Colors")
    writer.SetColorModeToDefault()
    writer.Write()
    log.info("Bulge heatmap written: %s", path)
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _smooth_point_scalar(surface, array_name, iterations=5):
    """Laplacian-smooth a point-data scalar over the mesh edges (in place).

    Averages each point's value with its edge neighbours, a handful of times,
    so the clip iso-contour (the neck) is smooth rather than jagged.
    """
    arr = surface.GetPointData().GetArray(array_name)
    if arr is None:
        return
    values = vtk_np.vtk_to_numpy(arr).astype(np.float64).copy()
    n = surface.GetNumberOfPoints()

    # Build neighbour lists from triangle cells.
    neighbours = [set() for _ in range(n)]
    id_list = vtk.vtkIdList()
    for cid in range(surface.GetNumberOfCells()):
        surface.GetCellPoints(cid, id_list)
        ids = [id_list.GetId(i) for i in range(id_list.GetNumberOfIds())]
        for a in ids:
            for b in ids:
                if a != b:
                    neighbours[a].add(b)

    nbr_arr = [np.fromiter(s, dtype=np.int64) for s in neighbours]

    for _ in range(iterations):
        new = values.copy()
        for i in range(n):
            nb = nbr_arr[i]
            if len(nb):
                new[i] = 0.5 * values[i] + 0.5 * values[nb].mean()
        values = new

    smoothed = vtk_np.numpy_to_vtk(values, deep=True)
    smoothed.SetName(array_name)
    surface.GetPointData().AddArray(smoothed)
    surface.GetPointData().SetActiveScalars(array_name)


def _select_seed_region(high_side, seed_mm):
    """Split *high_side* into (dome, leftover).

    dome = the connected region whose centroid is nearest seed_mm.
    leftover = all other high-bulge regions appended together (may be empty).
    """
    # vtkClipPolyData leaves orphan points; clean them so the connectivity
    # output's points and RegionId array stay the same length.
    clean = vtk.vtkCleanPolyData()
    clean.SetInputData(high_side)
    clean.Update()
    hi = clean.GetOutput()

    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(hi)
    conn.SetExtractionModeToAllRegions()
    conn.ColorRegionsOn()
    conn.Update()
    n_regions = conn.GetNumberOfExtractedRegions()
    colored = conn.GetOutput()

    if n_regions <= 1:
        return hi, vtk.vtkPolyData()

    # ColorRegions writes "RegionId" into POINT data; after cleaning, it is
    # aligned with the output points.
    pt_region_arr = colored.GetPointData().GetArray("RegionId")
    if pt_region_arr is None:
        return hi, vtk.vtkPolyData()
    pt_region = vtk_np.vtk_to_numpy(pt_region_arr)
    pts = vtk_np.vtk_to_numpy(colored.GetPoints().GetData())

    # Dome region = the one whose centroid is nearest the seed.
    seed = np.array(seed_mm)
    best_rid, best_d = 0, float("inf")
    for rid in range(n_regions):
        rp = pts[pt_region == rid]
        if len(rp) == 0:
            continue
        d = np.linalg.norm(rp.mean(axis=0) - seed)
        if d < best_d:
            best_d, best_rid = d, rid

    def _extract(rid):
        thr = vtk.vtkThreshold()
        thr.SetInputData(colored)
        thr.SetInputArrayToProcess(
            0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, "RegionId")
        thr.SetLowerThreshold(rid)
        thr.SetUpperThreshold(rid)
        thr.Update()
        geom = vtk.vtkGeometryFilter()
        geom.SetInputData(thr.GetOutput())
        geom.Update()
        return geom.GetOutput()

    dome = _extract(best_rid)
    leftover_pieces = [_extract(rid) for rid in range(n_regions) if rid != best_rid]
    return dome, _append(leftover_pieces)


def _append(polys):
    """Append a list of polydata into one. Empty list → empty polydata."""
    polys = [p for p in polys if p is not None and p.GetNumberOfCells() > 0]
    if not polys:
        return vtk.vtkPolyData()
    if len(polys) == 1:
        return polys[0]
    app = vtk.vtkAppendPolyData()
    for p in polys:
        app.AddInputData(p)
    app.Update()
    return app.GetOutput()


def _extract_neck_plane(sac_poly):
    """Extract neck plane from the boundary ring of the clipped sac surface.

    Uses vtkFeatureEdges to find the open boundary, then:
      - origin = centroid of boundary ring points
      - normal = smallest PCA eigenvector (perpendicular to the ring plane)

    Returns {'origin': list, 'normal': list} or None if no boundary found.
    """
    boundary = vtk.vtkFeatureEdges()
    boundary.SetInputData(sac_poly)
    boundary.BoundaryEdgesOn()
    boundary.FeatureEdgesOff()
    boundary.ManifoldEdgesOff()
    boundary.NonManifoldEdgesOff()
    boundary.Update()

    b_out = boundary.GetOutput()
    if b_out.GetNumberOfPoints() < 3:
        log.warning("No neck boundary ring found — neck_plane will be None")
        return None

    pts = vtk_np.vtk_to_numpy(b_out.GetPoints().GetData())
    origin = pts.mean(axis=0)

    centered = pts - origin
    cov = np.cov(centered.T)
    eigenvalues, eigenvectors = np.linalg.eigh(cov)
    normal = eigenvectors[:, 0]  # smallest eigenvalue → plane normal

    return {
        'origin': [float(v) for v in origin],
        'normal': [float(v) for v in normal],
    }


def _clean(poly):
    """Remove duplicate points and degenerate cells."""
    c = vtk.vtkCleanPolyData()
    c.SetInputData(poly)
    c.Update()
    return c.GetOutput()
