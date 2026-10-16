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
    find_merged_openings,
)

log = logging.getLogger(__name__)

_MISR_ARRAY    = "MaximumInscribedSphereRadius"
_TANGENT_ARRAY = "FrenetTangent"
_MERGE_TOL     = 1e-4     # mm — coordinate rounding when merging centerline points

# A measured parent diameter above this is not a vessel; it is skull or soft
# tissue. Reference: a whole-head segmentation measured 12.78 mm median MISR
# against 0.93 mm for a real cerebral artery.
_MAX_VESSEL_DIAMETER_MM = 12.0

# Growing the working region by a quarter can at most roughly double the
# triangles of a tube. A steeper jump than this means non-vessel material
# (typically skull base bone) has entered the box. Measured: 29,620 -> 109,534
# triangles going 12 -> 15 mm on an anterior communicating artery case.
_BONE_JUMP = 2.2

# Centerlines closer than this fraction of the local radius are the same lumen.
# VMTK routes each source-target pair separately, and near a junction two lines
# can run 0.4 mm apart without coinciding, so exact merging leaves them
# unjoined and the shortest route between them becomes a detour. On AA_009 a
# hairpin out to a neighbouring bifurcation and back put the only cut on the
# detour, and the left A1 stayed attached to the neck, untrimmed.
_BRIDGE_FACTOR = 0.5

# The parent diameter is read this far past the neck, where the junction bulge
# has passed: every AA_009 branch reads 2.0-2.9 mm in its first few mm against
# a steady 1.4-1.6 mm beyond.
_BRANCH_SKIP_MM = 4.0

# Below this the "parent" is probably the communicating segment or a daughter,
# not the inflow vessel. A warning, not a limit: AA_009's A1s are 1.4-1.6 mm.
_MIN_PARENT_DIAMETER_MM = 1.2


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

    rows, cols = [], []
    lines = centerlines.GetLines()
    lines.InitTraversal()
    ids = vtk.vtkIdList()
    while lines.GetNextCell(ids):
        raw = [ids.GetId(k) for k in range(ids.GetNumberOfIds())]
        if len(raw) < 2:
            continue
        a = inverse[raw]
        for u, v in zip(a[:-1], a[1:]):
            if u != v:                      # drop self-edges from merging
                rows.append(u); cols.append(v)

    if not rows:
        raise RuntimeError("Centerlines contain no usable segments")

    # A trunk shared by k polylines yields the same edge k times, and a sparse
    # matrix *sums* duplicates: measured 2x and 4x edge weights on AA_009,
    # which stretched every geodesic through the neck region. Keep each edge
    # once, weighted by its true length.
    uv = np.unique(np.sort(np.column_stack([rows, cols]), axis=1), axis=0)
    adj = _edges_to_adjacency(uv, nodes, n)
    # Degree comes from the lines as VMTK drew them. Bridges are shortcuts for
    # measuring distance, not new branches, so they must not turn a line's end
    # into a pass-through node or a straight run into a "bifurcation".
    degree = np.diff(adj.indptr)

    bridges = _parallel_bridges(adj, nodes, misr)
    if len(bridges):
        log.info("Joined %d point pair(s) where centerlines run side by side "
                 "without meeting", len(bridges))
        adj = _edges_to_adjacency(np.vstack([uv, bridges]), nodes, n)

    ncomp, _ = connected_components(adj, directed=False)
    log.info("Centerline graph: %d points -> %d nodes, %d component(s), "
             "%d bifurcation node(s)", len(pts), n, ncomp, int((degree >= 3).sum()))
    return CenterlineGraph(nodes, misr, tangent, adj, degree)


def _edges_to_adjacency(uv, nodes, n):
    """Symmetric adjacency, each edge once, weighted by its true length."""
    uv = np.unique(np.sort(uv, axis=1), axis=0)
    w = np.linalg.norm(nodes[uv[:, 0]] - nodes[uv[:, 1]], axis=1)
    return sp.coo_matrix((np.concatenate([w, w]),
                          (np.concatenate([uv[:, 0], uv[:, 1]]),
                           np.concatenate([uv[:, 1], uv[:, 0]]))),
                         shape=(n, n)).tocsr()


