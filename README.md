# VORTEX Aneurysm

**Cerebral aneurysm 3D model pipeline — DICOM → STL for CFD/FSI/3D printing**

VORTEX (Vascular Output & Real-time Thresholding EXtraction) is a tool for processing angio-CT or angio-MR DICOM images into high-quality STL meshes. It is designed to produce watertight models ready for OpenFOAM rigid-wall CFD, FSI simulations, or 3D printing.
It is designed to be used in GNU/Linux systems. It has not been tested in MacOS/Windows.

---

## Installation

### 1. Install Miniforge3 (Required)
The `vmtk` library is only available via conda-forge for modern Python versions.

```bash
curl -L https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh -o Miniforge3.sh
bash Miniforge3.sh
source ~/.bashrc
```

### 2. Run Setup
```bash
bash setup.sh
```
This script creates the `vortex-aneurysm` conda environment and installs all dependencies (vmtk, SimpleITK, etc.).

---

## CLI-First Approach (Recommended)

To avoid GUI stability issues and X11/Wayland interference (especially on Linux), **VORTEX prioritises a CLI-driven workflow**. The CLI is robust, supports headless execution, and allows for precise parameter control.

---

## Interactive Shell (Best Experience)

VORTEX features an interactive shell that maintains state in memory, avoiding the need to reload DICOMs for each step.

```bash
./run-cli.sh shell
```

---

## Recommended Workflow (Research / CFD Use)

In practice, almost every cerebral aneurysm case requires **external mesh editing** to remove bone artifacts and section the vessels at clean cut planes before CFD meshing. The recommended workflow reflects this reality and is split into three phases.

### Phase 1 — Initial Segmentation (DICOM → raw STL)

```
load "/path/to/dicom"   ← select the angio-CT or angio-MR series
seed                    ← click the aneurysm in the slice viewer to anchor the segmentation
segment                 ← threshold + connected-component isolation
mesh                    ← Marching Cubes + smoothing → surface mesh
export raw_vessel.stl   ← save for external editing
```

### Phase 2 — External Editing (MeshLab / Meshmixer / 3D Slicer)

Open `raw_vessel.stl` in your preferred tool and:
- Remove bone and non-vascular structures
- Section the vessel at clean, planar cut planes
- Fix any mesh defects (non-manifold edges, holes)

Save the result (e.g. `edited_vessel.stl`).

### Phase 3 — CFD Preparation (back in VORTEX)

```
load-mesh "edited_vessel.stl"   ← re-import the cleaned mesh
centerlines                     ← compute vessel centrelines
extend                          ← add flow extensions + cap all openings
set-seed X Y Z                  ← type the dome coordinate (get it from MeshLab/Meshmixer)
clip-sac                        ← split wall into dome + parent via the bulge field
                                   (writes sac_bulge_heatmap.ply; split_patches auto-on)
clip-sac --ratio 2.5            ← re-tune from the printed stats until the split looks
                                   right (see "Tuning the --ratio" below)
export aneurysm_cfd.stl         ← write all patches
```

This produces, alongside the base file:

| File | Contents |
|---|---|
| `aneurysm_cfd_aneurysm_dome.stl` | Aneurysm dome surface |
| `aneurysm_cfd_parent_vessel.stl` | Parent vessel wall **including flow extensions** |
| `aneurysm_cfd_cap_2.stl`, `_cap_3.stl` … | Flat inlet/outlet caps |
| `aneurysm_cfd_neck_plane.json` | Neck plane origin + normal |
| `sac_bulge_heatmap.ply` | Bulge-field heatmap (inspect/tune the clip) |

#### How `clip-sac` works

The parent-vessel centreline runs from opening to opening through the lumen and
**never enters the aneurysm dome** (the dome is a dead-end bulge with no opening).
VMTK gives each centreline point a local vessel radius (MISR). So for every
surface point VORTEX computes a dimensionless **bulge ratio**:

```
bulge = distance-to-nearest-centreline / local-vessel-radius (MISR)
```

- Healthy vessel wall sits near **1.0** (the wall is roughly one radius from the axis).
- The aneurysm dome bulges out to **~1.5–2.5** (sometimes higher).

`clip-sac` removes the part of the wall whose bulge exceeds a threshold (the
`--ratio`). The **seed only chooses which high-bulge region is the dome** — so the
seed never needs to be precise, it just has to sit on the right bulge. This is why
it separates bifurcation/terminal aneurysms cleanly: each daughter vessel and flow
extension is a *separate* low- or high-bulge region and is kept in the parent;
only the dome region at the seed is removed.

