#!/usr/bin/env bash
# Download Piper voice models to config/piper/.
# Run this after cloning or whenever model files are missing.
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/config/piper"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

mkdir -p "$DEST"

download_voice() {
    local name="$1" path="$2"
    local onnx="$DEST/${name}.onnx"
    local json="$DEST/${name}.onnx.json"
    if [[ -f "$onnx" ]]; then
        echo "  [skip] ${name}.onnx already present"
    else
        echo "  Downloading ${name}.onnx ..."
        curl -fL --progress-bar -o "$onnx" "${BASE}/${path}/${name}.onnx"
    fi
    if [[ -f "$json" ]]; then
        echo "  [skip] ${name}.onnx.json already present"
    else
        curl -fsSL -o "$json" "${BASE}/${path}/${name}.onnx.json"
    fi
}

echo "=== Downloading Piper voice models ==="
download_voice "en_US-lessac-high"  "en/en_US/lessac/high"
download_voice "en_US-amy-medium"   "en/en_US/amy/medium"
echo "=== Done. Models saved to config/piper/ ==="
