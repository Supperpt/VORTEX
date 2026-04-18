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

VORTEX features an interactive shell that maintains state in memory, avoiding the need to reload DICOMs for each step. This allows you to visually pick your seed, tweak parameters, and view metrics dynamically.

```bash
./run-cli.sh shell
```

We recommend running the help command for a brief overview of functionalities and commands:

```bash
help
```

---

### Optimal Shell Workflow
For a standard CFD-ready aneurysm extraction, follow this sequence inside the shell:

1. **`load "/path/to/dicom"`** — Scans the folder and prompts you to select the best series.
2. **`seed`** — Opens a visual window. Find the aneurysm, click on it, and confirm. This saves the target coordinates.
3. **`segment`** — Extracts the 3D volume of the vessel from the raw DICOM image. *Think of this as painting the "blood" pixels white and everything else black. The result is a blocky, 3D pixel mask.*
4. **`mesh`** — Converts the segmented pixel mask into a 3D surface mesh (Marching Cubes + smoothing). *Think of this as wrapping a smooth, geometric skin of triangles around the blocky pixels. This creates the actual 3D model.*

   _4.5. **External Editing** — For most aneurysms there will be a need to edit the mesh in an external program (MeshLab/Meshmixer) to remove bone and clean/section the vessels. Export with `export raw.stl`, edit externally, then re-import with `load-mesh edited.stl`._

5. **`centerlines`** — Computes the mathematical centerlines of the vessel branches.
6. **`extend`** — Adds cylindrical flow extensions to the inlets/outlets and caps them.
7. **`metrics`** *(Optional)* — Computes morphological biomarkers (Aspect Ratio, Size Ratio) based on your seed point.
8. **`export my_aneurysm.stl`** — Saves the final 3D model.

**Where does the output go?**
By default, the `export <filename.stl>` command saves the file in your **current working directory** (the folder where you typed `./run-cli.sh shell`). You can also provide a full path: `export /home/user/Desktop/final_model.stl`.

### All Shell Commands
| Command | Description |
|---|---|
| `load <dir>` | Load a DICOM folder and select a series. Use quotes for paths with spaces. |
| `load-mesh <file.stl>` | Load an existing STL directly into the session, skipping segmentation. Useful after external editing. |
| `list` | Show the series available in the currently loaded folder. |
| `seed` | Open the visual seed picker to select the aneurysm location. |
| `status` | Show the pipeline dashboard (what is loaded and ready). |
| `params` | View and edit pipeline parameters (HU thresholds, `roi_radius`, `use_levelset`, etc.). |
| `segment` | Segment the DICOM volume using thresholds and the selected seed. |
| `mesh` | Generate the 3D surface mesh from the segmentation. |
| `centerlines` | Compute vessel centerlines. |
| `extend` | Add flow extensions and cap the model. |
| `metrics` | Calculate morphological metrics for the aneurysm. |
| `check` | Check mesh quality: manifold edges, open boundary loops, triangle quality, normal consistency. |
| `check --deep` | Same as `check`, plus self-intersection detection (slow). |
| `export <file>` | Save the final STL. Defaults to `output.stl` in the current directory. |
| `export-mask <file.nii.gz>` | Export the segmentation mask (NIfTI format) for AI or radiomics. |

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

VORTEX is designed to produce CFD-ready geometry directly compatible with OpenFOAM's `snappyHexMesh` mesher.

### 1. Export from VORTEX

Run the full pipeline in the interactive shell:

```
load "/path/to/dicom"
seed
segment
mesh
centerlines
extend
export aneurysm_cfd.stl
```

For OpenFOAM, use **split patches** so each boundary (wall, inlet, outlet) is a separate STL file:

```bash
./run-cli.sh process /path/to/dicom/folder \
    --centerlines \
    --flow-extensions \
    --split-patches \
    --output aneurysm_cfd.stl
```

This produces:
- `aneurysm_cfd_wall.stl` — vessel wall surface
- `aneurysm_cfd_cap_2.stl`, `aneurysm_cfd_cap_3.stl`, … — inlet/outlet caps

Or from the shell, set `split_patches = True` via `params`, then run `extend` and `export`.

### 2. Place Files in OpenFOAM Case

Copy the split STLs into `constant/triSurface/` in your OpenFOAM case directory.

### 3. Configure `snappyHexMeshDict`

Reference the STL files as named geometry regions:

```cpp
geometry
{
    wall.stl   { type triSurfaceMesh; name wall; }
    cap_2.stl  { type triSurfaceMesh; name inlet; }
    cap_3.stl  { type triSurfaceMesh; name outlet; }
}
```

Then define boundary conditions in `0/U`, `0/p`, etc. using those patch names.

> **Note:** VMTK assigns cap IDs geometrically — inspect the mesh in ParaView or OpenFOAM to determine which cap is the inlet vs. outlet before setting boundary conditions.

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
