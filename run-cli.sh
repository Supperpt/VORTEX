#!/usr/bin/env bash
# VORTEX Aneurysm — CLI launcher
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Always source miniforge3/miniconda conda.sh so we use the user's conda
for _CONDA_SH in \
    "$HOME/miniforge3/etc/profile.d/conda.sh" \
    "$HOME/miniconda3/etc/profile.d/conda.sh" \
    "$HOME/anaconda3/etc/profile.d/conda.sh" \
    "/opt/conda/etc/profile.d/conda.sh"; do
    if [ -f "$_CONDA_SH" ]; then
        # shellcheck disable=SC1090
        source "$_CONDA_SH"
        break
    fi
done

if ! command -v conda &>/dev/null; then
    echo "ERROR: conda not found. Run 'bash setup.sh' first."
    exit 1
fi

# Resolve the environment path from conda's own registry.
ENV_PATH="$(conda info --base)/envs/vortex-aneurysm"
if [ ! -d "$ENV_PATH" ]; then
    echo "ERROR: conda environment 'vortex-aneurysm' not found at $ENV_PATH"
    echo "Run 'bash setup.sh' to create it."
    exit 1
fi

# Headless mode: avoid X11/Wayland interference
export QT_QPA_PLATFORM="offscreen"
export LIBGL_ALWAYS_SOFTWARE=1
export QT_LOGGING_RULES="*.debug=false;qt.qpa.*=false"

cd "$SCRIPT_DIR"
"$ENV_PATH/bin/python" -m vortex.cli "$@"
