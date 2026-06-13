# VORTEX Aneurysm — Implementation State & Pivot Summary

_Last updated: June 8, 2026_

---

## ⚡ Current State: CLI-First Pivot

The project has transitioned from a GUI-centric development to a **CLI-first approach**. This pivot was necessary to isolate the core pipeline from X11/Wayland instability and to provide a more reliable tool for research workflows.

### 📁 Core Architecture
- **`vortex/cli.py`**: The primary entry point. Now includes a **Rich Interactive Shell** (`shell` command) and a standalone CLI.
- **`vortex/pipeline/`**: Pure logic (SimpleITK, VMTK, VTK). Zero UI dependencies.
- **`vortex/state/app_state.py`**: Shared dataclasses (`PipelineParams`) used by CLI, Shell, and GUI.
- **`run-cli.sh`**: Forces `QT_QPA_PLATFORM=offscreen` and `LIBGL_ALWAYS_SOFTWARE=1` for headless execution.

---

## 🛠️ Critical Challenges & Fixes

### 1. VMTK/VTK Dependency Conflict
- **Problem**: VMTK (via conda-forge) bundles its own VTK build. A separate `pip install vtk` causes `vtkmodules` conflicts and immediate segfaults.
- **Solution**: Never install `vtk` separately. All VTK imports are handled via `vortex/utils/vtk_compat.py`.

### 2. GUI Stability & X11/Wayland
- **Problem**: The PyQt5 GUI often fails to initialize GL or crashes due to system-level display server conflicts.
- **Solution**: CLI-first pivot. The **Interactive Shell** provides a stateful experience (keeping images/meshes in memory) without requiring a stable GL display.

### 3. Background Threading (PyQt5)
- **Problem**: Workers were being garbage-collected or failing to emit signals correctly in the GUI.
- **Solution**: The CLI/Shell avoids this complexity by running the pipeline synchronously while providing high-quality visual feedback via the `rich` library.

### 4. Interactive Shell & Matplotlib Quirks
- **Problem**: Native string splitting (`text.split()`) broke when DICOM paths had spaces. The slice viewer crashed on scroll due to deprecated `AxesImage.set_aspect` calls.
- **Solution**: Used `shlex.split` for robust shell command parsing. Updated `slice_viewer.py` to use `Axes.set_aspect` and correctly initialized `_mode`.
- **Problem**: Portuguese/Non-ASCII DICOM tags (e.g., 'Relatório', 'Série') with invalid surrogate characters caused `rich` console printing to throw `UnicodeEncodeError`.
- **Solution**: Updated `dicom_loader.py` to decode tags using `'ascii', 'ignore'` (falling back from utf-8) to ensure terminal tables never crash on malformed clinical metadata.
- **Problem**: `preview` command in shell threw `ImportError: cannot import name 'VTKViewerWidget' from 'vortex.ui.vtk_viewer'`.
- **Solution**: Fixed case-sensitivity bug in `vortex/cli.py`. Changed `VTKViewerWidget` (all caps 'VTK') to `VtkViewerWidget` to match the class definition.
- **Problem**: `centerlines` command in shell threw `ValueError: invalid literal for int() with base 10: ''`. This was caused by VMTK's internal `vmtkCenterlines` script trying to interactively prompt for source/target seed IDs when they weren't explicitly provided, even with `SeedSelectorName="openprofiles"`.
- **Solution**: Updated `vortex/pipeline/centerlines.py` to explicitly provide `SourceIds` and `TargetIds` using the boundary profiles already detected by our robust internal logic. This bypasses VMTK's interactive prompts and prevents the crash.
- **Project Hygiene**: Added a comprehensive `.gitignore` file to manage Python bytecode, virtual environments, IDE settings, and prevent clinical DICOM data from being committed.

### 8. Seed Point Required for Segmentation
- **Decision**: `segment` now requires a seed point (`params.seed_point_ijk`). If none is set, it raises a `ValueError` with a clear message directing the user to run `seed` first.
- **Why**: Running without a seed on a full 512×512×584 angio-CT with `--resample 2.0` caused an OOM kill (`Morto`) — `ConnectedComponent` on the upsampled volume needed 10–15 GB. Rather than implement a slow, memory-unsafe fallback for a case the user should never hit, the pipeline simply refuses and tells the user what to do.
- **Pipeline order**: Resample HU image (BSpline) → threshold → seed-based component filter → morphological closing. This is the original order and gives the best sub-voxel boundary accuracy.