def _parallel_bridges(adj, nodes, misr, factor=_BRIDGE_FACTOR):
    """-> (K,2) node pairs to join: close in space, far apart along the lines.

    Close means within *factor* of the smaller local radius, so two separate
    vessels -- whose axes are at least the sum of their radii apart -- are
    never joined. Far means the existing route between them is over three
    times the gap; neighbours along one line are skipped, since joining them
    would change nothing.
    """
    from scipy.spatial import cKDTree
    pairs = cKDTree(nodes).query_pairs(factor * float(misr.max()), output_type="ndarray")
    if len(pairs) == 0:
        return pairs.reshape(0, 2)
    gap = np.linalg.norm(nodes[pairs[:, 0]] - nodes[pairs[:, 1]], axis=1)
    close = gap < factor * np.minimum(misr[pairs[:, 0]], misr[pairs[:, 1]])
    pairs, gap = pairs[close], gap[close]
    if len(pairs) == 0:
        return pairs.reshape(0, 2)
    src = np.unique(pairs[:, 0])
    route = dijkstra(adj, directed=False, indices=src, limit=3.0 * float(gap.max()) + 1e-9)
    row = {int(u): k for k, u in enumerate(src)}
    along = route[[row[int(u)] for u in pairs[:, 0]], pairs[:, 1]]
    return pairs[~(along <= 3.0 * gap)]


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

def estimate_parent_diameter(graph: CenterlineGraph, dist, pred, anchor: AnchorInfo) -> float:
    """The inflow vessel's diameter: the widest branch leaving the neck.

    Each branch is measured at its steady width -- the median over the stretch
    from _BRANCH_SKIP_MM past the neck to 1 mm short of its end -- and the
    widest is taken as the parent. The inflow is normally the largest vessel
    at an aneurysm, so this needs no anatomy labels.

    Measuring at the neck instead read the wrong vessel on anterior
    communicating aneurysms: the neck sits on the communicating segment or an
    A2, 1.12 mm on AA_009 against 1.6 mm for its A1, which cut every branch
    at about 3 of its own diameters. "First reading above 2 mm" was tried and
    picks bulges, not vessels -- 5.2 mm on AA_003 is the aneurysm's own neck.
    Falls back to the old neck band only when no branch is long enough.
    """
    widths = []
    for end in np.where(graph.degree <= 1)[0]:
        if end == anchor.node or not np.isfinite(dist[end]):
            continue
        path = [int(end)]
        while pred[path[-1]] >= 0:
            path.append(int(pred[path[-1]]))
        path = np.array(path)
        along = dist[path]
        steady = (along >= _BRANCH_SKIP_MM) & (along <= dist[end] - 1.0)
        if steady.sum() >= 3:
            widths.append(2.0 * float(np.median(graph.misr[path[steady]])))
    if widths:
        d = max(widths)
        log.info("Branch diameters %s mm; parent taken as the widest, %.2f mm",
                 [round(x, 2) for x in sorted(widths, reverse=True)], d)
    else:
        r = max(float(anchor.misr), 1e-3)
        band = np.isfinite(dist) & (dist >= 1.0 * r) & (dist <= 3.0 * r)
        d = 2.0 * float(np.median(graph.misr[band])) if band.any() else 2.0 * r
        log.warning("No branch reaches %.0f mm past the neck; measuring the "
                    "diameter at the neck instead, which can read the wrong "
                    "vessel.", _BRANCH_SKIP_MM)
    if d < _MIN_PARENT_DIAMETER_MM:
        log.warning("Parent diameter %.2f mm is small for an inflow artery; the "
                    "trim will be short. Check the seed is in the dome.", d)
    return d


