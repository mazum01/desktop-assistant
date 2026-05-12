#!/usr/bin/env bash
# Build the scrfd_decode_cpp pybind11 extension in-place.
# Run from repo root: bash scripts/build_scrfd_decode.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR="$(cd "$SCRIPT_DIR/../src/perception/scrfd_decode" && pwd)"

echo "Building scrfd_decode_cpp extension..."
cd "$EXT_DIR"
python3 setup.py build_ext --inplace 2>&1

SO=$(ls "$EXT_DIR"/scrfd_decode_cpp*.so 2>/dev/null | head -1)
if [[ -z "$SO" ]]; then
    echo "ERROR: .so not found after build." >&2
    exit 1
fi
echo "Built: $SO"
echo "Done."