### 5. Seed Point Coordinate Shift (Resampling / ROI Cropping)
- **Problem**: When a user selected a seed point `(i, j, k)` in the Interactive Shell via the `seed` command, the subsequent `segment` command often missed the aneurysm. This occurred because the `segment` command applied `--resample` (scaling the grid) or `--roi-radius` (shifting the origin) *before* using the `(i, j, k)` index to extract the connected component.
- **Solution**: In `vortex/pipeline/segmentation.py`, the `(i, j, k)` index is now immediately converted into a physical `(x, y, z)` coordinate in millimeters *before* any resampling or cropping. After the image grid is modified, the physical `(x, y, z)` point is converted *back* into a new local `(i', j', k')` index to ensure the component extraction perfectly targets the aneurysm regardless of grid changes.

### 6. MeshLab Rendering and `CellEntityIds`
- **Problem**: When the pipeline adds flow extensions (`vmtkCapper`), it injects `CellEntityIds` (1 for wall, 2+ for caps) into the mesh data. When saved as a binary `.stl` using `vtkSTLWriter`, VTK stores these IDs in the 2 unused attribute bytes of the STL format. Software like MeshLab interprets these bytes as RGB Face Colors, resulting in a mesh that appears almost completely black (e.g., RGB `(0,0,1)`).
- **Solution**: **DO NOT STRIP the `CellEntityIds`!** These labels are critical for downstream CFD pre-processors (e.g., ANSYS, Pointwise) to automatically detect boundary zones. To view the STL normally in MeshLab, instruct the user to change the Shading / Color mode from "Face Color" to "None" or "User-Defined".
  - *Alternative provided*: The interactive shell supports `--split-patches` (or `split_patches=True` in `params`), which uses `vtkThreshold` to separate the `CellEntityIds` into distinct `.stl` files (`model_wall.stl`, `model_cap_2.stl`, etc.) for solvers like OpenFOAM that require separate geometry files.

### 7. Selective Flow Extensions & Capping
- **Problem**: `vmtkFlowExtensions` by default attempts to extend all open boundaries on the mesh. Users often only need extensions on specific inlets or outlets (e.g., the parent artery but not small distal branches).
- **Solution**: 
    - **Interactive Selection**: The `centerlines` shell command now detects and labels boundary loops with IDs.
    - **Manual Capping**: In `vortex/pipeline/flow_extensions.py`, implemented `_cap_excluded_boundaries`. If the user provides a list of `selected_ids`, the pipeline manually triangulates caps (using `vtkContourTriangulator`) for all other boundaries *before* passing the mesh to VMTK. This ensures VMTK only "sees" and extends the requested vessels.
    - **Headless Support**: Added `--flow-ext-ids` to the `process` CLI command for non-interactive selective extensions.

### 17. `pick-seed` VTK Observer Issues (multiple rounds of fixes)

**Round 1 — crash with empty error `""`**:
- **Problem**: `_vtk_pick_seed_on_mesh()` in `vortex/cli.py` registered `AddObserver("LeftButtonPressEvent")` on the interactor and also manually called `style.OnLeftButtonDown()` inside the callback. When `SetInteractorStyle` is used, VTK already automatically dispatches mouse events to the style. The manual call caused the style handler to fire twice per click, crashing with an empty exception message (`""`).
- **Secondary symptom**: A `MatplotlibDeprecationWarning` about `FontProperties()` printed to the terminal on `render_window.Render()` — comes from VTK's internal text renderer using a deprecated matplotlib font API.
- **Solution**: Removed the manual `style.OnLeftButtonDown()` and `style.OnLeftButtonUp()` calls from the observer callbacks entirely. Wrapped the initial `render_window.Render()` in `warnings.catch_warnings()` to suppress the matplotlib noise.

