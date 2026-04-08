#!/usr/bin/env bash
# VORTEX Aneurysm — environment bootstrap script
# Primary target: Fedora Linux
# Run once: bash setup.sh
#
# Uses conda-forge for vmtk because the vmtk PyPI package only has wheels
# for Python 3.6–3.8 and is no longer maintained on PyPI.
# conda-forge ships vmtk 1.4.0 for Python 3.9/3.10 on Linux.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="vortex-aneurysm"

echo "========================================"
echo "  VORTEX Aneurysm — Environment Setup"
echo "========================================"
echo ""

# ---------------------------------------------------------------------------
# Source conda init script if conda is not yet on PATH
# (needed when running as 'bash setup.sh' in a shell that hasn't sourced
# ~/.bashrc yet, e.g. a fresh terminal or zsh calling bash explicitly)
# ---------------------------------------------------------------------------
if ! command -v conda &>/dev/null; then
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
fi

# ---------------------------------------------------------------------------
# Check for conda / mamba
# ---------------------------------------------------------------------------
CONDA_BIN=""
if command -v mamba &>/dev/null; then
    CONDA_BIN="mamba"
elif command -v conda &>/dev/null; then
    CONDA_BIN="conda"
fi

if [ -z "$CONDA_BIN" ]; then
    echo "  ERROR: conda / mamba not found."
    echo ""
    echo "  vmtk is not available on PyPI for Python >= 3.9."
    echo "  The only reliable install path is conda-forge."
    echo ""
    echo "  Please install Miniforge3 first, then re-run this script:"
    echo ""
    echo "    curl -L https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh -o Miniforge3.sh"
    echo "    bash Miniforge3.sh          # follow prompts, initialise shell"
    echo "    source ~/.bashrc            # or open a new terminal"
    echo "    bash setup.sh"
    echo ""
    echo "  Miniforge3 is a minimal conda installer that uses conda-forge"
    echo "  by default and includes mamba for fast dependency solving."
    exit 1
fi

echo "  Found: $CONDA_BIN ($($CONDA_BIN --version 2>&1 | head -1))"
echo ""

# ---------------------------------------------------------------------------
# Phase 1: System packages (GL + Qt xcb libs — needed at runtime)
# ---------------------------------------------------------------------------
if command -v dnf &>/dev/null; then
    echo "[1/4] Installing system packages (requires sudo)..."
    sudo dnf install -y \
        mesa-libGL \
        mesa-libGL-devel \
        libXt \
        libXrender \
        xcb-util-keysyms \
        xcb-util-renderutil \
        xcb-util-image \
        xcb-util-wm \
        libxcb \
        curl
    echo "    System packages OK."
elif command -v apt-get &>/dev/null; then
    echo "[1/4] Installing system packages (Ubuntu/Debian, requires sudo)..."
    sudo apt-get install -y \
        libgl1-mesa-glx \
        libxt6 \
        libxrender1 \
        libxcb-keysyms1 \
        libxcb-render-util0 \
        libxcb-image0 \
        libxcb-icccm4 \
        libxcb1 \
        curl
    echo "    System packages OK."
else
    echo "[1/4] Unknown distro — skipping system package install."
fi

# ---------------------------------------------------------------------------
# Phase 2: Create conda environment
# ---------------------------------------------------------------------------
echo ""
echo "[2/4] Setting up conda environment '$ENV_NAME'..."

if $CONDA_BIN env list | grep -q "^${ENV_NAME} "; then
    echo "    Environment '$ENV_NAME' already exists — skipping creation."
    echo "    To recreate: conda env remove -n $ENV_NAME && bash setup.sh"
else
    echo "    Creating environment with Python 3.10..."
    $CONDA_BIN create -y -n "$ENV_NAME" python=3.10

    echo "    Installing vmtk from conda-forge (large download, ~1 GB, please wait)..."
    $CONDA_BIN install -y -n "$ENV_NAME" -c vmtk -c conda-forge vmtk

    echo "    Environment created OK."
fi

# ---------------------------------------------------------------------------
# Phase 3: Install remaining Python packages into the conda env
# ---------------------------------------------------------------------------
echo ""
echo "[3/4] Installing Python dependencies..."

# Get the python path inside the conda env
CONDA_PY="$($CONDA_BIN run -n $ENV_NAME which python)"
CONDA_PIP="$($CONDA_BIN run -n $ENV_NAME which pip)"

$CONDA_BIN run -n "$ENV_NAME" pip install --upgrade pip

$CONDA_BIN run -n "$ENV_NAME" pip install \
    SimpleITK \
    PyQt5 \
    PyQt5-sip \
    matplotlib \
    scipy \
    rich \
    prompt_toolkit

echo "    Dependencies OK."

# ---------------------------------------------------------------------------
# Phase 4: Smoke test
# ---------------------------------------------------------------------------
echo ""
echo "[4/4] Running smoke test..."
$CONDA_BIN run -n "$ENV_NAME" python - <<'PYEOF'
errors = []
for mod in ["vmtk", "vtk", "PyQt5", "SimpleITK", "numpy", "matplotlib", "scipy"]:
    try:
        __import__(mod)
        print(f"    OK  {mod}")
    except ImportError as e:
        print(f"    ERR {mod}: {e}")
        errors.append(mod)
if errors:
    print(f"\nFailed imports: {errors}")
    raise SystemExit(1)
print("\nAll imports OK.")
PYEOF

# ---------------------------------------------------------------------------
# Write run.sh to use the conda env
# ---------------------------------------------------------------------------
cat > "$SCRIPT_DIR/run.sh" <<RUNEOF
#!/usr/bin/env bash
# VORTEX Aneurysm — launcher
set -e

SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"

# Fix: force XCB platform on Fedora/Wayland
export QT_QPA_PLATFORM="\${QT_QPA_PLATFORM:-xcb}"
export QT_LOGGING_RULES="*.debug=false;qt.qpa.*=false"

cd "\$SCRIPT_DIR"
conda run -n ${ENV_NAME} python -m vortex.main "\$@"
RUNEOF
chmod +x "$SCRIPT_DIR/run.sh"

echo ""
echo "========================================"
echo "  Setup complete!"
echo ""
echo "  Launch the app with:  bash run.sh"
echo ""
echo "  Or activate the env manually:"
echo "    conda activate ${ENV_NAME}"
echo "    python -m vortex.main"
echo "========================================"
