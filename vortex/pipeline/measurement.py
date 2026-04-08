"""Measurement helpers for the slice viewer.

Provides distance measurement between two points in world coordinates, and
basic aneurysm geometry estimation from a vtkPolyData surface patch.
"""

import math
import numpy as np


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------

def measure_line(p1: tuple, p2: tuple) -> float:
    """Euclidean distance between two world-coordinate points (mm).

    Parameters
    ----------
    p1, p2 : (x, y, z) in mm

    Returns
    -------
    float  distance in mm
    """
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))


def ijk_distance(p1_ijk: tuple, p2_ijk: tuple, spacing: tuple) -> float:
    """Distance between two image-index points, accounting for voxel spacing.

    Parameters
    ----------
    p1_ijk, p2_ijk : (i, j, k) image index coords
    spacing        : (sx, sy, sz) voxel spacing in mm

    Returns
    -------
    float  distance in mm
    """
    dx = (p2_ijk[0] - p1_ijk[0]) * spacing[0]
    dy = (p2_ijk[1] - p1_ijk[1]) * spacing[1]
    dz = (p2_ijk[2] - p1_ijk[2]) * spacing[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


# ---------------------------------------------------------------------------
# Aneurysm geometry
# ---------------------------------------------------------------------------

def estimate_aneurysm_geometry(surface, center_mm: tuple) -> dict:
    """Estimate basic aneurysm dimensions from a surface mesh near a centre point.

    Uses the bounding box of surface points within 3× the estimated radius.
    Returns a dict with keys: max_diameter, height, neck_estimate (all in mm).

    Parameters
    ----------
    surface    : vtk.vtkPolyData
    center_mm  : (x, y, z) seed point in world mm

    Returns
    -------
    dict with float values (mm), or empty dict if surface has no points
    """
    from vortex.utils.vtk_compat import vtk_np
    import vtk as _vtk  # imported locally to avoid circular deps

    n = surface.GetNumberOfPoints()
    if n == 0:
        return {}

    pts = vtk_np.vtk_to_numpy(surface.GetPoints().GetData())  # (N, 3)
    cx, cy, cz = center_mm

    # Rough distance from centre to all surface points
    dists = np.sqrt(
        (pts[:, 0] - cx) ** 2 +
        (pts[:, 1] - cy) ** 2 +
        (pts[:, 2] - cz) ** 2
    )

    # Take the mean of the 20th percentile as a rough radius estimate
    rough_radius = float(np.percentile(dists, 50))

    # Keep points within 3× that radius (aneurysm neighbourhood)
    mask = dists < rough_radius * 3
    local = pts[mask]

    if len(local) < 10:
        return {}

    # Bounding box dimensions
    bb_min = local.min(axis=0)
    bb_max = local.max(axis=0)
    dims   = bb_max - bb_min  # (dx, dy, dz)

    max_diameter = float(dims.max())
    height       = float(dims.min())   # smallest dimension ≈ neck-to-dome
    neck_estimate = float(max_diameter * 0.4)

    return {
        "max_diameter_mm": round(max_diameter, 2),
        "height_mm":       round(height, 2),
        "neck_estimate_mm": round(neck_estimate, 2),  # rough heuristic
        "aspect_ratio":    round(height / neck_estimate, 2) if neck_estimate > 0 else 0.0,
        "size_ratio":      round(max_diameter / neck_estimate, 2) if neck_estimate > 0 else 0.0,
    }