**Round 2 — window opens but closes immediately (5 s), no interaction possible**:
- **Problem**: After removing the double-fire, the window opened but the user could not interact — `picked[0]` remained `None` and "Window closed without picking a seed." was printed after ~5 seconds. Two root causes identified:
  1. `LIBGL_ALWAYS_SOFTWARE=1` was being unset before opening the window. On systems where Mesa software rendering is required (forced by `run-cli.sh`), removing this variable causes VTK to try hardware GL, which can fail silently and produce a non-interactive (blank or crashing) window.
  2. `LeftButtonPress/Release` click detection was fragile — timing/position deltas and observer ordering with the trackball style made reliable click vs drag detection hard in VTK Python.
  3. `interactor.Initialize()` was missing before `interactor.Start()` — required in VTK Python for proper event loop setup.
- **Solution**:
  - Remove BOTH `QT_QPA_PLATFORM=offscreen` AND `LIBGL_ALWAYS_SOFTWARE=1` before opening the window, restore both on exit. `QT_QPA_PLATFORM=offscreen` prevents the window from appearing. `LIBGL_ALWAYS_SOFTWARE=1` blocks EGL hardware GL — EGL cannot create a real on-screen surface in software-only mode and falls back to an offscreen (windowless) context, so `interactor.Start()` returns immediately. Both must be cleared.
  - Replaced click-detection (`LeftButtonPress/Release` observers) with `KeyPressEvent` on `'p'` or `'space'` — standard VTK pick key, no observer ordering concerns. Mouse is still used for camera navigation via the trackball style.
  - Added `interactor.Initialize()` before `interactor.Start()`.
  - Updated all instruction text, README, and shell help strings accordingly: "hover over dome → press P to place seed".
- **UX after fix**: User rotates/zooms with mouse, positions cursor over the aneurysm dome, presses P to place a red sphere marker, can reposition by pressing P again, presses Q to confirm.

### 18. clip-sac Rewritten as a Centerline Bulge-Field Clip