def _check_vessel_like(diameter_mm: float) -> None:
    if diameter_mm > _MAX_VESSEL_DIAMETER_MM:
        raise RuntimeError(
            f"Measured local vessel diameter is {diameter_mm:.1f} mm, which is "
            f"not vessel-like (a cerebral artery is roughly 2-4 mm). The "
            f"centerline is running through bone or soft tissue rather than "
            f"lumen.\n"
            f"Most often the working region is too large and has taken in bone: "
            f"try a smaller one, e.g. 'isolate --scaffold 12'. Otherwise re-run "
            f"'segment' with a tighter roi_radius or a higher HU threshold, or "
            f"clean the mesh externally first."
        )


# ---------------------------------------------------------------------------
# Step 6 — find the cuts
# ---------------------------------------------------------------------------

def find_branch_cuts(graph: CenterlineGraph, dist, pred, threshold_mm,
                      measure=None) -> list:
    """One CutPlane per branch crossing *threshold_mm* from the anchor.

    *measure* is the length the threshold applies to, per node -- by default
    *dist*, distance from the anchor; _length_past_dome gives distance from
    where each branch leaves the aneurysm. Crossings count only downstream
    (away from the anchor), so a branch that touches the dome again further
    along cannot produce a cut facing the wrong way.

    Scans graph *edges*, not nodes: an edge with one end inside the threshold
    and the other outside is a branch crossing the frontier, and there is
    exactly one such edge per branch.  So a bifurcation yields one cut per
    daughter automatically, with no selection heuristic that could silently
    drop a branch.  This is what keeps bifurcation aneurysms intact.
    """
    m = dist if measure is None else measure
    adj = graph.adjacency
    cuts = []
    for u in range(adj.shape[0]):
        if not np.isfinite(m[u]) or m[u] > threshold_mm:
            continue
        for v in adj.indices[adj.indptr[u]:adj.indptr[u + 1]]:
            if not np.isfinite(m[v]) or m[v] <= threshold_mm or dist[v] <= dist[u]:
                continue
            cut = _cut_on_edge(graph, pred, m, u, v, threshold_mm)
            if cut is not None:
                cuts.append(cut)

    return _dedupe_cuts(cuts)


def _cut_on_edge(graph, pred, m, u, v, target):
    """CutPlane where *m* reaches *target* on the edge u -> v (v downstream)."""
    pu, pv = graph.points[u], graph.points[v]
    span = m[v] - m[u]
    t = float((target - m[u]) / span) if span > 1e-9 else 0.5
    origin = pu + min(max(t, 0.0), 1.0) * (pv - pu)   # sub-step accurate, not quantised

    normal = graph.tangent[v].copy()
    if np.linalg.norm(normal) < 0.5:
        normal = pu - pv                # degenerate tangent: use the edge
    # Orient toward the anchor so "beyond the plane" means downstream.
    back = pu - pv
    p = int(pred[v])
    if p >= 0:
        back = graph.points[p] - pv
    if float(np.dot(normal, back)) < 0.0:
        normal = -normal
    nn = np.linalg.norm(normal)
    if nn < 1e-9:
        return None
    return CutPlane(origin=tuple(origin), normal=tuple(normal / nn),
                    misr=float(graph.misr[v]), node=int(v), geodesic=float(target))


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


def _dome_points(work, centerlines, seed_mm):
    """-> surface points of the aneurysm dome, or None when it cannot be found.

    Uses clip-sac's bulge field: dome wall sits much further from its nearest
    centerline than the local vessel radius. Trusted only if the wall nearest
    the seed -- which is inside the dome -- falls in the region it returns.
    That check fails on AA_003, whose segmentation has no wall between the
    dome and the right A2: the dome barely bulges and 19 triangles came back,
    5 mm from where the dome actually is.
    """
    from vortex.pipeline.sac_clipping import clip_aneurysm_sac
    try:
        sac = clip_aneurysm_sac(work, centerlines, tuple(seed_mm))["sac"]
    except Exception as exc:
        log.info("No dome region from the bulge field (%s)", exc)
        return None
    if sac is None or sac.GetNumberOfPoints() == 0:
        return None
    dome = vtk_np.vtk_to_numpy(sac.GetPoints().GetData()).astype(float)
    wall = vtk_np.vtk_to_numpy(work.GetPoints().GetData()).astype(float)
    seed = np.asarray(seed_mm, dtype=float)
    nearest_wall = wall[np.argmin(np.linalg.norm(wall - seed, axis=1))]
    if np.linalg.norm(dome - nearest_wall, axis=1).min() > 0.5:
        log.warning("The bulge field did not find the dome around the seed, so "
                    "vessel lengths are measured from the neck point instead of "
                    "from where each vessel leaves the aneurysm.")
        return None
    return dome