#### Tuning the `--ratio` (the tune-and-look loop)

There is **no universal ratio** — the right value is per-case. Find it like this:

1. **Run `clip-sac`** once. It clips at the default ratio (`1.4`), writes
   `sac_bulge_heatmap.ply`, and prints a stats table, e.g.:

   ```
   Bulge ratio used                 1.40
   Dome cells                       3,120
   Parent cells                     18,400
   Bulge median / p90 / p99 / max   1.19 / 2.25 / 4.75 / 5.81
   ```

2. **Open `sac_bulge_heatmap.ply` in MeshLab / 3D Slicer.** It is a *diagnostic
   file only* (never part of the CFD export). The dome shows **red**, the healthy
   vessel **blue**. This tells you where the cut will land.

3. **Pick a ratio from the stats.** A good first guess sits between `p90` and `p99`
   — high enough to exclude the healthy wall, low enough to keep the whole dome.
   For the numbers above, try `--ratio 2.5`.

4. **Re-run `clip-sac --ratio N`** and read the new dome/parent cell counts. Repeat
   until the split looks right. Each run is cheap and fully replaces the previous
   result.

| Symptom in the result | What it means | Adjustment |
|---|---|---|
| Dome patch eats into the healthy vessel / outflow vessels | Ratio too **low** | **Raise** `--ratio` (e.g. `1.4` → `2.0` → `2.5`) |
| Dome patch is only a small cap near the apex; neck left on the vessel | Ratio too **high** | **Lower** `--ratio` (e.g. `2.5` → `2.0`) |
| `No surface bulge reaches the threshold …` error | Ratio is above the field's max | **Lower** `--ratio` below the reported max |
| A stray red blob at a vessel bend (not the dome) | Tortuous vessel; harmless | Leave it — it becomes its own region and stays in the parent; the seed keeps the dome correct |

**Rule of thumb:** *higher ratio → smaller dome (toward the apex); lower ratio →
larger dome (toward the neck).* Once you are happy, run `export`.

The chosen ratio is remembered for the session (and shown under `params` as
`sac_bulge_ratio`); pass `--ratio` again any time to override it.

> **Aneurysm types**: works best where the bulge contrast is strong
> (bifurcation/terminal aneurysms). **Sidewall** aneurysms have gentler contrast,
> so lean on the heatmap and step the ratio carefully.

> **Tip — getting the seed coordinate**: Open the STL in MeshLab, activate *Pick Points* mode (the crosshair icon, or *Edit → Pick Points*), click the aneurysm dome, and read the `X Y Z` from the dialog. Then type `set-seed X Y Z` in VORTEX.

> **Order matters**: `extend` must run before `clip-sac`, so the flow extensions are included in `parent_vessel.stl`.

> **If automatic clipping can't separate an unusual geometry**: cut the dome off manually in Meshmixer (*Edit → Plane Cut → Slice (Keep Both)*), export the two pieces, then `load-mesh` the parent and run `centerlines` → `extend` → `export` on it.

---

### Quick Workflow (Simple Cases — No External Editing)

For simple anatomies where the raw segmentation is clean enough for CFD directly:

```
load "/path/to/dicom"
seed
segment
mesh
centerlines
extend
export aneurysm.stl
```

---

### All Shell Commands
| Command | Description |
|---|---|
| `load <dir>` | Load a DICOM folder and select a series. Use quotes for paths with spaces. |
| `load-mesh <file.stl>` | Load an existing STL directly into the session, skipping segmentation. Useful after external editing. |
| `list` | Show the series available in the currently loaded folder. |
| `seed` | Open the DICOM slice viewer to click the aneurysm. **Requires DICOM loaded.** |
| `set-seed X Y Z` | Set the seed point from known world coordinates (read from MeshLab or Meshmixer). Works without DICOM. |
| `status` | Show the pipeline dashboard (what is loaded and ready). |
| `params` | View and edit pipeline parameters (HU thresholds, `roi_radius`, `use_levelset`, `split_patches`, etc.). |
| `segment` | Segment the DICOM volume using thresholds and the selected seed. |
| `mesh` | Generate the 3D surface mesh from the segmentation. |
| `centerlines` | Compute vessel centerlines. |
| `extend` | Add flow extensions and cap the model. Must run before `clip-sac`. |
| `metrics` | Calculate morphological metrics for the aneurysm. Works with any seed source. |
| `clip-sac [--ratio N]` | Split the wall into dome + parent vessel via the centerline bulge field. Writes `sac_bulge_heatmap.ply` and prints field stats; tune the split with `--ratio`. Run after `extend`. |
| `check` | Check mesh quality: manifold edges, open boundary loops, triangle quality, normal consistency. |
| `check --deep` | Same as `check`, plus self-intersection detection (slow). |
| `export <file>` | Save the final STL (and split patches if `split_patches = True`). Defaults to `output.stl`. |
| `export-mask <file.nii.gz>` | Export the segmentation mask (NIfTI format) for AI or radiomics. |

