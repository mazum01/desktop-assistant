#!/usr/bin/env python3
"""Stereo camera calibration script for the Desktop Assistant.

Runs fully headlessly — no display required.  Captures paired frames
automatically whenever a checkerboard is detected in both cameras, saves
annotated preview JPEGs to /tmp/stereo_cal_previews/ so you can inspect
them from another machine (scp / web browser), then calibrates and saves
the result to config/stereo_cal.npz.

IMPORTANT: Stop the core daemon before running this script so both cameras
are free.

    sudo systemctl stop desktop-assistant-core.service
    python3 scripts/calibrate_stereo.py --square-mm 18
    sudo systemctl start desktop-assistant-core.service

Usage
-----
    python3 scripts/calibrate_stereo.py [--board-cols 9] [--board-rows 6] \\
        [--square-mm 18] [--captures 20] [--output config/stereo_cal.npz]

    --interval-s   Seconds between auto-capture attempts (default 2.0)
    --no-preview   Skip saving annotated preview JPEGs (faster)

The script prints progress to stdout and exits 0 on success, 1 on failure.
Press Ctrl-C to abort (no output is saved).
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
_PREVIEW_DIR = Path("/tmp/stereo_cal_previews")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Headless stereo camera calibration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--board-cols", type=int, default=9,
                   help="Inner corners per row (default 9)")
    p.add_argument("--board-rows", type=int, default=6,
                   help="Inner corners per column (default 6)")
    p.add_argument("--square-mm", type=float, default=18.0,
                   help="Physical square size in mm (default 18)")
    p.add_argument("--captures", type=int, default=20,
                   help="Number of valid frame pairs to collect (default 20)")
    p.add_argument("--interval-s", type=float, default=2.0,
                   help="Seconds between auto-capture attempts (default 2.0)")
    p.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT,
                   help="Output .npz path")
    p.add_argument("--cam1-index", type=int, default=0,
                   help="Camera 1 v4l2 index (default 0)")
    p.add_argument("--cam2-index", type=int, default=2,
                   help="Camera 2 v4l2 index (default 2)")
    p.add_argument("--width", type=int, default=1280,
                   help="Capture width (default 1280)")
    p.add_argument("--height", type=int, default=720,
                   help="Capture height (default 720)")
    p.add_argument("--no-preview", action="store_true",
                   help="Skip saving annotated preview JPEGs")
    return p.parse_args()


def open_cameras(cam1_idx: int, cam2_idx: int, width: int, height: int):
    def _open(idx: int):
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"ERROR: Cannot open camera {idx}  (is the daemon still running?)")
            sys.exit(1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"  Camera {idx}: {actual_w}×{actual_h}")
        return cap

    print("Opening cameras …")
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
    """Annotate a frame for saving as a preview JPEG (no imshow)."""
    vis = frame.copy()
    color = (0, 255, 0) if found else (0, 100, 255)
    status = "CORNERS FOUND" if found else "No corners"
    cv2.putText(vis, f"{status}  {n_captured}/{n_needed}",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
    return vis


def save_preview(
    f1: np.ndarray, f2: np.ndarray,
    corners1, corners2,
    pattern_size: tuple[int, int],
    n: int,
    preview_dir: Path,
    both_found: bool,
) -> None:
    """Save annotated side-by-side preview JPEG for later inspection."""
    vis1 = f1.copy()
    vis2 = f2.copy()
    if corners1 is not None:
        cv2.drawChessboardCorners(vis1, pattern_size, corners1, True)
    if corners2 is not None:
        cv2.drawChessboardCorners(vis2, pattern_size, corners2, True)
    label = "OK" if both_found else "FAIL"
    for vis in (vis1, vis2):
        cv2.putText(vis, f"#{n:02d} {label}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (0, 255, 0) if both_found else (0, 80, 255), 2)
    combined = np.hstack([
        cv2.resize(vis1, (640, 360)),
        cv2.resize(vis2, (640, 360)),
    ])
    path = preview_dir / f"pair_{n:02d}_{label}.jpg"
    cv2.imwrite(str(path), combined, [cv2.IMWRITE_JPEG_QUALITY, 80])


def main() -> int:
    args = parse_args()
    pattern_size = (args.board_cols, args.board_rows)
    square_m = args.square_mm / 1000.0

    objp = np.zeros((args.board_rows * args.board_cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:args.board_cols, 0:args.board_rows].T.reshape(-1, 2) * square_m

    if not args.no_preview:
        _PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Preview JPEGs → {_PREVIEW_DIR}/")

    print(f"\nStereo calibration — target: {args.captures} frame pairs")
    print(f"Board: {args.board_cols}×{args.board_rows} inner corners, {args.square_mm}mm squares")
    print(f"Auto-capture every {args.interval_s}s when corners found in both cameras.")
    print("Ctrl-C to abort.\n")
    print("TIP: Hold the checkerboard so BOTH cameras can see it clearly,")
    print("     then slowly tilt/move it between captures for best calibration.\n")

    cap1, cap2 = open_cameras(args.cam1_index, args.cam2_index, args.width, args.height)

    obj_points:  list = []
    img_points1: list = []
    img_points2: list = []
    img_size: tuple[int, int] | None = None
    attempt = 0

    try:
        while len(obj_points) < args.captures:
            # Flush stale frames from buffer
            for _ in range(3):
                cap1.grab()
                cap2.grab()

            ret1, f1 = cap1.retrieve() if cap1.grab() else (False, None)
            ret2, f2 = cap2.retrieve() if cap2.grab() else (False, None)

            # Fallback: plain read()
            if not ret1 or f1 is None:
                ret1, f1 = cap1.read()
            if not ret2 or f2 is None:
                ret2, f2 = cap2.read()

            if not ret1 or not ret2 or f1 is None or f2 is None:
                print("Camera read failed — retrying …")
                time.sleep(1.0)
                continue

            if img_size is None:
                img_size = (f1.shape[1], f1.shape[0])

            attempt += 1
            found1, corners1 = find_corners(f1, pattern_size)
            found2, corners2 = find_corners(f2, pattern_size)
            both_found = found1 and found2

            n_cap = len(obj_points)

            if not args.no_preview:
                save_preview(f1, f2, corners1, corners2, pattern_size,
                             attempt, _PREVIEW_DIR, both_found)

            if both_found and corners1 is not None and corners2 is not None:
                obj_points.append(objp.copy())
                img_points1.append(corners1)
                img_points2.append(corners2)
                n_cap = len(obj_points)
                print(f"  [{n_cap:2d}/{args.captures}] ✓ pair captured"
                      + (f"  (preview: pair_{attempt:02d}_OK.jpg)" if not args.no_preview else ""))
            else:
                cam_status = ("cam1=NO " if not found1 else "cam1=OK ") + \
                             ("cam2=NO" if not found2 else "cam2=OK")
                print(f"  [{n_cap:2d}/{args.captures}] ✗ no valid pair  {cam_status}"
                      + (f"  (preview: pair_{attempt:02d}_FAIL.jpg)" if not args.no_preview else ""))

            time.sleep(args.interval_s)

    except KeyboardInterrupt:
        print("\nAborted — no output saved.")
        cap1.release()
        cap2.release()
        return 1

    cap1.release()
    cap2.release()

    if img_size is None or len(obj_points) < 8:
        print(f"\nNot enough valid pairs ({len(obj_points)}) — need at least 8. Aborting.")
        return 1

    print(f"\nReached {args.captures} pairs — running calibration …")
    rms = run_calibration(obj_points, img_points1, img_points2,
                          img_size, square_m, args.output)

    if rms <= 1.0:
        print("\nCalibration SUCCESSFUL ✓")
    else:
        print(f"\nCalibration complete but RMS={rms:.3f} px is high.")
        print("Consider recapturing with better lighting / less motion blur.")

    if not args.no_preview:
        print(f"\nInspect captures: scp pi@vera:{_PREVIEW_DIR}/*.jpg /tmp/")

    return 0


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


if __name__ == "__main__":
    sys.exit(main())