def _length_past_dome(graph, dist, pred, dome):
    """-> per node, centerline length since its branch last touched the dome.

    One neck point cannot stand for where every vessel leaves the aneurysm.
    Measured from a single point, AA_009's left A2 kept 1.2 diameters past the
    dome against 2.7-2.9 for the others, and on AA_001 the parent kept 1.2
    against 5.2 for the distal vessel. A node touches the dome when the dome
    wall is within its vessel radius (plus a margin).
    """
    from scipy.spatial import cKDTree
    gap = cKDTree(dome).query(graph.points)[0]
    touch = gap < 1.5 * graph.misr + 0.3
    last = np.zeros(len(dist))
    for v in np.argsort(dist):
        if not np.isfinite(dist[v]):
            break
        p = int(pred[v])
        last[v] = dist[v] if touch[v] else (last[p] if p >= 0 else 0.0)
    out = dist - last
    out[~np.isfinite(dist)] = np.inf
    return out


# ---------------------------------------------------------------------------
# Step 7 — apply the cuts
# ---------------------------------------------------------------------------

def _cut_rim(surface, origin, normal):
    """-> (own, other): how far this vessel's rim reaches from the cut centre,
    and how close anything else in the cut plane comes. Both in mm.

    MISR is the *inscribed* radius, so it understates the cross-section
    wherever the plane meets the vessel obliquely or the lumen is non-circular
    (measured: MISR 1.24 mm against a rim reaching 3.34 mm), so the rim is
    measured directly: the plane's contour loop that passes nearest the cut
    centre is this vessel's; every other loop is something else.

    This replaced a slab search bounded at 6x MISR, which on an anterior
    communicating aneurysm took in the *other* A2 running parallel 3.6 mm away:
    a 0.90 mm rim measured as 4.38 mm, the localisation sphere grew to 10.9 mm,
    and one plane cut both A2s (AA_009; 14.7 mm on AA_003).
    """
    plane = vtk.vtkPlane()
    plane.SetOrigin(*origin)
    plane.SetNormal(*normal)
    cutter = vtk.vtkCutter()
    cutter.SetInputData(surface)
    cutter.SetCutFunction(plane)
    cutter.Update()
    loops = vtk.vtkPolyDataConnectivityFilter()
    loops.SetInputData(cutter.GetOutput())
    loops.SetExtractionModeToAllRegions()
    loops.ColorRegionsOn()
    loops.Update()
    out = loops.GetOutput()
    if out.GetNumberOfPoints() == 0:
        return None, np.inf
    pts = vtk_np.vtk_to_numpy(out.GetPoints().GetData()).astype(float)
    region = vtk_np.vtk_to_numpy(out.GetPointData().GetArray("RegionId"))
    d = np.linalg.norm(pts - np.asarray(origin, dtype=float), axis=1)
    mine = region == region[np.argmin(d)]
    other = float(d[~mine].min()) if (~mine).any() else np.inf
    return float(d[mine].max()), other


def _slab_radius(points, origin, normal, misr):
    """How far surface near the cut plane reaches from the cut centre, out to
    6x MISR -- the wide cut size.

    Unlike _cut_rim this does not tell the vessel apart from its neighbours:
    anything within that bound in a thin slab around the plane counts. That is
    what apply_cuts wants when a tight cut failed to separate a vessel.
    """
    o = np.asarray(origin, dtype=float)
    n = np.asarray(normal, dtype=float)
    d = points - o
    along = d @ n
    inplane = np.linalg.norm(d - np.outer(along, n), axis=1)
    slab = max(0.5 * misr, 0.3)
    m = (np.abs(along) < slab) & (inplane < 6.0 * misr)
    return float(inplane[m].max()) if m.any() else float(misr)


