#!/usr/bin/env bash
# VORTEX Aneurysm — launcher
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Fix: force XCB platform on Fedora/Wayland
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
export QT_LOGGING_RULES="*.debug=false;qt.qpa.*=false"

cd "$SCRIPT_DIR"
conda run -n vortex-aneurysm python -m vortex.main "$@"
