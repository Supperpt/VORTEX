# Changelog

All notable changes to VORTEX are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/Supperpt/VORTEX/releases/tag/v1.0.0