def apply_cuts(surface, cuts, seed_mm, sphere_factor=2.5, dome=None):
    """Remove everything beyond each cut, keeping the seed's connected region.

    Returns (surface, cuts): the cuts actually applied. A cut that cannot
    separate its vessel is dropped, so this can be fewer than were passed in.

    A plane is infinite, and that is the one real trap here.  A cut placed on
    one daughter of a bifurcation would also slice the other daughter, the
    parent, or the dome, wherever they fall on its negative side.  Intersecting
    half-spaces is worse: that only ever describes a convex region, and a
    vessel tree is not convex.

    So every cut is localised to a sphere of a few local radii around it.  The
    removal region is (beyond the plane) AND (inside the sphere), unioned
    across cuts, evaluated in one clip pass (a second pass only when a cut
    failed to separate its vessel).  Inside the sphere the clip surface *is*
    the plane, so each opening comes out genuinely planar, which is what
    vmtkFlowExtensions needs.  The curved part of the boundary only ever
    appears on the discarded side.
    """
    if not cuts:
        return _triangulate_and_clean(surface), []

    surface = _triangulate_and_clean(surface)
    pts = vtk_np.vtk_to_numpy(surface.GetPoints().GetData()).astype(float)

    # The sphere must fully clear the vessel cross-section. While it cuts
    # through the rim, part of the opening follows the sphere instead of the
    # plane and the result is not planar -- and the error grows with radius
    # until the sphere finally clears, so a slightly-too-small sphere is worse
    # than a much-too-small one. Measured on a real cut: 0.48 mm deviation at
    # 3x MISR, 1.08 mm at 5x, exactly 0.00 mm once the sphere cleared.
    #
    # Two sizes per cut. The tight one clears this vessel's own rim and stops
    # short of anything else the plane passes through, so parallel daughters
    # each get their own cut (AA_009's A2s, 3.6 mm apart, were cut by one
    # plane). The wide one is the old size, which also takes whatever lies
    # next to the vessel in the plane. It is needed where the tree has a loop
    # the centerlines cannot see -- AA_003's two A2s rejoin distally, so a
    # tight cut on one leaves everything beyond it attached through the other.
    #
    # Neither size may reach the aneurysm. On AA_003 the wide sphere was
    # 14.8 mm and sliced through the dome where the right A2 merges into it.
    # So both are capped short of the seed's clear ball (the seed is inside
    # the dome) and of any dome wall beyond the plane.
    seed = np.asarray(seed_mm, dtype=float)
    depth = float(np.linalg.norm(pts - seed, axis=1).min())

    def sizes(c):
        o = np.asarray(c.origin, dtype=float)
        own, other = _cut_rim(surface, c.origin, c.normal)
        if own is None:
            own = c.misr
        cap = float(np.linalg.norm(o - seed)) - depth
        if dome is not None:
            beyond = dome[(dome - o) @ np.asarray(c.normal) < 0.0]
            if len(beyond):
                cap = min(cap, float(np.linalg.norm(beyond - o, axis=1).min()) - 0.3)
        floor = 1.05 * own
        if cap < floor:
            log.warning("The cut at (%.1f, %.1f, %.1f) is so close to the aneurysm "
                        "that clearing its own rim may touch the dome.", *c.origin)
        r = sphere_factor * own
        if np.isfinite(other):
            r = min(r, 0.5 * (own + other))
        t = max(min(r, cap), floor, 1e-3)
        w = max(sphere_factor * _slab_radius(pts, c.origin, c.normal, c.misr), 3.0 * c.misr)
        return t, max(min(w, cap), t)

    cuts = list(cuts)
    tight, wide = map(list, zip(*[sizes(c) for c in cuts]))
    radii = list(tight)

    def loose_after_clip():
        kept = _clip_and_keep(surface, cuts, radii, seed_mm)
        return kept, [k for k, c in enumerate(cuts) if not _severed(kept, c, radii[k])]

    kept, loose = loose_after_clip()

    # 1. Cut everything next to the vessel in its plane, short of the dome.
    #    Needed where the tree has a loop the centerlines cannot see -- two
    #    vessels rejoining distally, or touching (AA_009's A2s, 6.3 mm past
    #    the dome). Moving the cuts back toward the aneurysm instead was
    #    tried and left fragments of the contact attached.
    if loose:
        for k in loose:
            log.warning("The cut at (%.1f, %.1f, %.1f) did not separate its vessel "
                        "-- the part beyond it is still attached some other way, "
                        "typically two vessels fused further along. Cutting "
                        "everything next to it in the same plane instead.",
                        *cuts[k].origin)
            radii[k] = wide[k]
        kept, loose = loose_after_clip()

    # 2. A cut that still separates nothing only punches a hole in a vessel
    #    that carries on regardless (AA_003). Drop it: an untrimmed vessel is
    #    reported by the caller, a holed one is not usable.
    if loose:
        for k in loose:
            log.warning("The cut at (%.1f, %.1f, %.1f) cannot separate its vessel "
                        "without reaching the aneurysm; that vessel is left "
                        "untrimmed.", *cuts[k].origin)
        keep = [k for k in range(len(cuts)) if k not in loose]
        cuts = [cuts[k] for k in keep]
        radii = [radii[k] for k in keep]
        kept = (_clip_and_keep(surface, cuts, radii, seed_mm) if cuts
                else _seed_region(surface, seed_mm))
    # Rims are left open on purpose; capping is 'extend's job.
    return _triangulate_and_clean(kept), cuts


