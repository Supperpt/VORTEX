# VMTK / VORTEX Plan: Aneurysm Sac Geometry for Biomarker Computation

## Goal

Produce an `aneurysm_sac.stl` patch (and matching `parent_vessel.stl`) from the existing VMTK pipeline, plus a `neck_plane.json` file that encodes the neck cutting-plane geometry. These outputs become the inputs to the CFD pipeline's named-patch system and to the neck-plane flow metrics.

---

## Context

The current VORTEX pipeline already:
1. Computes vessel centerlines (`vmtkcenterlines`)
2. Extends vessel ends (`vmtkflowextensions`)
3. Caps openings (`vmtksurfacecapper`)
4. Splits STLs per opening (`--split-patches` → `wall.stl`, `inlet.stl`, `outlet_0.stl`, …)

`wall.stl` is the entire lumen surface, including the aneurysm sac fused with the parent vessel wall. The CFD pipeline receives this single patch and computes biomarkers over the whole vessel. To restrict metrics to the aneurysm sac, the split must happen here, before vortex-cfd is called.

---

## What Needs to Be Added

### Step 1 — Aneurysm Neck Detection and Surface Clip

Use **`vmtksurfaceclipper`** (Piccinelli method) to automatically detect the aneurysm neck from the bifurcation geometry and clip the surface there.

VMTK provides two routes:

**A — Automatic (preferred for saccular aneurysms with visible neck):**
Uses centerline branch geometry to identify the neck ring:
```
vmtksurfaceclipper \
    -ifile lumen_wall.vtp \
    -centerlines centerlines.vtp \
    -ofile aneurysm_sac.vtp
```
The complement surface (parent vessel wall) is obtained by clipping with the inverted selection or by surface difference with the original.

**B — Interactive (for complex / wide-neck / fusiform cases):**
```
vmtksurfaceclipperinteractive \
    -ifile lumen_wall.vtp \
    -ofile aneurysm_sac.vtp
```
The user draws the neck loop on-screen. Output is the clipped sac surface.

### Step 2 — Export STLs

Convert both clipped surfaces to STL:
```
vmtksurfacewriter -ifile aneurysm_sac.vtp -ofile aneurysm_sac.stl -format stl
vmtksurfacewriter -ifile parent_vessel.vtp -ofile parent_vessel.stl -format stl
```

Naming convention for vortex-cfd:
- `aneurysm_sac.stl` → becomes the `aneurysm_sac` named patch in the mesh
- `parent_vessel.stl` → replaces the existing `wall.stl` (or rename, update the labeller accordingly)

### Step 3 — Export the Neck Plane Geometry

The clip plane defined by the Piccinelli method has a centroid and a normal. Extract these and save as `neck_plane.json`:

```json
{
  "origin": [x, y, z],
  "normal": [nx, ny, nz]
}
```

This JSON is passed to vortex-cfd so it can configure the `surfaceFieldValue` function object (neck inflow rate, peak velocity) and ParaView neck-slice scripts without requiring geometric recomputation downstream.

How to extract the plane parameters from VMTK:
- After `vmtksurfaceclipper`, the clip plane is stored in the output surface's point data / attributes.
- Alternatively, compute the plane from the neck boundary: extract the boundary loop of `aneurysm_sac.vtp`, take its centroid as `origin` and the area-weighted normal of the adjacent faces as `normal`.
- In Python with vmtk/vtk: `vtkFeatureEdges` → boundary polyline → centroid + PCA normal.

---

## Output Contract to vortex-cfd

The VORTEX `--split-patches` output directory should contain, after this addition:

```
<patient_case>/
├── wall.stl              ← rename to parent_vessel.stl, or keep + add aneurysm_sac
├── aneurysm_sac.stl      ← NEW: aneurysm dome clipped at neck
├── parent_vessel.stl     ← NEW: remainder of wall surface
├── inlet.stl
├── outlet_0.stl
├── neck_plane.json        ← NEW: {"origin": [...], "normal": [...]}
└── (other caps)
```

The vortex-cfd `patch_labeller.py` interactive step will need to be updated to accept `aneurysm_sac` and `parent_vessel` as the two wall-type patches (both assigned BC type `wall`).

---

## Key VMTK References

- `vmtksurfaceclipper` — uses centerline-based neck detection (Piccinelli 2009)
- `vmtksurfaceclipperinteractive` — manual loop selection
- `vmtkcenterlines` — required input for automatic clipping
- `vmtksurfaceregionextractor` — alternative for region-based extraction after clipping
- Piccinelli M et al., *A Framework for Geometric Analysis of Vascular Structures: Application to Cerebral Aneurysms*, IEEE TMI 2009 — the algorithm behind automatic neck detection

---

## Validation

- Open `aneurysm_sac.stl` and `parent_vessel.stl` in MeshLab or ParaView: verify the two surfaces together reconstruct the original `wall.stl` without gaps or overlaps at the neck.
- Verify `neck_plane.json` origin lies visually at the centre of the neck opening.
- Verify the normal points from the sac into the parent vessel (or vice versa — document the convention so vortex-cfd uses it consistently).