**Where does the output go?**
By default, `export <filename.stl>` saves to your **current working directory**. Provide a full path to save elsewhere: `export /home/user/Desktop/final.stl`.

---

## CLI Usage (Non-Interactive)

For scripted/batch use without the interactive shell.

### List DICOM Series
Scan a folder to find available series and their UIDs.
```bash
./run-cli.sh list-series /path/to/dicom/folder
```

### Generate a 3D Model (Basic)
Process the largest series in the folder with default settings (150–400 HU).
```bash
./run-cli.sh process /path/to/dicom/folder -o result.stl
```

### Check Mesh Quality
Validate a mesh before further processing or CFD export.
```bash
./run-cli.sh check-mesh model.stl
```

Add `--deep` to also run self-intersection detection (slow on large meshes):
```bash
./run-cli.sh check-mesh model.stl --deep
```

Reports:
- **Non-manifold edges** — CFD mesher will fail if any are found
- **Open boundary loops** — count of vessel openings (≥2 expected before `extend`)
- **Aspect ratio** (mean/max) and **min triangle angle**
- **Normal consistency** — detects incorrectly oriented faces
- **Self-intersections** (with `--deep` only)

### Process an Existing STL Mesh
Apply centerlines, flow extensions, and watertight capping to an already segmented STL.
```bash
./run-cli.sh process-mesh input.stl --output processed_mesh.stl --flow-ext-ratio 5.0
```

### Generate a CFD-Ready Model (Full Pipeline)
Includes centerlines, flow extensions, and watertight capping from DICOM.
```bash
./run-cli.sh process /path/to/dicom/folder \
    --centerlines \
    --flow-extensions \
    --flow-ext-ratio 5.0 \
    --output aneurysm_cfd.stl
```

### Exporting Split Patches (For CFD Boundary Conditions)
If you need the vessel wall and individual inlet/outlet caps as separate files (e.g., for OpenFOAM or ANSYS):
```bash
./run-cli.sh process /path/to/dicom/folder \
    --centerlines \
    --flow-extensions \
    --split-patches \
    --output model.stl
```
*This generates `model_wall.stl`, `model_cap_2.stl`, `model_cap_3.stl`, etc. VMTK assigns IDs geometrically — inspect in your CFD software to identify which cap is inlet/outlet.*

> **Aneurysm sac split:** If you run `extend` + `clip-sac` in the shell before exporting, the split-patches output replaces `model_wall.stl` with `model_aneurysm_dome.stl` + `model_parent_vessel.stl` (with flow extensions) and writes `model_neck_plane.json`. Without `clip-sac`, the classic `wall.stl` + caps output is unchanged.

### Advanced: ROI & Seed Isolation
If the scan contains multiple vessels, use a seed point to isolate the aneurysm.
```bash
./run-cli.sh process /path/to/dicom/folder \
    --seed-ijk 256,256,120 \
    --roi-radius 50 \
    --use-levelset \
    -o isolated_aneurysm.stl
```

---

## CLI Arguments Reference

### `process` (DICOM to STL)

| Argument | Default | Description |
|---|---|---|
| `folder` | (Required) | Path to the DICOM folder |
| `--series-uid` | Largest | Specific SeriesInstanceUID to load |
| `--output`, `-o` | `output.stl` | Output STL path |
| `--lower-threshold` | `150.0` | Minimum Hounsfield Unit (HU) |
| `--upper-threshold` | `400.0` | Maximum HU |
| `--resample` | `2.0` | Isotropic upsampling factor |
| `--seed-ijk` | `None` | `i,j,k` coordinates for component isolation |
| `--roi-radius` | `0.0` | mm radius for ROI cropping (requires `--seed-ijk`) |
| `--use-levelset` | `False` | Enable ITK Level-Set refinement |
| `--ls-iterations` | `1000` | Level-set max iterations |
| `--ls-curvature` | `0.7` | Level-set curvature scaling |
| `--ls-propagation` | `1.0` | Level-set propagation scaling |
| `--reduce-mesh` | `0.0` | Fraction of triangles to remove (0.0–1.0) |
| `--increase-mesh` | `0` | Number of Loop subdivision passes |
| `--centerlines` | `False` | Compute vessel centerlines |
| `--flow-extensions` | `False` | Add flow extensions and capping (requires `--centerlines`) |
| `--flow-ext-ratio` | `5.0` | Ratio of extension length to vessel radius |
| `--mode` | `cfd` | Output type: `cfd`, `fsi`, or `solid` |
| `--wall-thickness` | `0.2` | Wall thickness in mm (for `fsi` mode) |
| `--split-patches` | `False` | Split CFD output into separate STLs for wall and caps |