def _clip_and_keep(surface, cuts, radii, seed_mm):
    """One clip pass: remove (beyond plane AND inside sphere) for every cut,
    then keep the seed's connected region.

    VTK convention: negative is inside, INTERSECTION takes the max, UNION the
    min.  The plane normal points toward the anchor, so its function is
    negative downstream of the cut, which is the material to remove.
    """
    removal = vtk.vtkImplicitBoolean()
    removal.SetOperationTypeToUnion()
    for c, radius in zip(cuts, radii):
        plane = vtk.vtkPlane()
        plane.SetOrigin(*c.origin)
        plane.SetNormal(*c.normal)
        sphere = vtk.vtkSphere()
        sphere.SetCenter(*c.origin)
        sphere.SetRadius(radius)
        piece = vtk.vtkImplicitBoolean()
        piece.SetOperationTypeToIntersection()
        piece.AddFunction(plane)
        piece.AddFunction(sphere)
        removal.AddFunction(piece)

    clipper = vtk.vtkClipPolyData()
    clipper.SetInputData(surface)
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
    return kept


def _severed(kept, cut, radius):
    """False when the vessel still continues just past the cut's sphere.

    Looks in a short cylinder around the cut axis, beyond the plane and just
    outside the sphere: a severed vessel has nothing kept there. A vessel that
    curves out of the cylinder reads as severed, which is the old behaviour.
    """
    if kept.GetNumberOfPoints() == 0:
        return True
    p = vtk_np.vtk_to_numpy(kept.GetPoints().GetData()).astype(float)
    n = np.asarray(cut.normal, dtype=float)
    d = p - np.asarray(cut.origin, dtype=float)
    beyond = -(d @ n)                           # > 0 downstream of the cut
    inplane = np.linalg.norm(d + np.outer(beyond, n), axis=1)
    reach = 2.0 * max(cut.misr, 0.5)
    band = (beyond > radius) & (beyond < radius + reach) & (inplane < 1.5 * reach)
    return not bool(band.any())


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

    if getattr(iso, "scaffold_auto", True) and centerlines is None:
        scaffold = choose_scaffold(surface, seed_mm, float(iso.scaffold_mm),
                                   iso.scaffold_inset_mm, _prog)
        if scaffold < float(iso.scaffold_mm):
            log.info("Working region auto-set to %.0f mm (cap %.0f mm).",
                     scaffold, iso.scaffold_mm)
    else:
        scaffold = float(iso.scaffold_mm)

    # If centerlines fail at the chosen size, step down rather than give up:
    # a slightly smaller region often still has two clean openings.
    _tried = []
    while True:
        try:
            result = _isolate_once(surface, seed_mm, iso, scaffold, centerlines, _prog)
            break
        except RuntimeError as exc:
            _tried.append(scaffold)
            smaller = scaffold - 2.0
            if centerlines is not None or smaller < 6.0 or not getattr(iso, "scaffold_auto", True):
                raise
            log.warning("Isolation failed at a %.0f mm working region (%s). "
                        "Retrying at %.0f mm.", scaffold, exc, smaller)
            scaffold = smaller

    # The working region is NOT widened automatically when a branch is cut
    # short, even though that is the obvious thing to try. Widening drags in
    # the bone and soft tissue that surround the vessel, and the diameter is
    # measured from the centerline running through whatever is in the region.
    # Measured on AA_001: at a 15 mm region the centerline is a clean artery
    # (anchor radius 0.85 mm, diameter 1.62 mm, matching an independent 1.78 mm
    # measurement). At 30 mm the anchor sits in bone, the median radius goes
    # from 0.83 to 3.67 mm, and the vessel "measures" 19.24 mm. The region also
    # grew from 31k to 458k triangles. So growing the region to win a longer
    # trim destroys the measurement that sets the trim.
    #
    # Truncation is therefore reported, not fixed. Raising it is the user's
    # call, because only they can tell a vessel that leaves the scan from one
    # that is merely outside the current region.
    if result["truncated"]:
        log.warning(
            "At least one branch reaches the %.0f mm working region before the "
            "full %.1f mm trim, so the region is setting its length rather than "
            "the anatomy. Raise it with 'isolate --scaffold %.0f' if the vessel "
            "continues in the scan — but check the reported diameter stays "
            "vessel-like, since a larger region can take in bone.",
            scaffold, result["threshold_mm"], min(scaffold * 1.5, scaffold + 10))

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
        "measured_from":      result["measured_from"],
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
            "untrimmed_edge":  result["untrimmed_edge"],
            "untrimmed_other": result["untrimmed_other"],
            "merged_openings": result["merged_openings"],
        },
    }


