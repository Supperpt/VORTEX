# Changelog

All notable changes to VORTEX are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- NIfTI input (`.nii` / `.nii.gz`): `load`, `list-series`, `seed-picker`,
  `sample-hu` and `process` accept a NIfTI file, and NIfTI files inside a
  scanned folder are listed alongside DICOM series. Volumes are reoriented to
  LPS on load so meshes align with world mm; 4-D files use the first volume.

## [1.1.0] — 2026-10-16

### Added
- `isolate`: automatically trims a segmented vessel tree to the aneurysm plus
  N parent-vessel diameters of each attached vessel, replacing manual
  sectioning in MeshLab/Meshmixer. Available in the shell (`isolate`,
  `isolate-params`) and as a one-off (`isolate in.stl --seed-mm=x,y,z -o out.stl`,
  `process --isolate`).
- Headless `remesh` subcommand (`--edge`, `--min-edge`, `--smooth`,
  `--uniform`, `--check`) for scripted/batch use without the interactive shell.
- `check` reports per-loop boundary radii and flags loops ≤15% of the
  largest as suspected tears, distinguishing real vessel openings from tears.
- `cap_label` warns on caps under 2% of the largest cap's area — a likely
  phantom cap from a tear — without silently dropping it.
- Windows 11 / WSL2 installation instructions in the README.

### Changed
- `remesh` is curvature-adaptive by default (`edge` 0.2–0.5 mm, derived from
  local mean curvature) instead of uniform `edge=0.25`, cutting the triangle
  budget roughly 3x while improving triangle quality on tested geometries.

### Fixed
- `remesh` left VTK normals splitting on its default, which tore the mesh
  along feature edges and produced spurious extra openings after
  `cap_label` (phantom caps).
- `remesh`'s adaptive sizing now tapers toward the local rim spacing near
  boundaries, removing slivers that formed where finely-discretised
  openings met coarse interior triangles.
- `remesh` no longer leaves stale `cap_labels`/`clip-sac` session state
  pointing at entity IDs that no longer exist after remeshing.
- Centerline extraction is rescued when VMTK's profile IDs don't match
  its own.
- `isolate`: fixed several defects found testing against real patient data —
  shared centerline stretches counted multiple times, the hole filter
  closing real small vessels, centerline hairpin detours, diameter measured
  on the wrong vessel, a cut taking two parallel vessels, and cuts reaching
  the dome on a vessel loop.
- Shell errors now stay on screen until Enter is pressed, instead of
  disappearing immediately.

### Validated
- Confirmed via a regression run (AA_010_wave, remeshed vs. non-remeshed
  baseline) that inserting `remesh` into the pipeline does not break
  downstream `snappyHexMesh`/`pimpleFoam` CFD solving.

## [1.0.0] — 2026-06-17

First public release.

### Highlights
- **DICOM → STL pipeline** for cerebral aneurysm CFD/FSI/3D printing: threshold +
  seed-based connected-component segmentation, Marching Cubes meshing, Taubin
  smoothing, and uniform surface remeshing for CFD-grade triangle quality.
- **Interactive shell (TUI)** that keeps the volume/mesh in memory across steps
  (`load`, `seed`, `segment`, `mesh`, `remesh`, `centerlines`, `extend`,
  `clip-sac`, `cap_label`, `check`, `export`, …).
- **Aneurysm sac clipping** (`clip-sac`) using a centerline bulge-ratio field, with
  a tunable `--ratio` and a diagnostic `sac_bulge_heatmap.ply`.
- **Flow extensions + capping** and split-patch export (wall / dome / parent /
  inlet / outlet caps) ready for OpenFOAM `snappyHexMesh`.
- **Mesh quality checks** (`check` / `check-mesh`): non-manifold edges, open
  boundary loops, aspect ratio, normal consistency, optional self-intersection.
- **Non-interactive CLI** (`process`, `process-mesh`, `list-series`, `check-mesh`,
  `seed-picker`) for scripted/batch use.

### Scope
- This release ships the **CLI and interactive shell only**. The earlier
  experimental PyQt5 GUI application was removed from the release; the tested,
  headless-friendly CLI/shell is the supported workflow.

[1.1.0]: https://github.com/Supperpt/VORTEX/releases/tag/v1.1.0
[1.0.0]: https://github.com/Supperpt/VORTEX/releases/tag/v1.0.0
