#!/usr/bin/env python3
"""Generate a printable stereo calibration checkerboard target.

Produces an SVG (vector, resolution-independent) and a high-DPI PNG
that can be printed at 100% scale (no "fit to page") on A4 or US Letter.

The default parameters (9×6 inner corners, 25 mm squares) produce a
board that is 250 × 175 mm — fits comfortably on either A4 or Letter.

Usage
-----
    python3 scripts/make_calibration_target.py
    python3 scripts/make_calibration_target.py --square-mm 30 --cols 7 --rows 5
    python3 scripts/make_calibration_target.py --output docs/my_target

Output files (same base name)
------------------------------
    <output>.svg   — print this from a browser at 100% / "actual size"
    <output>.png   — 200 DPI raster; useful when SVG printing is unavailable
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _REPO_ROOT / "docs" / "stereo_calibration_target"

# A4/Letter safe margins (mm)
_MARGIN_MM = 15.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate printable stereo calibration target")
    p.add_argument("--cols",       type=int,   default=9,    help="Inner corner columns (default 9)")
    p.add_argument("--rows",       type=int,   default=6,    help="Inner corner rows (default 6)")
    p.add_argument("--square-mm",  type=float, default=18.0, help="Square size in mm (default 18 — fits A4/Letter portrait)")
    p.add_argument("--dpi",        type=int,   default=200,  help="PNG DPI (default 200)")
    p.add_argument("--output",     type=Path,  default=_DEFAULT_OUT,
                   help="Output path without extension (default: docs/stereo_calibration_target)")
    return p.parse_args()


# ── SVG generation ────────────────────────────────────────────────────────────

def make_svg(cols: int, rows: int, sq: float, margin: float) -> str:
    """Return SVG markup for a checkerboard with info text.

    Parameters
    ----------
    cols, rows : inner corner count (board squares = cols+1, rows+1)
    sq         : square size in mm
    margin     : white border in mm
    """
    num_sq_x = cols + 1
    num_sq_y = rows + 1
    board_w  = num_sq_x * sq
    board_h  = num_sq_y * sq
    total_w  = board_w + 2 * margin
    total_h  = board_h + 2 * margin + 18  # extra room for footer text

    lines: list[str] = []
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_w:.2f}mm" height="{total_h:.2f}mm" '
        f'viewBox="0 0 {total_w:.4f} {total_h:.4f}">'
    )

    # White background
    lines.append(f'<rect width="{total_w:.4f}" height="{total_h:.4f}" fill="white"/>')

    # Black squares
    for r in range(num_sq_y):
        for c in range(num_sq_x):
            if (r + c) % 2 == 0:
                x = margin + c * sq
                y = margin + r * sq
                lines.append(
                    f'<rect x="{x:.4f}" y="{y:.4f}" '
                    f'width="{sq:.4f}" height="{sq:.4f}" fill="black"/>'
                )

    # Thin border around the board area
    lines.append(
        f'<rect x="{margin:.4f}" y="{margin:.4f}" '
        f'width="{board_w:.4f}" height="{board_h:.4f}" '
        f'fill="none" stroke="black" stroke-width="0.3"/>'
    )

    # Reference scale bar: 50 mm line at bottom-right of margin
    bar_x   = margin + board_w - 50.0
    bar_y   = margin + board_h + 6.0
    lines.append(
        f'<line x1="{bar_x:.2f}" y1="{bar_y:.2f}" '
        f'x2="{bar_x + 50:.2f}" y2="{bar_y:.2f}" '
        f'stroke="black" stroke-width="0.5"/>'
    )
    for tick_x in (bar_x, bar_x + 50):
        lines.append(
            f'<line x1="{tick_x:.2f}" y1="{bar_y - 2:.2f}" '
            f'x2="{tick_x:.2f}" y2="{bar_y + 2:.2f}" '
            f'stroke="black" stroke-width="0.5"/>'
        )
    lines.append(
        f'<text x="{bar_x + 25:.2f}" y="{bar_y + 5:.2f}" '
        f'text-anchor="middle" font-size="3.5" font-family="sans-serif">50 mm</text>'
    )

    # Info text (bottom-left)
    info = (
        f"{cols}×{rows} inner corners  |  {sq:.0f} mm squares  |  "
        f"board {board_w:.0f}×{board_h:.0f} mm  |  "
        f"PRINT AT 100% (no fit-to-page)"
    )
    lines.append(
        f'<text x="{margin:.2f}" y="{bar_y + 5:.2f}" '
        f'font-size="3.2" font-family="sans-serif" fill="#333">{info}</text>'
    )

    lines.append("</svg>")
    return "\n".join(lines)


# ── PNG generation ────────────────────────────────────────────────────────────

def make_png(cols: int, rows: int, sq_mm: float, margin_mm: float, dpi: int) -> np.ndarray:
    """Return a checkerboard as a numpy BGRA image at the requested DPI."""
    px_per_mm = dpi / 25.4

    def mm2px(mm: float) -> int:
        return int(round(mm * px_per_mm))

    sq     = mm2px(sq_mm)
    margin = mm2px(margin_mm)
    num_sq_x = cols + 1
    num_sq_y = rows + 1
    footer_px = mm2px(12)

    w = margin * 2 + num_sq_x * sq
    h = margin * 2 + num_sq_y * sq + footer_px

    img = np.full((h, w, 3), 255, dtype=np.uint8)

    # Draw black squares
    for r in range(num_sq_y):
        for c in range(num_sq_x):
            if (r + c) % 2 == 0:
                x1 = margin + c * sq
                y1 = margin + r * sq
                img[y1:y1 + sq, x1:x1 + sq] = 0

    # Border
    cv2.rectangle(img, (margin, margin),
                  (margin + num_sq_x * sq, margin + num_sq_y * sq), 0, 1)

    # Footer text
    txt = (f"{cols}x{rows} inner corners | {sq_mm:.0f}mm squares | "
           f"board {(cols+1)*sq_mm:.0f}x{(rows+1)*sq_mm:.0f}mm | PRINT AT 100%")
    fy = margin + num_sq_y * sq + mm2px(7)
    cv2.putText(img, txt, (margin, fy),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 60, 60), 1, cv2.LINE_AA)

    # Scale bar (50 mm)
    bar_len  = mm2px(50)
    bar_x    = margin + num_sq_x * sq - bar_len
    bar_y    = fy + mm2px(4)
    cv2.line(img, (bar_x, bar_y), (bar_x + bar_len, bar_y), 0, 1)
    for tx in (bar_x, bar_x + bar_len):
        cv2.line(img, (tx, bar_y - mm2px(1.5)), (tx, bar_y + mm2px(1.5)), 0, 1)
    cv2.putText(img, "50 mm", (bar_x + bar_len // 2 - mm2px(5), bar_y + mm2px(4)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, 0, 1, cv2.LINE_AA)

    return img


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()

    cols, rows = args.cols, args.rows
    sq   = args.square_mm

    board_w = (cols + 1) * sq
    board_h = (rows + 1) * sq
    print(f"\nCalibration target: {cols}×{rows} inner corners, {sq:.0f} mm squares")
    print(f"Board size: {board_w:.0f} × {board_h:.0f} mm  (fits A4/Letter ≥ 210×148 mm)")

    # Warn if board is too big for A4 portrait (210×297) with 15mm margins each side
    if board_w > 180 or board_h > 267:
        print(f"WARNING: board ({board_w:.0f}×{board_h:.0f} mm) may not fit A4 portrait — "
              f"try --square-mm {int(min(180/(cols+1), 267/(rows+1)))} or print landscape")

    out = args.output
    out.parent.mkdir(parents=True, exist_ok=True)

    # SVG
    svg_path = out.with_suffix(".svg")
    svg_path.write_text(make_svg(cols, rows, sq, _MARGIN_MM), encoding="utf-8")
    print(f"\nSVG  → {svg_path}")

    # PNG
    png_path = out.with_suffix(".png")
    img = make_png(cols, rows, sq, _MARGIN_MM, args.dpi)
    # Embed DPI metadata
    params = [cv2.IMWRITE_PNG_COMPRESSION, 6]
    cv2.imwrite(str(png_path), img, params)
    print(f"PNG  → {png_path}  ({args.dpi} DPI, {img.shape[1]}×{img.shape[0]} px)")

    print("\nHow to print:")
    print("  SVG: open in Firefox/Chrome → Print → Scale 100% (NOT 'fit to page')")
    print("  PNG: print at 'actual size' / 100% scale")
    print(f"\nThen run:  python3 scripts/calibrate_stereo.py --square-mm {sq:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