def choose_scaffold(surface, seed_mm, max_mm, inset_mm, prog=None):
    """Pick the largest working region that still contains only vessel.

    A fixed region cannot work. Too small and the trim is cut short; too large
    and it swallows nearby bone, whereupon the centerline routes through the
    bone and the "vessel diameter" becomes meaningless -- which then sets the
    trim distance, so one bad measurement corrupts everything downstream.

    Bone entering the box announces itself by a jump in triangle count far
    steeper than the box is growing. Measured on two anterior communicating
    artery cases, where the skull base is millimetres away:

        AA_003   8mm 23028   10mm 26463   12mm 29620   15mm 109534 tris
                 diameter    2.33  2.29    2.25  ->  8.93 mm at 15mm
        AA_009   8mm 10648   10mm 13253                15mm 119798 tris
                 diameter    1.01  0.96                9.84 mm at 15mm

    Growing the box by a quarter can at most about double the triangles of a
    tube-like structure, so a jump past _BONE_JUMP means something that is not
    vessel came in. This is a clip-only test, so it costs no centerline runs.
    """
    sizes = [s for s in (8.0, 10.0, 12.0, 15.0, 20.0, 25.0) if s <= max_mm + 1e-9]
    if not sizes:
        sizes = [max_mm]

    chosen, prev_cells, prev_size = sizes[0], None, None
    for s in sizes:
        if prog:
            prog(3, f"Sizing the working region ({s:.0f} mm)...")
        cells = scaffold_clip(surface, seed_mm, s, inset_mm).GetNumberOfCells()
        if cells == 0:
            continue
        if prev_cells and cells > _BONE_JUMP * prev_cells:
            log.info("Working region stops at %.0f mm: going to %.0f mm would "
                     "take the surface from %d to %d triangles, too steep for "
                     "vessel alone, so something else is entering the box.",
                     prev_size, s, prev_cells, cells)
            chosen = prev_size
            break
        chosen, prev_cells, prev_size = s, cells, s
    return float(chosen)


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
    diameter = estimate_parent_diameter(graph, dist, pred, anchor)
    _check_vessel_like(diameter)
    threshold = float(iso.n_diameters) * diameter
    log.info("Parent diameter %.2f mm -> trimming at %.1f mm (%.1f diameters)",
             diameter, threshold, iso.n_diameters)

    prog(75, "Locating the aneurysm dome...")
    dome = _dome_points(work, centerlines, seed_mm)
    measure = dist if dome is None else _length_past_dome(graph, dist, pred, dome)

    prog(80, f"Cutting {threshold:.1f} mm past the aneurysm...")
    cuts = find_branch_cuts(graph, dist, pred, threshold, measure)
    log.info("Found %d branch cut(s)", len(cuts))

    truncated, n_short = _detect_truncation(graph, measure, threshold, work)
    if truncated:
        log.warning("%d vessel branch(es) end at the working-region boundary "
                    "before reaching %.1f mm, so the box is setting their "
                    "length instead of the anatomy.", n_short, threshold)

    prog(88, "Applying the cuts...")
    out, cuts = apply_cuts(work, cuts, seed_mm, iso.cut_sphere_factor, dome)
    if not cuts:
        log.info("Nothing to trim — the whole working region is within "
                 "%.1f mm (%.1f diameters) of the aneurysm.",
                 threshold, iso.n_diameters)

    at_edge, other = _untrimmed_openings(out, cuts, work)
    if at_edge:
        log.warning("%d opening(s) sit on the working-region edge rather than "
                    "at a cut: those vessels were not trimmed.", at_edge)
    if other:
        log.warning("%d opening(s) are neither a cut nor on the working-region "
                    "edge. Inspect the result before continuing.", other)
    merged = find_merged_openings(out)
    for m in merged:
        log.warning("The opening at (%.1f, %.1f, %.1f) looks like two vessels "
                    "sharing one hole; centerlines will treat them as one.",
                    *m["center_mm"])

    return {"surface": out, "centerlines": centerlines, "engine": engine,
            "anchor": anchor, "cuts": cuts, "parent_diameter_mm": diameter,
            "threshold_mm": threshold, "truncated": truncated,
            "untrimmed_edge": at_edge, "untrimmed_other": other,
            "merged_openings": merged,
            "measured_from": "neck" if dome is None else "dome"}


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


def _untrimmed_openings(out, cuts, work, tol=0.1, edge_tol=0.7):
    """-> (at_edge, other): openings on *out* that no cut made.

    A cut opening lies in its cut's plane (possibly several vessels in one
    plane, when a cut was widened). Anything else is a vessel `isolate` did
    not trim: on the working-region edge it ran out of the region first,
    typically because it has no centerline; elsewhere it is unexplained. The
    centerline-based _detect_truncation cannot see either case -- a vessel
    with no centerline, or one still attached past its cut, has no endpoint
    to test.
    """
    b = work.GetBounds()
    at_edge = other = 0
    for p in _detect_boundary_profiles(out):
        if p["radius_mm"] < 0.3:                 # a sliver, not a vessel
            continue
        c = np.asarray(p["center_mm"], dtype=float)
        if any(abs(float(np.dot(c - np.asarray(k.origin), k.normal))) < tol
               for k in cuts):
            continue
        if any(min(abs(c[ax] - b[2 * ax]), abs(c[ax] - b[2 * ax + 1])) < edge_tol
               for ax in range(3)):
            at_edge += 1
        else:
            other += 1
    return at_edge, other


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