### `check-mesh` (Mesh Quality Report)

| Argument | Default | Description |
|---|---|---|
| `input_stl` | (Required) | Path to the STL file to check |
| `--deep` | `False` | Also run self-intersection detection (slow) |

### `process-mesh` (STL to Capped STL)

| Argument | Default | Description |
|---|---|---|
| `input_stl` | (Required) | Path to the input STL mesh |
| `--output`, `-o` | `processed_mesh.stl` | Output STL path |
| `--flow-ext-ratio` | `5.0` | Ratio of extension length to vessel radius |
| `--mode` | `cfd` | Output type: `cfd`, `fsi`, or `solid` |
| `--wall-thickness` | `0.2` | Wall thickness in mm (for `fsi` mode) |
| `--split-patches` | `False` | Split CFD output into separate STLs for wall and caps |

### `seed-picker`

| Argument | Default | Description |
|---|---|---|
| `folder` | (Required) | Path to the DICOM folder |
| `--series-uid` | Largest | Specific SeriesInstanceUID to load |

### `list-series`

| Argument | Default | Description |
|---|---|---|
| `folder` | (Required) | Path to the DICOM folder |

---

## OpenFOAM Workflow

VORTEX produces CFD-ready geometry directly compatible with OpenFOAM's `snappyHexMesh` mesher.

### 1. Export from VORTEX (with aneurysm sac split)

Follow the recommended workflow (Phase 1–3 above). `clip-sac` automatically enables `split_patches`:

```
load-mesh "edited_vessel.stl"
centerlines
extend
set-seed X Y Z
clip-sac                 # check the printed stats + sac_bulge_heatmap.ply
clip-sac --ratio 2.5     # re-tune until the dome/parent split is right
export aneurysm_cfd.stl
```

See [Tuning the `--ratio`](#tuning-the---ratio-the-tune-and-look-loop) for how to
choose the ratio. This produces:
- `aneurysm_cfd_aneurysm_dome.stl` — aneurysm dome surface
- `aneurysm_cfd_parent_vessel.stl` — parent vessel wall with flow extensions
- `aneurysm_cfd_cap_2.stl`, `aneurysm_cfd_cap_3.stl`, … — inlet/outlet caps
- `aneurysm_cfd_neck_plane.json` — neck plane geometry

If you do not need the dome/parent split, skip `clip-sac` and the export writes `aneurysm_cfd_wall.stl` instead.

### 2. Place Files in OpenFOAM Case

Copy the split STLs into `constant/triSurface/` in your OpenFOAM case directory.

### 3. Configure `snappyHexMeshDict`

```cpp
geometry
{
    aneurysm_dome.stl    { type triSurfaceMesh; name aneurysm_dome; }
    parent_vessel.stl    { type triSurfaceMesh; name parent_vessel; }
    cap_2.stl            { type triSurfaceMesh; name inlet; }
    cap_3.stl            { type triSurfaceMesh; name outlet; }
}
```

Then define boundary conditions in `0/U`, `0/p`, etc. using those patch names.

> **Note:** VMTK assigns cap IDs geometrically — inspect in ParaView or OpenFOAM to determine which cap is inlet vs. outlet before setting boundary conditions.

---

## Project Structure
- `vortex/cli.py`: Main entry point (CLI + interactive shell).
- `vortex/pipeline/`: Pure logic for segmentation, meshing, and VMTK operations.
- `vortex/state/`: Parameter and state definitions.
- `run-cli.sh`: Headless-friendly CLI launcher.

---

## Troubleshooting
- **Missing vmtk**: Ensure you used `setup.sh`. Do NOT `pip install vtk` manually.
- **X11 Errors**: Use `./run-cli.sh` which forces offscreen rendering.
- **Bone Leaking**: Increase `--lower-threshold` (e.g., to 250) or use `--seed-ijk`.