**History of failed approaches** (all removed):
- **VMTK `vmtkSurfaceClipper` (Piccinelli)**: needs pristine, bifurcation-structured centerlines that externally edited meshes don't have. Produced degenerate/wrong splits.
- **Sphere-clip fallback**: modelled the neck as a sphere boundary (it isn't), with a fragile radius estimate. The sphere swallowed most of the vessel ("lost 90% of the STL"; "dome is just a fragment of the wall").
- Passing wall+extensions as the clip source also confused VMTK (extension inlets look like branchings).

**Current method — centerline bulge field** (`vortex/pipeline/sac_clipping.py`):
- Key fact: the parent-vessel centerline runs opening-to-opening through the lumen and **never enters the dome** (the dome is a dead-end bulge). VMTK centerlines carry `MaximumInscribedSphereRadius` (MISR) — the local vessel radius (the same array `vmtkFlowExtensions` uses).
- `compute_bulge_field(surface, centerlines)`: for each surface point, `bulge = distance_to_nearest_centerline_point / MISR_there`. Healthy wall ≈ 1.0, dome ≈ 1.5–2.5. Stored as point-data array `BulgeRatio`. Returns stats (median/p90/p99/max).
- `_smooth_point_scalar`: Laplacian-smooths `BulgeRatio` over mesh edges (5 iters) so the clip iso-contour (neck) is clean, not jagged.
- `clip_aneurysm_sac(surface, centerlines, seed_mm, ratio=1.4)`: `vtkClipPolyData` at the `ratio` iso-value → high side (dome candidates) + low side (parent). `vtkPolyDataConnectivityFilter` (closest-point region to seed) picks the dome; other high blobs are folded back into parent. The **seed is only used to choose which bulge is the dome**, so it no longer needs to be precise. Raises an informative error (no fallback) if `max(bulge) < ratio`.
- `export_bulge_heatmap`: writes a colour-mapped PLY (blue→red ramp on `BulgeRatio`) for inspection in MeshLab/Meshmixer/3D Slicer.
- **Tunable**: param `sac_bulge_ratio` (default 1.4) in `PipelineParams`; shell `clip-sac --ratio N` overrides per run. Higher → smaller dome (toward apex); lower → larger dome (toward neck). Every run prints field stats + dome/parent cell counts and writes `sac_bulge_heatmap.ply` in the cwd.
- **`split_patches` auto-enabled** by `clip-sac` so the next `export` writes dome+parent+caps without manual param editing.
- Flow extensions / bifurcations: the `clip-sac` handler clips the **extended wall** directly (EntityId==1 from `final_surface` = vessel wall + extensions, caps excluded) when `extend` has run, else the pre-extension `session.surface`. A single planar neck re-cut was tried first and was **wrong for bifurcation/terminal aneurysms** — the parent vessel and daughter outflow vessels sit on *both* sides of the neck plane, so the "keep the side farthest from seed" heuristic discarded one branch and its extensions. The bulge-field clip handles this correctly: the dome is one high-bulge region (removed via the seed) while every vessel and flow extension is either low-bulge or a *separate* high-bulge region and is folded back into parent. Extensions read as high-bulge (the centerline ends at the original opening, so distance grows along the extension) but form regions disjoint from the dome, so they stay in `parent_vessel`. Verified on a synthetic vessel+dome+extension. Caps (EntityId≥2) still come from `final_surface`.
- The heatmap PLY is a **diagnostic only** — never an export artifact. Users were confused seeing it layered over the real STLs in MeshLab; messaging now says "diagnostic; not exported".
- **Escape hatch**: if the field method can't separate an odd geometry, the documented fallback is a manual Meshmixer plane-cut (see README).
- **Removed**: `detect_aneurysm_seed`, `pick-seed`/`detect-aneurysm` commands, `_vmtk_clip`, `_estimate_sac_radius`, `_sphere_clip_fallback`.
- **Required workflow**: `centerlines` → `extend` → `set-seed X Y Z` → `clip-sac [--ratio N]` → `export`.
- **Verified** on a synthetic cylinder-with-bump: median bulge 1.0, max 2.5, clean 162/1818 dome/parent split, neck plane + heatmap produced.

---

## 🚀 Key Implementation Details

### Interactive Shell (`shell`)
Implemented a REPL using `prompt_toolkit` and `rich`:
- Maintains a `Session` object with loaded data.
- **Status Dashboard**: Automatically prints a `rich` Table indicating which pipeline components (DICOM, Seed, Mask, Surface, Centerlines, Caps) are currently in memory. Available via `status`.
- **Boundary Detection Table**: After `centerlines`, a table shows all detected openings with IDs, physical (x,y,z) coordinates, and estimated radii (mm).
- **Live Status Spinner**: Long-running C++ operations (ITK/VTK) lock the main Python thread, making standard `rich` progress bars appear frozen. The pipeline now uses `console.status` with a live spinner and text updates, which is much more reliable for background C++ processing.
- Supports incremental processing: `load` -> `segment` -> `mesh` -> `centerlines` -> `extend` -> `export`.
- This avoids re-loading and re-processing when only late-stage parameters (like flow extension ratio) need adjustment.

### 3D Preview (`preview`)
Added a native VTK 3D preview window directly inside the CLI shell:
- Allows the user to inspect the generated surface mesh (`mesh`) and overlaid `centerlines` without having to export the file to MeshLab first.
- Requires temporarily unsetting `QT_QPA_PLATFORM="offscreen"` to allow `xcb` rendering.

### Mesh Processing (`process-mesh`)
Added a command to apply VMTK operations to existing STL files:
- Loads STL using `vtkSTLReader`.
- Computes centerlines and adds extensions/capping.
- Useful for meshes generated in other tools (e.g., 3D Slicer, Horos).

### Surface Remeshing (`remesh`) for CFD-grade triangle quality
New shell command `remesh` (`vortex/pipeline/meshing.py:remesh_surface`):
- **Why:** A vortex-cfd run (patient AA_011) tripped OpenFOAM's checkMesh skewness gate. Root cause is surface quality — irregular/non-uniform STL triangulation in high-curvature regions forces snappyHexMesh into skewed boundary-layer cells. VORTEX already Taubin-smooths in `mesh`, but had **no uniform isotropic remeshing**, the standard vmtk CFD-prep step.
- **What:** optional Taubin smoothing pass (reuses `_taubin_smooth`) + `vmtkSurfaceRemeshing` (`ElementSizeMode="edgelength"`, `TargetEdgeLength=params.remesh_edge_length`, `PreserveBoundaryEdges=1` to keep openings clean for flow extensions) + largest-region + normals.
- **Where:** operates on `session.surface` (the open lumen) and **must run before `centerlines`/`extend`** — it does not touch cap `CellEntityIds` (none exist yet) and preserves the open boundary loops. The `remesh` handler resets `session.centerlines`/`profiles` (geometry changed) like `load-mesh` does. Works on a `load-mesh`-loaded STL too, so already-segmented geometries can be improved.
- **Params:** `remesh_edge_length` (mm, default 0.25, 0=skip) and `remesh_smooth_iterations` (default 20, 0=skip) in `PipelineParams`; editable via the `params` command (keys `edge` / `smooth`).
- **Tuning (documented in README "Tuning the `remesh` parameters"):**
  - `edge` ↓ = finer/more triangles, better curvature capture, heavier CFD mesh; ↑ = fewer/faster but if it exceeds the local curvature scale it re-introduces skew. Keep it ≤ the CFD near-wall cell size (~0.125 mm in vortex-cfd); ICA vessels ~0.2–0.3 mm, small domes 0.15–0.2 mm. Uniformity (not small size) is what fixes skew.
  - `smooth` ↑ = removes segmentation noise/spikes but can round off real blebs/daughter sacs; ↓/0 preserves detail. Taubin is volume-preserving (no sac shrinkage). `mesh` already smooths 30 iter → set `smooth 0` when remeshing a freshly-meshed surface; keep it on for raw `load-mesh` STLs.
  - Verify with `check` after remesh + the downstream OpenFOAM `checkMesh` skewness.
- **Verified:** vmtkSurfaceRemeshing available in the vortex-aneurysm env; smoke test on a real STL reduced triangle-area CV (0.57 → 0.28).

### Automated Morphological Metrics
Implemented a `metrics` command in the interactive shell:
- Relies on the `seed_point_ijk` to locate the aneurysm dome.
- Computes a local bounding box based on a distance heuristic.
- Calculates max diameter, height, neck estimate, Aspect Ratio, and Size Ratio.

### CFD Surface Patching & Labeling
Configured `vmtkCapper` to output `CellEntityIds`:
- Wall is typically tagged as `1`.
- Caps (inlets/outlets) are tagged `2`, `3`, etc.
- The `export` function has a `--split-patches` flag (or `split_patches=True` in params).
- When enabled, it uses `vtkThreshold` (shared helper `exporter.iter_caps`) to separate these IDs and write them to individual STLs. **Output uses bare names matching vortex-cfd's default scheme** (no basename prefix), written into the export directory: `aneurysm.stl` (dome), `wall.stl` (parent vessel), `neck_plane.json`, and one STL per cap.
- **Cap inlet/outlet labelling** is no longer left to the CFD pre-processor: the `cap_label` command (with the `view` viewer's `c` toggle to overlay cap numbers) records `session.cap_labels = {CellEntityId: 'inlet'|'outlet_N'}`. `export` then writes caps as `inlet.stl` / `outlet_1.stl` / … Unlabelled caps fall back to `cap_<uid>.stl`. VMTK still assigns the geometric IDs; `cap_label` adds the semantics once, interactively, so downstream vortex-cfd batch runs are unattended.

### 14. Mesh Quality Checks (`check` / `check-mesh`)
- **Feature**: Added `vortex/pipeline/mesh_quality.py` with `check_mesh_quality(mesh, deep=False) → dict`.
- **Shell command**: `check` (or `check --deep`) — operates on `session.final_surface` if available, else `session.surface`.
- **CLI command**: `check-mesh <file.stl> [--deep]`.
- **Checks performed**:
  - Basic stats: points, triangles, surface area (via `vtkMassProperties`), bounding box
  - Non-manifold edges via `vtkFeatureEdges` (BoundaryEdgesOff, NonManifoldEdgesOn)
  - Open boundary loops: `vtkFeatureEdges` (BoundaryEdgesOn) + `vtkPolyDataConnectivityFilter` to count distinct loops
  - Triangle quality (aspect ratio + min angle) via `vtkMeshQuality`; uses `vtk_np.vtk_to_numpy` for fast numpy aggregation
  - Normal consistency: compares existing normals against re-computed ones (`ConsistencyOn + AutoOrientNormalsOn`); uses numpy dot products to count flips; reports warning if >5% flipped
  - Self-intersections (deep only): tries `vtkPolyDataSelfIntersectionFilter` (VTK 9+), silently skips if unavailable
- **Design note**: `vtkMassProperties` requires a closed mesh for volume but surface area is valid for open meshes too. Triangle quality uses two separate `vtkMeshQuality` instances (one for aspect ratio, one for min angle) since `SetTriangleQualityMeasure*` is a single-mode filter.

### 9. `vtkIdList` Not Iterable in VMTK Centerlines
- **Problem**: `vmtkCenterlines` with `SeedSelectorName="profileidlist"` crashed with `'vtkmodules.vtkCommonCore.vtkIdList' object is not iterable`. VMTK's internal seed selector iterates over `SourceIds`/`TargetIds` with a Python `for` loop.
- **Solution**: Pass plain Python lists (`[0]` and `list(range(1, n))`) instead of `vtkIdList` objects. Fixed in `vortex/pipeline/centerlines.py`.

### 10. Flow Extension Mesh Explosion (`boundarynormal` mode)
- **Problem**: `vmtkFlowExtensions` with `ExtensionMode="boundarynormal"` and `AdaptiveNumberOfBoundaryPoints` (default True) caused the mesh to explode from ~99k to ~53M points, accompanied by many `vtkMath::Jacobi: Error extracting eigenfunctions` warnings. The boundary normal mode is sensitive to degenerate boundary geometry and can create excessively subdivided/long extensions.
- **Solution**: Switched to `ExtensionMode="centerlinedirection"` (derives extension axis from the actual vessel centerlines — more stable and more correct for CFD) and set `AdaptiveNumberOfBoundaryPoints=0`. Added a post-hoc sanity check warning if point count still explodes (>50× original). Fixed in `vortex/pipeline/flow_extensions.py`.

### 11. `vmtkCapper` Missing in VMTK 1.4 (conda-forge)
- **Problem**: `module 'vmtk.vmtkscripts' has no attribute 'vmtkCapper'` — the script was renamed to `vmtkSurfaceCapper` in the conda-forge VMTK 1.4 build.
- **Solution**: `_cap_surface()` helper in `flow_extensions.py` now tries `vmtkSurfaceCapper` → `vmtkCapper` → `vtkFillHolesFilter` in order. The VTK fallback is watertight but loses `CellEntityIds` patch labels.

### 12. Preview Black Window (Qt + VTK GLEW Conflict)
- **Problem**: The `preview` command opened a black window. Root cause: `vtkGenericOpenGLRenderWindow` fails with `GLEW could not be initialized: Missing GL version` in vmtk's bundled VTK build when used inside a `QOpenGLWidget`. The GLEW initialisation conflicts with Qt's OpenGL context management.
- **Solution**: Dropped Qt entirely for the preview. `do_preview()` in `vortex/cli.py` now uses `vtkRenderWindow` + `vtkRenderWindowInteractor` directly (native VTK X11 window via XWayland). Also temporarily unsets `LIBGL_ALWAYS_SOFTWARE` during preview to allow hardware GL acceleration.

### 13. `load-mesh` Command for External STL Re-import
- **Feature**: Added `load-mesh <file.stl>` to the interactive shell. Allows the user to export a mesh, edit it in an external tool (MeshLab, Blender, 3D Slicer), and re-import it into the session without restarting the pipeline.
- **Behaviour**: Reads the STL via `vtkSTLReader`, sets `session.surface` and `session.final_surface`, and clears `session.centerlines`/`session.profiles` (stale after an edit). DICOM image, seed point, and params are preserved.
- **Workflow**: `mesh` → `export raw.stl` → *(edit externally)* → `load-mesh edited.stl` → `centerlines` → `extend` → `pick-seed` (or `detect-aneurysm`) → `clip-sac` → `export final.stl`

### 15. Aneurysm Sac Clipping (`clip-sac`)
- **Feature**: New shell command `clip-sac` in `vortex/cli.py`. Calls `clip_aneurysm_sac()` from `vortex/pipeline/sac_clipping.py`.
- **Purpose**: Splits `session.surface` (pre-extension wall mesh) into an aneurysm dome patch and a parent vessel patch at the neck. Results are stored in `session.sac_surface`, `session.parent_vessel`, `session.neck_plane`. The main CFD pipeline state (`session.surface`, `session.final_surface`) is **not modified**.
- **Primary method**: `vmtkscripts.vmtkSurfaceClipper` — sets `.Surface` and `.Centerlines`, calls `.Execute()`. Outputs `.Surface` (one half) and `.ClippedSurface` (the other half). Whichever half's centroid is closest to `seed_mm` is labelled the sac.
- **Fallback**: If VMTK raises or returns empty geometry, `vtkClipPolyData` is used with a `vtkSphere` centred on `seed_mm`. Sphere radius = 60% of the estimated sac bounding-box max dimension from `estimate_aneurysm_geometry()`.
- **Neck plane**: Extracted from the open boundary ring of the sac surface via `vtkFeatureEdges(BoundaryEdgesOn)`. Centroid = mean of ring points; normal = smallest eigenvector of the ring's covariance matrix (PCA). Saved as `{'origin': [...], 'normal': [...]}`.
- **Export integration**: When `clip-sac` has been run and `split_patches=True`, `_export_cfd()` in `exporter.py` replaces the wall patch (EntityId == 1) with `{base}_aneurysm_dome.stl` + `{base}_parent_vessel.stl` + `{base}_neck_plane.json`. Cap patches (EntityId >= 2) are written unchanged. Without `clip-sac`, the existing `wall.stl` + caps behaviour is fully preserved.
- **Seed resolution** (updated): `clip-sac` no longer hard-requires DICOM. It resolves `seed_mm` in order: (1) `session.seed_mm` (from `pick-seed` or `detect-aneurysm`), (2) `seed_point_ijk` + `sitk_image` via `ijk_to_mm`. If neither is available, it prints a clear error listing the three seed commands.
- Same seed resolution applies to `metrics`.
- **Clip source** (updated): `clip-sac` now clips the extended wall, not the raw mesh. If `session.final_surface` has a `CellEntityIds` array (i.e. `extend` was run), the handler uses `vtkThreshold` to extract only EntityId==1 cells (vessel wall + flow extensions, no caps) and passes that to `clip_aneurysm_sac`. This ensures flow extensions end up in `parent_vessel` and are not dropped by the exporter. If `extend` has not been run (no `CellEntityIds`), a yellow warning is shown and `session.surface` is used as before. **Correct workflow**: `extend` must run before `clip-sac`.

### 16. Seed Point Without DICOM (`pick-seed` / `detect-aneurysm`)
- **Problem**: When the workflow starts from `load-mesh` (externally edited STL), no DICOM image is loaded and no seed point is set. `clip-sac` and `metrics` previously failed with a hard error in this case.
- **`session.seed_mm`**: New field on `Session` (in `vortex/cli.py`). Stores a seed point as `(x, y, z)` in world mm directly, independent of DICOM or IJK coordinates. Takes priority over `seed_point_ijk` in all seed resolution logic.

#### `pick-seed` (Option A — interactive)
- **Shell**: `pick-seed` command. **CLI**: `./run-cli.sh pick-seed <stl_file> [-o seed.json]`.
- **Implementation**: `_vtk_pick_seed_on_mesh(surface)` in `vortex/cli.py`. Opens a `vtkRenderWindow` with the mesh, temporarily unsets `QT_QPA_PLATFORM=offscreen` and `LIBGL_ALWAYS_SOFTWARE=1`. Uses `vtkCellPicker` bound to left-click release events. Drag vs click is distinguished by comparing mouse position at press vs release (≤4 px → click). A red sphere marker (radius = 1.2% of mesh diagonal) appears at the picked position and the text overlay updates with coordinates. Press Q to confirm.
- **Output**: Sets `session.seed_mm`. CLI mode prints coordinate and optionally writes `{"seed_mm": [x, y, z]}` to JSON.

#### `detect-aneurysm` (Option B — automatic)
- **Shell**: `detect-aneurysm` command. **CLI**: `./run-cli.sh detect-aneurysm <stl_file> [-o seed.json]`.
- **Implementation**: `detect_aneurysm_seed(surface, centerlines)` in `vortex/pipeline/sac_clipping.py`. Algorithm: for every mesh surface point, computes distance to its nearest centerline point (via `scipy.spatial.cKDTree`), normalised by the local `MaximumInscribedSphereRadius` (MISR). The mesh point with the highest normalised score is the aneurysm dome — it bulges farthest beyond the expected vessel wall. Falls back to raw distance if MISR is absent from centerlines.
- **Requires**: `centerlines` must be computed first (both shell and CLI compute them internally for the CLI command).
- **Output**: Sets `session.seed_mm`. CLI mode prints coordinate and optionally writes JSON. Shell shows a suggestion to verify with `pick-seed` if the result looks wrong.
- **Limitation**: May misidentify a vessel segment if the mesh contains large non-aneurysmal bulges. Always verify the detected point before relying on `clip-sac` output.

### 19. `export` Command — Missing `.stl` Extension & Wrong Output Directory

**Problem 1 — missing `.stl` extension**: When the user ran `export mymodel` (no extension), the file was written without the `.stl` suffix.
- **Fix**: `export_stl()` in `vortex/pipeline/exporter.py` already guards with `if not os.path.splitext(path)[1]: path += ".stl"`. Confirmed present.

**Problem 2 — directory path mangled**: When the user ran `export path/to/directory` (an existing directory or a path ending with `/`), `os.path.splitext` returned `""` for the extension, so `.stl` was appended directly to the directory path (e.g. `path/to/directory.stl`) instead of writing inside it.
- **Fix** (`vortex/pipeline/exporter.py`): Added a directory check *before* the extension check:
  ```python
  if os.path.isdir(path) or path.endswith('/') or path.endswith(os.sep):
      os.makedirs(path, exist_ok=True)
      path = os.path.join(path, "output.stl")
  elif not os.path.splitext(path)[1]:
      path = path + ".stl"
  ```

**Problem 3 — `sac_bulge_heatmap.ply` always written to CWD**: The heatmap was hardcoded to `os.path.abspath("sac_bulge_heatmap.ply")` during `clip-sac`, so when the user ran `export path/to/dir` the STL files went to `path/to/dir/` but the heatmap stayed in the program's working directory.
- **Fix** (`vortex/cli.py`, `export` command): After `export_stl` returns the resolved output path, if `session.bulge_surface` is set (i.e. `clip-sac` was run), the heatmap is re-written to the same directory as the exported STLs. The immediate post-`clip-sac` write to CWD is preserved for quick diagnostic inspection.

---

## ⚠️ Known Issues for Future LLMs

1. **GUI Rendering**: The interactive 3D viewer in the GUI depends on `QVTKRenderWindowInteractor`, which is notoriously finicky with Wayland. Recommend sticking to CLI for mesh generation.
2. **VMTK Availability**: VMTK is *only* reliable via the `vmtk` conda channel. Do not attempt to fix PyPI `vmtk` installs for Python 3.9+.
3. **Coordinate Systems**: DICOM (LPS) vs. VTK (XYZ) vs. SimpleITK (IJK). Coordinate conversions are handled in `dicom_loader.py` and `vtk_compat.py`. Always verify directions if orientation seems flipped.
4. **Flow extensions mesh quality**: `vmtkFlowExtensions` is sensitive to mesh cleanliness at vessel openings. Signs of bad input: Jacobi eigenvector warnings, point count explosion. Ensure the mesh has clean manifold geometry and well-formed closed boundary loops before calling `extend`. Running `mesh` with default smoothing (no `--reduce-mesh`) usually produces a clean enough input.
