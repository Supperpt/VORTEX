"""Aneurysm sac clipping for VORTEX.

Splits the vessel wall surface into an aneurysm dome patch and a parent vessel
patch by detecting the neck plane. Primary method: VMTK vmtkSurfaceClipper
(Piccinelli 2009 centerline-based automatic detection). Fallback: VTK sphere
clip centred on the seed point.

Entry point:
  clip_aneurysm_sac(surface, centerlines, seed_mm, progress_cb) → dict
  Returns {'sac': vtkPolyData, 'parent': vtkPolyData, 'neck_plane': dict}
"""

import logging
import numpy as np

from vortex.utils.vtk_compat import vtk, vtk_np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def clip_aneurysm_sac(surface, centerlines, seed_mm, progress_cb=None):
    """Clip the aneurysm sac from the parent vessel wall.

    Tries VMTK's automatic Piccinelli clipper first. Falls back to a sphere
    clip centred on *seed_mm* if VMTK is unavailable or returns empty geometry.

    Parameters
    ----------
    surface     : vtkPolyData — pre-extension vessel wall mesh
    centerlines : vtkPolyData — VMTK centerlines with MaximumInscribedSphereRadius
    seed_mm     : (x, y, z) in world mm — point inside the aneurysm dome
    progress_cb : optional callable(pct, msg)

    Returns
    -------
    dict with keys:
      'sac'        : vtkPolyData — aneurysm dome (open surface)
      'parent'     : vtkPolyData — parent vessel wall (open surface)
      'neck_plane' : {'origin': [x,y,z], 'normal': [nx,ny,nz]} or None
    """
    def _prog(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    sac = parent = None

    # ── Attempt 1: VMTK automatic clipper ────────────────────────────────────
    _prog(5, "Trying VMTK automatic neck detection (Piccinelli method)...")
    try:
        sac, parent = _vmtk_clip(surface, centerlines, seed_mm, _prog)
    except Exception as e:
        log.warning("VMTK surface clipper failed (%s). Falling back to sphere clip.", e)
        sac = parent = None

    # ── Attempt 2: VTK sphere fallback ───────────────────────────────────────
    if sac is None or sac.GetNumberOfPoints() == 0:
        _prog(30, "Falling back to sphere clip around seed point...")
        try:
            radius = _estimate_sac_radius(surface, seed_mm)
            sac, parent = _sphere_clip_fallback(surface, seed_mm, radius)
            log.info("Sphere clip used: centre=%s radius=%.1f mm", seed_mm, radius)
        except Exception as e:
            raise RuntimeError(f"Both VMTK and sphere clip failed: {e}") from e

    if sac is None or sac.GetNumberOfPoints() == 0:
        raise RuntimeError(
            "Sac clip produced empty geometry. Check that the seed point is "
            "inside the aneurysm dome and that centerlines are computed."
        )

    _prog(70, "Extracting neck plane geometry...")
    neck_plane = _extract_neck_plane(sac)

    _prog(90, "Cleaning clipped surfaces...")
    sac    = _clean(sac)
    parent = _clean(parent)

    _prog(100, "Done.")
    log.info("Sac: %d pts  |  Parent: %d pts  |  Neck plane: %s",
             sac.GetNumberOfPoints(), parent.GetNumberOfPoints(), neck_plane)
    return {'sac': sac, 'parent': parent, 'neck_plane': neck_plane}


# ---------------------------------------------------------------------------
# VMTK automatic clip
# ---------------------------------------------------------------------------

def _vmtk_clip(surface, centerlines, seed_mm, progress_cb):
    """Try vmtkSurfaceClipper. Returns (sac, parent) or raises."""
    from vmtk import vmtkscripts  # raises ImportError if VMTK unavailable

    clipper = None
    for cls_name in ("vmtkSurfaceClipper",):
        cls = getattr(vmtkscripts, cls_name, None)
        if cls is not None:
            clipper = cls()
            break

    if clipper is None:
        raise RuntimeError("vmtkSurfaceClipper not found in vmtkscripts")

    clipper.Surface = surface
    clipper.Centerlines = centerlines
    clipper.Execute()

    poly_a = clipper.Surface
    poly_b = getattr(clipper, 'ClippedSurface', None)

    if poly_b is None or poly_b.GetNumberOfPoints() == 0:
        raise RuntimeError("vmtkSurfaceClipper did not produce a complement surface")

    return _identify_sac(poly_a, poly_b, seed_mm)


# ---------------------------------------------------------------------------
# Sphere-clip fallback
# ---------------------------------------------------------------------------

def _estimate_sac_radius(surface, seed_mm):
    """Estimate sac radius from point distances around the seed."""
    pts = vtk_np.vtk_to_numpy(surface.GetPoints().GetData())
    cx, cy, cz = seed_mm
    dists = np.sqrt((pts[:, 0] - cx) ** 2 +
                    (pts[:, 1] - cy) ** 2 +
                    (pts[:, 2] - cz) ** 2)
    rough_r = float(np.percentile(dists, 50))
    local = pts[dists < rough_r * 3]
    if len(local) < 10:
        return rough_r * 1.5
    dims = local.max(axis=0) - local.min(axis=0)
    max_diam = float(dims.max())
    return max_diam * 0.6   # 60% of max bounding-box diameter


def _sphere_clip_fallback(surface, seed_mm, radius):
    """Clip with a sphere centred on seed_mm.

    vtkClipPolyData removes where F > 0. For vtkSphere:
      F < 0  inside sphere  → GetOutput()        = sac region
      F > 0  outside sphere → GetClippedOutput() = parent vessel
    """
    sphere = vtk.vtkSphere()
    sphere.SetCenter(*seed_mm)
    sphere.SetRadius(radius)

    clipper = vtk.vtkClipPolyData()
    clipper.SetInputData(surface)
    clipper.SetClipFunction(sphere)
    clipper.GenerateClippedOutputOn()
    clipper.Update()

    inside  = clipper.GetOutput()          # inside sphere = sac
    outside = clipper.GetClippedOutput()   # outside sphere = parent

    return _identify_sac(inside, outside, seed_mm)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _identify_sac(poly_a, poly_b, seed_mm):
    """Return (sac, parent) — the region whose centroid is closest to seed_mm."""
    seed = np.array(seed_mm)

    def centroid(poly):
        if poly is None or poly.GetNumberOfPoints() == 0:
            return np.full(3, float('inf'))
        return vtk_np.vtk_to_numpy(poly.GetPoints().GetData()).mean(axis=0)

    dist_a = np.linalg.norm(centroid(poly_a) - seed)
    dist_b = np.linalg.norm(centroid(poly_b) - seed)

    if dist_a <= dist_b:
        return poly_a, poly_b
    return poly_b, poly_a


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
