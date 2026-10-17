#!/usr/bin/env bash
# Shared build/upload selection: never mix an S3 image with a C6 upload target.
BOARD="${VERA_DISPLAY_BOARD:-s3-touch}"
BOARD_FLAGS=()
case "$BOARD" in
    s3-touch)
        DEFAULT_FQBN="esp32:esp32:esp32s3"
        BOARD_FLAGS=(--build-property "compiler.cpp.extra_flags=-DVERA_DISPLAY_S3_TOUCH=1")
        ;;
    touch) DEFAULT_FQBN="esp32:esp32:esp32c6" ;;
    legacy)
        DEFAULT_FQBN="esp32:esp32:esp32c6"
        BOARD_FLAGS=(--build-property "compiler.cpp.extra_flags=-DVERA_DISPLAY_LEGACY_LCD=1")
        ;;
    *) printf 'Unknown VERA_DISPLAY_BOARD: %s (expected s3-touch, touch, or legacy)\n' "$BOARD" >&2; exit 2 ;;
esac
FQBN="${VERA_ESP32_FQBN:-$DEFAULT_FQBN}"
if [[ "$FQBN" != "$DEFAULT_FQBN" && "$FQBN" != "$DEFAULT_FQBN":* ]]; then
    printf 'Board profile %s requires %s, not %s\n' "$BOARD" "$DEFAULT_FQBN" "$FQBN" >&2
    exit 2
fi
