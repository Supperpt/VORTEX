# Contributing to VORTEX

Thanks for your interest in improving VORTEX! This project aims to make
cerebral-aneurysm CFD model preparation reproducible and accessible, and to grow
the pool of openly available aneurysm-CFD data. Contributions from engineers,
researchers, and clinicians are all welcome.

## Getting set up

VORTEX targets **GNU/Linux** and depends on `vmtk`, which is only reliably
available via conda-forge.

```bash
# 1. Install Miniforge3 (if you don't already have conda)
curl -L https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh -o Miniforge3.sh
bash Miniforge3.sh && source ~/.bashrc

# 2. Create the environment and install dependencies
bash setup.sh

# 3. Run the interactive shell
./run-cli.sh shell
```

See [README.md](README.md) for the full workflow and command reference.

## Branching model

- **`main`** — stable, released code. This is what tagged releases come from.
- **`in_developement`** — work in progress; may be untested. Internal
  implementation notes (`LLM.md`) live here, not on `main`.

Please base pull requests on `main` and keep them focused.

## Making changes

1. Fork the repo and create a feature branch off `main`.
2. Keep the pipeline (`vortex/pipeline/`) free of UI dependencies — it must run
   headless. Do not add a separate `vtk` install; VTK is provided by `vmtk`
   (see `vortex/utils/vtk_compat.py`).
3. After mesh-affecting changes, validate with `check` / `check-mesh` and, where
   relevant, confirm the downstream OpenFOAM `checkMesh` behaviour.
4. Update `README.md` and `CHANGELOG.md` when behaviour or commands change.

## Reporting issues

Open a GitHub issue with:
- what you ran (command + parameters),
- what you expected vs. what happened,
- your OS/distro and how you installed (conda version, `vmtk` version),
- a minimal mesh or anonymised example if the problem is data-specific.

> **Privacy:** never attach clinical DICOM or patient-identifiable data to issues
> or commits. The `.gitignore` already excludes `DICOM/`, `*.dcm`, `*.stl`, etc.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
