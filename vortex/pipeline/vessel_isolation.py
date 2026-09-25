"""Seed-driven vessel isolation for VORTEX.

Trims a segmented vascular tree down to the aneurysm plus a short, controlled
length of every vessel attached to it, so a case can go to CFD without a manual
pass in MeshLab/Meshmixer.

The governing geometric facts, all of which `sac_clipping.py` also relies on:

  * The aneurysm is a dead-end bulge with no opening, so a source-to-target
    centerline runs opening-to-opening through the lumen and never enters the
    dome.  The centerline point nearest a seed placed in the dome is therefore
    the foot of the perpendicular onto the parent axis -- geometrically, the
    neck.  That is what makes a single seed enough to find the parent vessel.

  * VMTK centerlines carry MaximumInscribedSphereRadius, the local vessel
    radius.  So "5 vessel diameters" is measurable directly off the centerline
    rather than being a number the user has to guess per case.

  * Distance along the vessel is geodesic distance on the centerline graph, not
    straight-line distance.  Only the former respects branching: two points on
    opposite daughters of a bifurcation can be close in space while being far
    apart along the vessel.

Pipeline, in order (the order matters -- see scaffold_clip):

    1. scaffold_clip          box-clip around the seed; opens sealed ends
    2. prepare_isolation_centerlines   tear cull, then centerlines
    3. build_centerline_graph + geodesic_from_node
    4. find_neck_anchor
    5. estimate_parent_diameter -> threshold
    6. find_branch_cuts        one cut per branch crossing the threshold
    7. apply_cuts              clip + keep the seed's connected region

Entry point:
    isolate_aneurysm_region(surface, seed_mm, iso_params, ...) -> dict

The centerlines computed here are scaffolding for locating the cuts.  They are
NOT reusable downstream: `isolate` creates new openings that did not exist when
they were computed, `extend` derives flow-extension placement from where
centerlines terminate, and `clip-sac` assumes the centerline and surface
describe the same geometry.  Re-run `centerlines` after `isolate`.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import dijkstra, connected_components

from vortex.utils.vtk_compat import vtk, vtk_np
from vortex.pipeline.centerlines import (
    compute_centerlines,
    _detect_boundary_profiles,
    _cull_tear_loops,
)

log = logging.getLogger(__name__)

_MISR_ARRAY    = "MaximumInscribedSphereRadius"
_TANGENT_ARRAY = "FrenetTangent"
_MERGE_TOL     = 1e-4     # mm — coordinate rounding when merging centerline points

# A measured parent diameter above this is not a vessel; it is skull or soft
# tissue. Reference: a whole-head segmentation measured 12.78 mm median MISR
# against 0.93 mm for a real cerebral artery.
_MAX_VESSEL_DIAMETER_MM = 12.0


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

@dataclass
class CenterlineGraph:
    points:    np.ndarray        # (M,3) merged node coordinates
    misr:      np.ndarray        # (M,)  local vessel radius per node
    tangent:   np.ndarray        # (M,3) unit tangents
    adjacency: object            # (M,M) scipy csr, symmetric, distance-weighted
    degree:    np.ndarray        # (M,)  neighbour count


@dataclass
class CutPlane:
    origin:   tuple
    normal:   tuple              # unit, oriented TOWARD the anchor
    misr:     float
    node:     int
    geodesic: float


@dataclass
class AnchorInfo:
    node:          int
    point:         tuple
    misr:          float
    seed_distance: float         # |seed - anchor|, a proxy for sac height
    retracted:     bool = False


# ---------------------------------------------------------------------------
# Step 1 — scaffold clip
# ---------------------------------------------------------------------------

def scaffold_clip(surface, seed_mm, radius_mm, inset_mm=0.6):
    """Clip *surface* to a working region around the seed, opening sealed ends.

    Two jobs in one pass:

    1. **It opens vessel ends that segmentation sealed.**  Marching cubes on an
       ROI-cropped volume sometimes caps a vessel where it meets the crop
       boundary instead of leaving it open, intermittently, depending on how
       the vessel meets the box.  Because this clip cuts through solid
       geometry, such an end comes out as a clean planar opening regardless.
       Measured: AA_001 goes from 1 usable opening to 4.

    2. **It drops distant anatomy**, so centerlines are fast and the boundary
       loop count collapses.

    The box is the seed-centred box of half-width *radius_mm*, intersected with
    the mesh bounds shrunk by *inset_mm*.  Only geometry within *inset_mm* of a
    bounding-box extreme is touched; everything deeper passes through
    untouched.

    Side effect the caller must handle: where the wall is merely *tangent* to
    the bounding box rather than cut by the ROI, the inset shaves a sliver and
    leaves a spurious sub-millimetre hole.  Run _cull_tear_loops afterwards and
    before counting openings.  Real vessel cuts measure 1.02-2.88 mm in radius
    across the reference cases; slivers come out under 0.5 mm.
    """
    b = list(surface.GetBounds())
    s = np.asarray(seed_mm, dtype=float)
    d = float(inset_mm)

    lo, hi = [], []
    for ax in range(3):
        bmin, bmax = b[2 * ax], b[2 * ax + 1]
        # Seed-centred window, then never exceed the mesh bounds shrunk by the
        # inset. max/min keep the box valid when the mesh is thinner than 2*d.
        a = max(s[ax] - radius_mm, bmin + d)
        c = min(s[ax] + radius_mm, bmax - d)
        if c <= a:                      # degenerate axis: fall back to the raw span
            a, c = bmin, bmax
        lo.append(a)
        hi.append(c)

    box = vtk.vtkBox()
    box.SetBounds(lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])

    clipper = vtk.vtkClipPolyData()
    clipper.SetInputData(surface)
    clipper.SetClipFunction(box)
    clipper.InsideOutOn()               # keep what is inside the box
    clipper.Update()

    kept = _seed_region(clipper.GetOutput(), seed_mm)
    out = _triangulate_and_clean(kept)
    log.info("Scaffold clip (r=%.1f mm, inset=%.2f mm): %d -> %d cells",
             radius_mm, inset_mm, surface.GetNumberOfCells(), out.GetNumberOfCells())
    return out


# ---------------------------------------------------------------------------
# Step 2 — centerlines
# ---------------------------------------------------------------------------

def prepare_isolation_centerlines(surface, iso_params, progress_cb=None):
    """-> (centerlines, engine) where engine is 'profiles' or 'network'.

    Culls tear loops first, then counts real openings.  Two or more takes the
    normal fast path.  Fewer means the surface is effectively sealed, which the
    scaffold clip should usually have prevented, so warn loudly and fall back
    to the seedless network skeletoniser.
    """
    def _prog(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)

    tear = float(getattr(iso_params, "tear_radius_mm", 1.0))
    culled = _cull_tear_loops(surface, tear) if tear > 0 else surface
    profiles = [p for p in _detect_boundary_profiles(culled) if p["radius_mm"] >= 0.5]

    if len(profiles) >= 2:
        _prog(20, f"Computing centerlines from {len(profiles)} openings...")
        centerlines, _ = compute_centerlines(culled, progress_cb=None,
                                             cull_tears_mm=tear)
        return centerlines, "profiles"

    log.warning(
        "Only %d usable opening(s) after the scaffold clip — the surface is "
        "effectively sealed. Falling back to the seedless network "
        "skeletoniser, which is slower.", len(profiles))
    _prog(20, "Surface is sealed — using the slower network skeletoniser...")
    return _centerlines_network(culled, iso_params, _prog), "network"


def _centerlines_network(surface, iso_params, prog):
    """Seedless centerlines for a closed surface via vmtkCenterlinesNetwork.

    Needs no source/target, which is the whole point, but it is slow: 318 s on
    148k triangles in testing. Decimate first -- we consume only point
    positions, radius and tangent, none of which need the full triangle
    density, and the result is then used against the full-resolution surface.
    """
    from vmtk import vmtkscripts

    work = surface
    target = float(getattr(iso_params, "decimate_target", 0.7))
    if 0.0 < target < 1.0 and surface.GetNumberOfCells() > 20000:
        dec = vtk.vtkDecimatePro()
        dec.SetInputData(surface)
        dec.SetTargetReduction(target)
        dec.PreserveTopologyOn()
        dec.Update()
        work = _triangulate_and_clean(dec.GetOutput())
        log.info("Decimated %d -> %d cells for the network skeletoniser",
                 surface.GetNumberOfCells(), work.GetNumberOfCells())

    prog(35, "Skeletonising (this can take several minutes)...")
    net = vmtkscripts.vmtkCenterlinesNetwork()
    net.Surface = work
    net.Execute()
    centerlines = net.Centerlines

    if centerlines is None or centerlines.GetNumberOfPoints() == 0:
        raise RuntimeError(
            "The network skeletoniser produced no centerlines. The surface may "
            "be badly non-manifold — inspect it with 'check-mesh'."
        )

    # The network output carries only EdgeArray, EdgePCoordArray and MISR, so
    # there are no Frenet tangents. Try to attach them, but expect to fall back
    # to finite differences, which is why that helper is the default path.
    try:
        geo = vmtkscripts.vmtkCenterlineGeometry()
        geo.Centerlines = centerlines
        geo.Execute()
        if geo.Centerlines.GetPointData().GetArray(_TANGENT_ARRAY) is not None:
            centerlines = geo.Centerlines
    except Exception as exc:
        log.info("vmtkCenterlineGeometry unavailable on network output (%s); "
                 "using finite-difference tangents", exc)
    return centerlines


# ---------------------------------------------------------------------------
# Step 3 — graph and geodesic distance
# ---------------------------------------------------------------------------

def build_centerline_graph(centerlines, merge_tol=_MERGE_TOL) -> CenterlineGraph:
    """Build a merged, distance-weighted graph over the centerline points.

    VMTK emits one polyline per source-target pair, so the shared trunk is
    duplicated across several lines.  Merging coincident points collapses those
    into single nodes, which is what makes the result a graph rather than a
    bundle of parallel paths, and is what lets one Dijkstra run reach every
    branch.  Verified: 592 points collapse to 424 nodes in one component.
    """
    pts = vtk_np.vtk_to_numpy(centerlines.GetPoints().GetData()).astype(float)
    if len(pts) == 0:
        raise RuntimeError("Centerlines contain no points")

    keys = np.round(pts / merge_tol).astype(np.int64)
    _, first_idx, inverse = np.unique(keys, axis=0, return_index=True,
                                      return_inverse=True)
    inverse = np.asarray(inverse).ravel()
    n = len(first_idx)
    nodes = pts[first_idx]

    misr_arr = centerlines.GetPointData().GetArray(_MISR_ARRAY)
    if misr_arr is None:
        raise RuntimeError(
            "Centerlines carry no MaximumInscribedSphereRadius array, so the "
            "vessel diameter cannot be measured."
        )
    src_misr = vtk_np.vtk_to_numpy(misr_arr).astype(float)
    misr = np.zeros(n)
    np.maximum.at(misr, inverse, src_misr)

    tangent = _node_tangents(centerlines, pts, inverse, n)

    rows, cols, weights = [], [], []
    lines = centerlines.GetLines()
    lines.InitTraversal()
    ids = vtk.vtkIdList()
    while lines.GetNextCell(ids):
        raw = [ids.GetId(k) for k in range(ids.GetNumberOfIds())]
        if len(raw) < 2:
            continue
        a = inverse[raw]
        seg = np.linalg.norm(np.diff(pts[raw], axis=0), axis=1)
        for u, v, w in zip(a[:-1], a[1:], seg):
            if u != v:                      # drop self-edges from merging
                rows.append(u); cols.append(v); weights.append(float(w))

    if not rows:
        raise RuntimeError("Centerlines contain no usable segments")

    adj = sp.coo_matrix((weights + weights, (rows + cols, cols + rows)),
                        shape=(n, n)).tocsr()
    degree = np.diff(adj.indptr)

    ncomp, _ = connected_components(adj, directed=False)
    log.info("Centerline graph: %d points -> %d nodes, %d component(s), "
             "%d bifurcation node(s)", len(pts), n, ncomp, int((degree >= 3).sum()))
    return CenterlineGraph(nodes, misr, tangent, adj, degree)


def _node_tangents(centerlines, pts, inverse, n):
    """Per-node unit tangents, from Frenet if present else finite differences.

    Signs can disagree where several polylines meet, so each contribution is
    flipped to agree with the first one seen at that node before averaging;
    otherwise opposing tangents would cancel to zero at every shared point.
    """
    arr = centerlines.GetPointData().GetArray(_TANGENT_ARRAY)
    if arr is not None:
        src = vtk_np.vtk_to_numpy(arr).astype(float)
    else:
        src = np.zeros_like(pts)
        lines = centerlines.GetLines()
        lines.InitTraversal()
        ids = vtk.vtkIdList()
        while lines.GetNextCell(ids):
            raw = [ids.GetId(k) for k in range(ids.GetNumberOfIds())]
            if len(raw) < 2:
                continue
            p = pts[raw]
            t = np.zeros_like(p)
            t[1:-1] = p[2:] - p[:-2]        # central difference
            t[0]    = p[1] - p[0]           # forward at the start
            t[-1]   = p[-1] - p[-2]         # backward at the end
            src[raw] = t

    acc  = np.zeros((n, 3))
    ref  = np.zeros((n, 3))
    seen = np.zeros(n, dtype=bool)
    for i, node in enumerate(inverse):
        v = src[i]
        if not seen[node]:
            ref[node] = v
            seen[node] = True
        if float(np.dot(v, ref[node])) < 0.0:
            v = -v
        acc[node] += v

    norms = np.linalg.norm(acc, axis=1)
    good = norms > 1e-9
    acc[good] /= norms[good][:, None]
    return acc


def geodesic_from_node(graph: CenterlineGraph, node: int):
    """-> (distances, predecessors) along the vessel from *node*."""
    dist, pred = dijkstra(graph.adjacency, directed=False, indices=node,
                          return_predecessors=True)
    return np.asarray(dist).ravel(), np.asarray(pred).ravel()


# ---------------------------------------------------------------------------
# Step 4 — neck anchor
# ---------------------------------------------------------------------------

def find_neck_anchor(graph: CenterlineGraph, seed_mm, engine="profiles",
                     retract=True) -> AnchorInfo:
    """Locate the centerline node at the aneurysm neck.

    The nearest node to the seed is the neck, because a source-to-target
    centerline never enters the dome (it is a dead end with no opening), so the
    closest point on it is the foot of the perpendicular from the dome onto the
    parent axis.

    The network engine breaks that guarantee: a seedless skeletoniser does push
    a short dead-end branch into the dome. Only in that case, walk back out of
    the sac onto the trunk.
    """
    seed = np.asarray(seed_mm, dtype=float)
    d = np.linalg.norm(graph.points - seed, axis=1)
    node = int(np.argmin(d))
    seed_distance = float(d[node])

    if seed_distance > 20.0:
        raise RuntimeError(
            f"The seed is {seed_distance:.1f} mm from the nearest vessel "
            f"centerline. That usually means the seed and the mesh are in "
            f"different coordinate frames — check the seed came from this "
            f"mesh's world coordinates and not from voxel indices."
        )

    retracted = False
    if retract and engine == "network":
        node, retracted = _retract_to_trunk(graph, node, seed, seed_distance)

    return AnchorInfo(node=node, point=tuple(graph.points[node]),
                      misr=float(graph.misr[node]),
                      seed_distance=seed_distance, retracted=retracted)


def _retract_to_trunk(graph, node, seed, budget, max_steps=200):
    """Walk away from the seed to the first junction, for network centerlines.

    Stops at a node of degree >= 3 (the neck/bifurcation), or once it has
    walked further than the seed was from the starting node, whichever is
    first. Returns the original node unchanged if neither happens -- a sidewall
    aneurysm on a straight segment has no junction and the naive anchor is
    already right.
    """
    cur, walked, visited = node, 0.0, {node}
    for _ in range(max_steps):
        if graph.degree[cur] >= 3:
            return cur, cur != node
        nbrs = graph.adjacency.indices[
            graph.adjacency.indptr[cur]:graph.adjacency.indptr[cur + 1]]
        cand = [x for x in nbrs if x not in visited]
        if not cand:
            break
        nxt = max(cand, key=lambda x: float(np.linalg.norm(graph.points[x] - seed)))
        walked += float(np.linalg.norm(graph.points[nxt] - graph.points[cur]))
        if walked > max(budget, 1e-6):
            break
        visited.add(nxt)
        cur = nxt
    return node, False


# ---------------------------------------------------------------------------
# Step 5 — parent diameter and threshold
# ---------------------------------------------------------------------------

def estimate_parent_diameter(graph: CenterlineGraph, dist, anchor: AnchorInfo) -> float:
    """Measure the parent vessel diameter just clear of the neck.

    Not simply 2*MISR at the anchor: the inscribed sphere at the neck bulges
    into the sac and overestimates.  Take the median over a band one to three
    anchor radii along the vessel, which is past the neck but still local.
    """
    r = max(float(anchor.misr), 1e-3)
    band = np.isfinite(dist) & (dist >= 1.0 * r) & (dist <= 3.0 * r)
    d = 2.0 * float(np.median(graph.misr[band])) if band.any() else 2.0 * r
    if not band.any():
        log.warning("No centerline nodes 1-3 radii from the neck; "
                    "falling back to the anchor radius for the diameter")
    return d


def _check_vessel_like(diameter_mm: float) -> None:
    if diameter_mm > _MAX_VESSEL_DIAMETER_MM:
        raise RuntimeError(
            f"Measured local vessel diameter is {diameter_mm:.1f} mm, which is "
            f"not vessel-like (a cerebral artery is roughly 2-4 mm). The mesh "
            f"most likely contains skull or soft tissue rather than lumen.\n"
            f"Re-run 'segment' with a tighter roi_radius or a higher HU "
            f"threshold, or clean the mesh externally first."
        )


# ---------------------------------------------------------------------------
# Step 6 — find the cuts
# ---------------------------------------------------------------------------

def find_branch_cuts(graph: CenterlineGraph, dist, pred, threshold_mm) -> list:
    """One CutPlane per branch crossing *threshold_mm* from the anchor.

    Scans graph *edges*, not nodes: an edge with one end inside the threshold
    and the other outside is a branch crossing the frontier, and there is
    exactly one such edge per branch.  So a bifurcation yields one cut per
    daughter automatically, with no selection heuristic that could silently
    drop a branch.  This is what keeps bifurcation aneurysms intact.
    """
    adj = graph.adjacency
    cuts = []
    for u in range(adj.shape[0]):
        if not np.isfinite(dist[u]) or dist[u] > threshold_mm:
            continue
        for v in adj.indices[adj.indptr[u]:adj.indptr[u + 1]]:
            if not np.isfinite(dist[v]) or dist[v] <= threshold_mm:
                continue
            pu, pv = graph.points[u], graph.points[v]
            span = dist[v] - dist[u]
            t = float((threshold_mm - dist[u]) / span) if span > 1e-9 else 0.5
            origin = pu + t * (pv - pu)     # sub-step accurate, not quantised

            normal = graph.tangent[v].copy()
            if np.linalg.norm(normal) < 0.5:
                normal = pu - pv            # degenerate tangent: use the edge
            # Orient toward the anchor so "beyond the plane" means downstream.
            back = pu - pv
            p = int(pred[v])
            if p >= 0:
                back = graph.points[p] - pv
            if float(np.dot(normal, back)) < 0.0:
                normal = -normal
            nn = np.linalg.norm(normal)
            if nn < 1e-9:
                continue
            cuts.append(CutPlane(origin=tuple(origin), normal=tuple(normal / nn),
                                 misr=float(graph.misr[v]), node=int(v),
                                 geodesic=float(dist[v])))

    return _dedupe_cuts(cuts)


def _dedupe_cuts(cuts):
    """Drop near-duplicate cuts left by polylines running briefly in parallel."""
    kept = []
    for c in cuts:
        o, n = np.asarray(c.origin), np.asarray(c.normal)
        dup = False
        for k in kept:
            near = np.linalg.norm(o - np.asarray(k.origin)) < 0.5 * min(c.misr, k.misr)
            aligned = abs(float(np.dot(n, np.asarray(k.normal)))) > 0.966  # ~15 deg
            if near and aligned:
                dup = True
                break
        if not dup:
            kept.append(c)
    return kept


# ---------------------------------------------------------------------------
# Step 7 — apply the cuts
# ---------------------------------------------------------------------------

def _cross_section_radius(points, origin, normal, misr):
    """Measured in-plane radius of the vessel at a cut.

    MISR is the *inscribed* sphere radius, so it understates the cross-section
    wherever the plane meets the vessel obliquely or the lumen is non-circular.
    Measured on a real cut: MISR 1.24 mm against an actual rim reaching 3.34 mm,
    a factor of 2.7. Sizing the localisation sphere off MISR alone therefore
    lets the sphere slice through the rim, which is what makes the opening
    non-planar.

    Takes surface points in a thin slab either side of the plane and returns
    how far they reach from the cut centre.
    """
    o = np.asarray(origin, dtype=float)
    n = np.asarray(normal, dtype=float)
    d = points - o
    along = d @ n
    inplane = np.linalg.norm(d - np.outer(along, n), axis=1)
    slab = max(0.5 * misr, 0.3)
    # Bound the search so a neighbouring branch crossing the same slab cannot
    # inflate the measurement.
    m = (np.abs(along) < slab) & (inplane < 6.0 * misr)
    return float(inplane[m].max()) if m.any() else float(misr)


def apply_cuts(surface, cuts, seed_mm, sphere_factor=2.5):
    """Remove everything beyond each cut, keeping the seed's connected region.

    A plane is infinite, and that is the one real trap here.  A cut placed on
    one daughter of a bifurcation would also slice the other daughter, the
    parent, or the dome, wherever they fall on its negative side.  Intersecting
    half-spaces is worse: that only ever describes a convex region, and a
    vessel tree is not convex.

    So every cut is localised to a sphere of a few local radii around it.  The
    removal region is (beyond the plane) AND (inside the sphere), unioned
    across cuts, evaluated in a single clip pass.  Inside the sphere the clip
    surface *is* the plane, so each opening comes out genuinely planar, which
    is what vmtkFlowExtensions needs.  The curved part of the boundary only
    ever appears on the discarded side.

    VTK convention: negative is inside, INTERSECTION takes the max, UNION the
    min.  The plane normal points toward the anchor, so its function is
    negative downstream of the cut, which is the material to remove.
    """
    if not cuts:
        return _triangulate_and_clean(surface)

    pts = vtk_np.vtk_to_numpy(surface.GetPoints().GetData()).astype(float)

    removal = vtk.vtkImplicitBoolean()
    removal.SetOperationTypeToUnion()
    for c in cuts:
        plane = vtk.vtkPlane()
        plane.SetOrigin(*c.origin)
        plane.SetNormal(*c.normal)

        # The sphere must fully clear the vessel cross-section. While it cuts
        # through the rim, part of the opening follows the sphere instead of
        # the plane and the result is not planar -- and the error grows with
        # radius until the sphere finally clears, so a slightly-too-small
        # sphere is worse than a much-too-small one. Measured on a real cut:
        # 0.48 mm deviation at 3x MISR, 1.08 mm at 5x, exactly 0.00 mm once the
        # sphere cleared. Sizing off the measured cross-section makes that
        # scale with the vessel instead of being a number that happens to work.
        r_cross = _cross_section_radius(pts, c.origin, c.normal, c.misr)
        radius = max(sphere_factor * r_cross, 3.0 * c.misr, 1e-3)

        sphere = vtk.vtkSphere()
        sphere.SetCenter(*c.origin)
        sphere.SetRadius(radius)

        piece = vtk.vtkImplicitBoolean()
        piece.SetOperationTypeToIntersection()
        piece.AddFunction(plane)
        piece.AddFunction(sphere)
        removal.AddFunction(piece)

    clipper = vtk.vtkClipPolyData()
    clipper.SetInputData(_triangulate_and_clean(surface))
    clipper.SetClipFunction(removal)
    clipper.SetValue(0.0)
    clipper.InsideOutOff()              # keep f > 0, i.e. outside every removal region
    clipper.GenerateClipScalarsOff()
    clipper.Update()

    clipped = clipper.GetOutput()
    if clipped.GetNumberOfCells() == 0:
        raise RuntimeError(
            "Isolation removed the entire surface. The seed may lie outside "
            "the mesh, or the cut localisation radius may be too large — try "
            "'isolate --sphere 2'."
        )

    kept = _seed_region(clipped, seed_mm)
    if kept.GetNumberOfCells() == 0:
        raise RuntimeError(
            "No surface remains around the seed after cutting. Check the seed "
            "sits on the aneurysm and in this mesh's coordinate frame."
        )
    # Rims are left open on purpose; capping is 'extend's job.
    return _triangulate_and_clean(kept)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def isolate_aneurysm_region(surface, seed_mm, iso_params=None,
                            centerlines=None, progress_cb=None) -> dict:
    """Trim *surface* to the aneurysm plus N parent diameters of every vessel.

    Returns a dict with: 'surface', 'centerlines', 'engine', 'anchor', 'cuts',
    'parent_diameter_mm', 'threshold_mm', 'scaffold_mm', 'stats'.
    """
    from vortex.state.app_state import IsolateParams
    iso = iso_params or IsolateParams()

    def _prog(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)
        log.debug("[%3d%%] %s", pct, msg)

    cells_before = surface.GetNumberOfCells()
    openings_before = len(_detect_boundary_profiles(surface))

    scaffold = float(iso.scaffold_mm)
    result = _isolate_once(surface, seed_mm, iso, scaffold, centerlines, _prog)

    # Truncation self-check: a cut sitting on the scaffold boundary means the
    # scaffold was too small to reach N diameters, so the trim length is being
    # set by the box rather than by the anatomy. Retry once, wider.
    if result["truncated"] and centerlines is None:
        # Cap the expansion. Without a ceiling, a mis-measured diameter (a seed
        # that landed on bone, say) produces a huge threshold and the retry
        # would blow the working region up to the whole scan, which is slow and
        # defeats the point of isolating.
        wider = min(max(1.5 * scaffold, result["threshold_mm"] * 1.3), 2.0 * scaffold)
        log.warning("Cuts reached the %.1f mm scaffold boundary — the trim was "
                    "being limited by the box, not the vessel. Retrying at "
                    "%.1f mm.", scaffold, wider)
        _prog(50, f"Scaffold too small — retrying at {wider:.0f} mm...")
        result = _isolate_once(surface, seed_mm, iso, wider, None, _prog)
        scaffold = wider
        if result["truncated"]:
            log.warning(
                "Cuts still reach the scaffold boundary at %.1f mm. The vessel "
                "may leave the segmented volume before %.1f mm; the trim is "
                "shorter than %.1f diameters on at least one branch.",
                wider, result["threshold_mm"], iso.n_diameters)

    out = result["surface"]
    cells_after = out.GetNumberOfCells()
    openings_after = len(_detect_boundary_profiles(out))

    if cells_before and cells_after > 0.80 * cells_before:
        log.warning("Isolation removed only %.0f%% of the mesh — the region may "
                    "be larger than intended. Try 'isolate --diameters 3'.",
                    100.0 * (1.0 - cells_after / cells_before))
    if cells_before and cells_after < 0.02 * cells_before:
        log.warning("Isolation kept only %.1f%% of the mesh — the seed may sit "
                    "on a small fragment. Inspect with 'check'.",
                    100.0 * cells_after / cells_before)
    if openings_after < 2:
        log.warning("Isolation left %d opening(s); 'centerlines' needs at least "
                    "2. Lower --diameters, or check the seed.", openings_after)

    _prog(100, f"Isolated: {cells_before:,} -> {cells_after:,} cells")
    return {
        "surface":            out,
        "centerlines":        result["centerlines"],
        "engine":             result["engine"],
        "anchor":             result["anchor"],
        "cuts":               result["cuts"],
        "parent_diameter_mm": result["parent_diameter_mm"],
        "threshold_mm":       result["threshold_mm"],
        "scaffold_mm":        scaffold,
        "stats": {
            "cells_before":    cells_before,
            "cells_after":     cells_after,
            "openings_before": openings_before,
            "openings_after":  openings_after,
            "bbox_before":     _bbox_diag(surface),
            "bbox_after":      _bbox_diag(out),
            "truncated":       result["truncated"],
        },
    }


def _isolate_once(surface, seed_mm, iso, scaffold_mm, centerlines, prog):
    """One full pass at a given scaffold size. See module docstring for order."""
    prog(5, f"Clipping to a {scaffold_mm:.0f} mm working region...")
    work = scaffold_clip(surface, seed_mm, scaffold_mm, iso.scaffold_inset_mm)
    if work.GetNumberOfCells() == 0:
        raise RuntimeError(
            "The working region around the seed is empty. The seed is probably "
            "not inside this mesh — check it is in world mm, not voxel indices."
        )

    if centerlines is None:
        centerlines, engine = prepare_isolation_centerlines(work, iso, prog)
    else:
        engine = "cached"

    prog(60, "Building the centerline graph...")
    graph = build_centerline_graph(centerlines)

    prog(65, "Locating the aneurysm neck...")
    anchor = find_neck_anchor(graph, seed_mm, engine=engine,
                              retract=iso.anchor_retract)
    dist, pred = geodesic_from_node(graph, anchor.node)

    unreachable = int((~np.isfinite(dist)).sum())
    if unreachable:
        log.warning("%d centerline node(s) sit in a disconnected piece of the "
                    "tree and were ignored.", unreachable)

    prog(70, "Measuring the parent vessel...")
    diameter = estimate_parent_diameter(graph, dist, anchor)
    _check_vessel_like(diameter)
    threshold = float(iso.n_diameters) * diameter
    log.info("Parent diameter %.2f mm -> trimming at %.1f mm (%.1f diameters)",
             diameter, threshold, iso.n_diameters)

    prog(80, f"Cutting at {threshold:.1f} mm from the neck...")
    cuts = find_branch_cuts(graph, dist, pred, threshold)
    log.info("Found %d branch cut(s)", len(cuts))

    truncated, n_short = _detect_truncation(graph, dist, threshold, work)
    if truncated:
        log.warning("%d vessel branch(es) end at the working-region boundary "
                    "before reaching %.1f mm, so the box is setting their "
                    "length instead of the anatomy.", n_short, threshold)

    prog(88, "Applying the cuts...")
    out = apply_cuts(work, cuts, seed_mm, iso.cut_sphere_factor) if cuts else work
    if not cuts:
        log.info("Nothing to trim — the whole working region is within "
                 "%.1f mm (%.1f diameters) of the aneurysm.",
                 threshold, iso.n_diameters)

    return {"surface": out, "centerlines": centerlines, "engine": engine,
            "anchor": anchor, "cuts": cuts, "parent_diameter_mm": diameter,
            "threshold_mm": threshold, "truncated": truncated}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_truncation(graph, dist, threshold_mm, work, edge_tol=1.5):
    """-> (truncated, count) — branches the scaffold cut short.

    A branch is truncated when its centerline *endpoint* (a degree-1 node) sits
    on the working-region boundary while still inside the threshold. That
    branch never crosses the frontier, so it produces no CutPlane and a check
    on cut positions alone would miss it entirely. What the user gets instead
    is a box-face opening: planar, but square to the box rather than
    perpendicular to the vessel.

    An endpoint inside the threshold but away from the boundary is a genuine
    anatomical dead end, not truncation, so it is not counted.
    """
    b = list(work.GetBounds())
    ends = np.where((graph.degree <= 1) & np.isfinite(dist) & (dist < threshold_mm))[0]
    n = 0
    for i in ends:
        p = graph.points[i]
        if any(min(abs(p[ax] - b[2 * ax]), abs(p[ax] - b[2 * ax + 1])) < edge_tol
               for ax in range(3)):
            n += 1
    return n > 0, n


def _seed_region(poly, seed_mm):
    """Keep the connected region nearest the seed.

    Deliberately not largest-region: on a bone-contaminated segmentation the
    largest connected region is often not the one holding the aneurysm.
    """
    if poly.GetNumberOfCells() == 0:
        return poly
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(poly)
    conn.SetExtractionModeToClosestPointRegion()
    conn.SetClosestPoint(float(seed_mm[0]), float(seed_mm[1]), float(seed_mm[2]))
    conn.Update()
    return conn.GetOutput()


def _triangulate_and_clean(poly):
    tri = vtk.vtkTriangleFilter()
    tri.SetInputData(poly)
    tri.Update()
    clean = vtk.vtkCleanPolyData()
    clean.SetInputData(tri.GetOutput())
    clean.Update()
    return clean.GetOutput()


def _bbox_diag(poly) -> float:
    b = poly.GetBounds()
    return float(np.sqrt((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2))
