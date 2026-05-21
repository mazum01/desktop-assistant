#!/usr/bin/env python3
"""Stereo camera calibration script for the Desktop Assistant.

Captures paired frames from both cameras with a checkerboard visible in both,
then runs cv2.stereoCalibrate() and saves the results to config/stereo_cal.npz.

Usage
-----
    python3 scripts/calibrate_stereo.py [--board-cols 9] [--board-rows 6] \
        [--square-mm 50] [--captures 20] [--output config/stereo_cal.npz]

Controls
--------
    SPACE   — capture current frame pair (if corners found in both)
    r       — reject/discard last captured pair
    c       — run calibration now (requires ≥ 8 pairs)
    q / ESC — quit without saving
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = _REPO_ROOT / "config" / "stereo_cal.npz"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stereo camera calibration")
    p.add_argument("--board-cols", type=int, default=9, help="Inner corners per row (default 9)")
    p.add_argument("--board-rows", type=int, default=6, help="Inner corners per column (default 6)")
    p.add_argument("--square-mm", type=float, default=50.0, help="Physical square size in mm (default 50)")
    p.add_argument("--captures", type=int, default=20, help="Number of frame pairs to capture (default 20)")
    p.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT, help="Output .npz path")
    p.add_argument("--cam1-index", type=int, default=0, help="Camera 1 index (default 0)")
    p.add_argument("--cam2-index", type=int, default=2, help="Camera 2 index (default 2)")
    p.add_argument("--width", type=int, default=1280, help="Capture width (default 1280)")
    p.add_argument("--height", type=int, default=720, help="Capture height (default 720)")
    return p.parse_args()


def open_cameras(cam1_idx: int, cam2_idx: int, width: int, height: int):
    def _open(idx: int):
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            print(f"ERROR: Cannot open camera {idx}")
            sys.exit(1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    return _open(cam1_idx), _open(cam2_idx)


def find_corners(frame: np.ndarray, pattern_size: tuple[int, int]) -> tuple[bool, np.ndarray | None]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK
    found, corners = cv2.findChessboardCorners(gray, pattern_size, flags)
    if found:
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    return found, (corners if found else None)


def draw_status(frame: np.ndarray, found: bool, n_captured: int, n_needed: int) -> np.ndarray:
    vis = frame.copy()
    color = (0, 255, 0) if found else (0, 100, 255)
    status = "CORNERS FOUND — press SPACE to capture" if found else "No corners detected"
    cv2.putText(vis, status, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    cv2.putText(vis, f"Captured: {n_captured}/{n_needed}", (10, 65),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.putText(vis, "SPACE=capture  r=undo  c=calibrate  q=quit",
                (10, frame.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
    return vis


def run_calibration(
    obj_points: list,
    img_points1: list,
    img_points2: list,
    img_size: tuple[int, int],
    square_m: float,
    output_path: Path,
) -> float:
    print("\nRunning stereo calibration …")
    flags = (
        cv2.CALIB_FIX_INTRINSIC  # fix single-cam intrinsics already computed
    )

    # Individual camera calibration first
    ret1, K1, D1, _, _ = cv2.calibrateCamera(obj_points, img_points1, img_size, None, None)
    ret2, K2, D2, _, _ = cv2.calibrateCamera(obj_points, img_points2, img_size, None, None)
    print(f"  Cam1 RMS: {ret1:.4f}  Cam2 RMS: {ret2:.4f}")

    rms, K1, D1, K2, D2, R, T, E, F = cv2.stereoCalibrate(
        obj_points, img_points1, img_points2,
        K1, D1, K2, D2,
        img_size,
        criteria=(cv2.TERM_CRITERIA_MAX_ITER + cv2.TERM_CRITERIA_EPS, 100, 1e-5),
        flags=cv2.CALIB_FIX_INTRINSIC,
    )
    print(f"  Stereo RMS: {rms:.4f} px")

    # Compute rectification maps
    R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(
        K1, D1, K2, D2, img_size, R, T,
        flags=cv2.CALIB_ZERO_DISPARITY, alpha=0,
    )

    map1x, map1y = cv2.initUndistortRectifyMap(K1, D1, R1, P1, img_size, cv2.CV_32FC1)
    map2x, map2y = cv2.initUndistortRectifyMap(K2, D2, R2, P2, img_size, cv2.CV_32FC1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        output_path,
        K1=K1, D1=D1, K2=K2, D2=D2,
        R=R, T=T, E=E, F=F,
        R1=R1, R2=R2, P1=P1, P2=P2, Q=Q,
        map1x=map1x, map1y=map1y,
        map2x=map2x, map2y=map2y,
        image_size=np.array(img_size),
        square_m=np.float64(square_m),
        rms=np.float64(rms),
    )
    print(f"  Saved → {output_path}")
    print(f"  RMS reprojection error: {rms:.4f} px  ({'excellent' if rms < 0.5 else 'good' if rms < 1.0 else 'acceptable — consider recapturing'})")
    return rms


def main() -> int:
    args = parse_args()
    pattern_size = (args.board_cols, args.board_rows)
    square_m = args.square_mm / 1000.0

    # 3D object points for one checkerboard
    objp = np.zeros((args.board_rows * args.board_cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:args.board_cols, 0:args.board_rows].T.reshape(-1, 2) * square_m

    cap1, cap2 = open_cameras(args.cam1_index, args.cam2_index, args.width, args.height)

    obj_points: list = []
    img_points1: list = []
    img_points2: list = []

    print(f"\nStereo calibration — target: {args.captures} frame pairs")
    print(f"Board: {args.board_cols}×{args.board_rows} inner corners, {args.square_mm}mm squares")
    print("Hold checkerboard so BOTH cameras can see it clearly.")
    print("Press SPACE to capture, r to undo, c to calibrate early, q to quit.\n")

    img_size: tuple[int, int] | None = None

    while True:
        ret1, f1 = cap1.read()
        ret2, f2 = cap2.read()
        if not ret1 or not ret2:
            print("Camera read failed")
            break

        if img_size is None:
            img_size = (f1.shape[1], f1.shape[0])

        found1, corners1 = find_corners(f1, pattern_size)
        found2, corners2 = find_corners(f2, pattern_size)

        both_found = found1 and found2

        # Draw corners on previews
        vis1 = f1.copy()
        vis2 = f2.copy()
        if found1 and corners1 is not None:
            cv2.drawChessboardCorners(vis1, pattern_size, corners1, found1)
        if found2 and corners2 is not None:
            cv2.drawChessboardCorners(vis2, pattern_size, corners2, found2)

        vis1 = draw_status(vis1, both_found, len(obj_points), args.captures)
        vis2 = draw_status(vis2, both_found, len(obj_points), args.captures)

        # Stack side by side (scale down to fit screen)
        scale = 0.6
        h = int(vis1.shape[0] * scale)
        w = int(vis1.shape[1] * scale)
        vis1s = cv2.resize(vis1, (w, h))
        vis2s = cv2.resize(vis2, (w, h))
        combined = np.hstack([vis1s, vis2s])
        cv2.imshow("Stereo Calibration — Cam1 | Cam2", combined)

        key = cv2.waitKey(30) & 0xFF

        if key in (ord('q'), 27):  # q or ESC
            print("Quitting without saving.")
            break

        elif key == ord(' ') and both_found and corners1 is not None and corners2 is not None:
            obj_points.append(objp.copy())
            img_points1.append(corners1)
            img_points2.append(corners2)
            print(f"  Captured pair {len(obj_points)}/{args.captures}")
            time.sleep(0.3)  # brief pause to avoid duplicate captures
            if len(obj_points) >= args.captures:
                print(f"\nReached {args.captures} pairs — running calibration automatically …")
                run_calibration(obj_points, img_points1, img_points2,
                                img_size, square_m, args.output)
                break

        elif key == ord('r') and obj_points:
            obj_points.pop()
            img_points1.pop()
            img_points2.pop()
            print(f"  Removed last pair — {len(obj_points)} remaining")

        elif key == ord('c'):
            if len(obj_points) < 8:
                print(f"  Need at least 8 pairs (have {len(obj_points)})")
            else:
                run_calibration(obj_points, img_points1, img_points2,
                                img_size, square_m, args.output)
                break

    cap1.release()
    cap2.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
