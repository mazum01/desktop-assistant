"""
Vision service.

Owns a `Camera` instance, runs a continuous capture loop, exposes the
latest frame to in-process subscribers, and publishes lightweight
frame metadata on the bus so other services can react without us
spamming megabyte-sized payloads through the pub/sub layer.

Architecture: two-thread pipeline to decouple capture rate from encode rate.
  • run_tick() (service thread) — capture frame (~2ms), store `self._latest`,
    publish `vision.frame_ready` so Hailo inference can start immediately.
  • _encoder_loop() (encoder thread) — copy + draw overlays + JPEG encode,
    then publish `vision.jpeg_ready` so the MJPEG stream delivers the new frame.
This avoids blocking the capture loop with GIL-contended cv2 operations
(copy 7-54ms, draw 17-95ms, encode 2-27ms) and pushes cam1 from ~11fps to
closer to the 30fps ISP delivery rate.

Topics published:
    vision.frame_ready    {"index": int, "shape": (H, W, C), "ts": float}
    vision.jpeg_ready     {"index": int}
    vision.error          {"reason": str}
    vision.lens_position  {"position": float}  — cam0 lens position (diopters), ~2 Hz

Topics subscribed:
    vision.capture_still  {"path": str}     — write a JPEG still to *path*
    perception.faces      — caches face bboxes for overlay drawing
    perception.objects    — caches object bboxes for overlay drawing

Public accessors (in-process callers):
    svc.latest_frame() → np.ndarray | None   (BGR, for detection)
    svc.latest_jpeg()  → bytes | None        (pre-encoded JPEG, for streaming)
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import List, Optional

import cv2
import numpy as np
try:
    from PIL import Image, ImageDraw, ImageFont as _ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

# Object box colour (BGR)
_CYAN = (255, 212, 0)   # #00d4ff

# Distinct face colours (BGR) — visually separated, readable on camera backgrounds
_FACE_COLORS = [
    (  0, 255, 136),   # green
    (255, 100,   0),   # blue
    (  0, 100, 255),   # red-orange
    (255,   0, 200),   # magenta
    (  0, 220, 255),   # yellow
    (200, 255,   0),   # lime
    (255, 160,  50),   # sky blue
    (128,   0, 255),   # purple
]


def _rotate_frame(frame: np.ndarray, degrees: int) -> np.ndarray:
    """Rotate *frame* by *degrees* clockwise.

    Multiples of 90° use cv2.rotate (lossless, may change dimensions).
    Other angles use cv2.warpAffine centred on the frame, keeping the same
    output canvas (corners are clipped by the rotation).
    """
    deg = degrees % 360
    if deg == 0:
        return frame
    if deg == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if deg == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    h, w = frame.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), -float(deg), 1.0)
    return cv2.warpAffine(frame, M, (w, h))


def _bg_luminance(frame: np.ndarray, x: int, y: int, tw: int, th: int) -> float:
    """Return perceived luminance [0..255] of the text bounding-box ROI.

    Uses BT.601 weights on the mean BGR of the clipped region.
    Falls back to 128 (mid-grey) if the ROI is empty.
    """
    h, w = frame.shape[:2]
    x1 = max(0, x)
    y1 = max(0, y - th)
    x2 = min(w, x + tw)
    y2 = min(h, y)
    if x2 <= x1 or y2 <= y1:
        return 128.0
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return 128.0
    mean_b, mean_g, mean_r = cv2.mean(roi)[:3]
    # BT.601 perceived luminance
    return 0.114 * mean_b + 0.587 * mean_g + 0.299 * mean_r


def _contrast_color(luma: float, accent: tuple | None = None) -> tuple:
    """Return a high-contrast text color for the given background luminance.

    If *accent* is provided and the background is dark, blend the accent toward
    white to ensure it stays bright.  On bright backgrounds always return near-black.
    """
    if luma > 128:
        # Bright background → dark text
        return (15, 15, 15)
    # Dark background → use accent brightened toward white, or pure white
    if accent is not None:
        b, g, r = accent
        # Boost each channel toward 255 so even dark accents stay readable
        b = min(255, int(b * 0.4 + 255 * 0.6))
        g = min(255, int(g * 0.4 + 255 * 0.6))
        r = min(255, int(r * 0.4 + 255 * 0.6))
        return (b, g, r)
    return (255, 255, 255)


def _put_text_outlined(
    frame: np.ndarray,
    text: str,
    org: tuple,
    font: int,
    font_scale: float,
    accent: tuple,
    thickness: int,
    force_accent: bool = False,
) -> None:
    """Draw *text* with high contrast on any background.

    Samples the background luminance of the text bounding-box:
      - Dark background: darkens a pill behind the text + draws bright text.
        A background rect is orders of magnitude faster than a thick stroke
        outline, and avoids the O(thickness²) cv2.putText cost.
      - Bright background: draws near-black text directly — no background
        treatment needed since the text already has natural contrast.

    When *force_accent* is True the accent colour is always used (with a dark
    background pill so it remains readable on any background).
    """
    (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)

    if force_accent:
        # Always darken a pill and draw in accent colour regardless of background
        pad = max(1, round(2 * font_scale))
        rx1 = max(0, org[0] - pad)
        ry1 = max(0, org[1] - th - pad)
        rx2 = min(frame.shape[1], org[0] + tw + pad)
        ry2 = min(frame.shape[0], org[1] + baseline + pad)
        roi = frame[ry1:ry2, rx1:rx2]
        if roi.size > 0:
            roi[:] = (roi >> 1)
        cv2.putText(frame, text, org, font, font_scale, accent, thickness, cv2.LINE_AA)
        return

    luma = _bg_luminance(frame, org[0], org[1], tw, th)
    color = _contrast_color(luma, accent)

    if luma <= 128:
        # Dark background — darken a rect behind the text for contrast
        pad = max(1, round(2 * font_scale))
        rx1 = max(0, org[0] - pad)
        ry1 = max(0, org[1] - th - pad)
        rx2 = min(frame.shape[1], org[0] + tw + pad)
        ry2 = min(frame.shape[0], org[1] + baseline + pad)
        roi = frame[ry1:ry2, rx1:rx2]
        if roi.size > 0:
            roi[:] = (roi >> 1)   # 50% darken — fast bitshift, no float conversion

    cv2.putText(frame, text, org, font, font_scale, color, thickness, cv2.LINE_AA)


def _face_color(face_id: str | None, index: int) -> tuple:
    """Return a consistent BGR colour for a face.

    Uses face_id hash for stability (same person → same colour across frames);
    falls back to round-robin index when no id is available.
    """
    if face_id:
        return _FACE_COLORS[hash(face_id) % len(_FACE_COLORS)]
    return _FACE_COLORS[index % len(_FACE_COLORS)]


# ── Face overlay size smoothing ──────────────────────────────────────────────
# Detection bboxes jitter frame-to-frame; smoothing the *size* (not the centre)
# keeps the overlay shapes stable while still tracking head movement responsively.
# Keyed by face_id → {"w": float, "h": float, "ts": float}.
_face_size_cache: dict = {}
_FACE_SIZE_ALPHA = 0.25      # EMA weight for new measurements (lower = smoother)
_FACE_SIZE_TTL_S = 2.0       # drop cached sizes not refreshed within this window


def _smoothed_face_size(face_id: str | None, w: float, h: float,
                        now: float) -> tuple:
    """Return EMA-smoothed (width, height) for a face to reduce size jitter.

    The centre position is intentionally NOT smoothed so the overlay still
    follows head motion immediately; only the shape dimensions are stabilised.
    Faces without a stable id are returned unsmoothed.
    """
    if not face_id:
        return w, h
    # Opportunistically prune stale entries
    if len(_face_size_cache) > 32:
        for k in [k for k, v in _face_size_cache.items()
                  if now - v["ts"] > _FACE_SIZE_TTL_S]:
            _face_size_cache.pop(k, None)
    prev = _face_size_cache.get(face_id)
    if prev is None or (now - prev["ts"]) > _FACE_SIZE_TTL_S:
        sw, sh = w, h
    else:
        a = _FACE_SIZE_ALPHA
        sw = prev["w"] * (1 - a) + w * a
        sh = prev["h"] * (1 - a) + h * a
    _face_size_cache[face_id] = {"w": sw, "h": sh, "ts": now}
    return sw, sh


# ── PIL ROI-patch text (degree symbol support, no full-frame conversion) ─────
_PIL_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_pil_font_cache: dict = {}

# ── Servo overlay pre-render cache ────────────────────────────────────────────
# Static elements (dark panel + arc + limit labels) rendered once per
# (frame_w, frame_h, servo_min, servo_max) combination.  Per-frame work is
# reduced to a cheap numpy copy + pure-C cv2 calls, eliminating the PIL GIL
# starvation that was capping cam1 at ~4 fps under Hailo load.
_servo_bg_cache: dict = {}    # key → (bgr_patch, alpha_patch, geom)
_servo_hdg_cache: dict = {}   # (lbl_size, angle_int) → pre-rendered BGR/alpha patch tuple


def _pil_font(size: int):
    """Return a cached PIL FreeType font, or None if PIL is unavailable."""
    if not _PIL_AVAILABLE:
        return None
    if size not in _pil_font_cache:
        try:
            _pil_font_cache[size] = _ImageFont.truetype(_PIL_FONT_PATH, size)
        except OSError:
            _pil_font_cache[size] = _ImageFont.load_default()
    return _pil_font_cache[size]


def _put_text_patch(
    frame: np.ndarray,
    text: str,
    xy: tuple,
    size: int,
    color_bgr: tuple,
) -> None:
    """Draw Unicode *text* onto a small PIL patch and alpha-composite onto *frame*.

    Only the text bounding-box ROI is converted — no full-frame BGR/RGB
    round-trip.  Falls back to _put_text_outlined (ASCII only) when PIL is
    unavailable.
    """
    font = _pil_font(size)
    if font is None:
        fs = max(0.4, size / 28.0)
        th = max(1, round(fs))
        _put_text_outlined(frame, text, (xy[0], xy[1] + size),
                           cv2.FONT_HERSHEY_SIMPLEX, fs, color_bgr, th)
        return

    fh, fw = frame.shape[:2]
    b, g, r = color_bgr

    dummy = Image.new("RGBA", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    pad = 2
    tw = bbox[2] - bbox[0] + pad * 2
    th = bbox[3] - bbox[1] + pad * 2
    if tw <= 0 or th <= 0:
        return

    x0, y0 = int(xy[0]), int(xy[1])
    x1 = max(0, x0)
    y1 = max(0, y0)
    x2 = min(fw, x0 + tw)
    y2 = min(fh, y0 + th)
    if x2 <= x1 or y2 <= y1:
        return

    patch = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(patch)
    ox = -bbox[0] + pad
    oy = -bbox[1] + pad
    d.text((ox + 1, oy + 1), text, font=font, fill=(0, 0, 0, 200))
    d.text((ox, oy), text, font=font, fill=(r, g, b, 255))

    arr = np.array(patch, dtype=np.float32)
    alpha = arr[:, :, 3:4] / 255.0
    patch_bgr = arr[y1 - y0: y2 - y0, x1 - x0: x2 - x0, [2, 1, 0]]
    patch_a   = alpha[y1 - y0: y2 - y0, x1 - x0: x2 - x0]
    roi = frame[y1:y2, x1:x2].astype(np.float32)
    frame[y1:y2, x1:x2] = np.clip(
        patch_bgr * patch_a + roi * (1.0 - patch_a), 0, 255
    ).astype(np.uint8)


def _scale_bboxes(detections: list, sx: float, sy: float) -> list:
    """Return a copy of detections with bbox coordinates scaled by (sx, sy).

    Called when the display frame is resized before overlay drawing so that
    face ovals and object boxes still align on the smaller stream frame.
    """
    if not detections or (sx == 1.0 and sy == 1.0):
        return detections
    out = []
    for d in detections:
        bbox = d.get("bbox")
        if bbox and len(bbox) >= 4:
            d = dict(d)
            d["bbox"] = [bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy]
        out.append(d)
    return out


def _confidence_ring_color(match_score: float, face_id: str | None) -> tuple:
    """Return BGR ring colour based on face-match confidence.

    Green  → high confidence (named face, score ≥ 0.70)
    Yellow → medium confidence (tentative match, 0.45 ≤ score < 0.70)
    Red    → low confidence / unknown face (score < 0.45 or no face_id)
    """
    if not face_id or match_score < 0.45:
        return (0, 0, 220)       # red
    if match_score < 0.70:
        return (0, 200, 220)     # yellow
    return (0, 200, 60)          # green


def _draw_hud_face(frame: np.ndarray, cx: int, cy: int, half_w: int, half_h: int,
                   bracket_color: tuple, ring_color: tuple, scale: float) -> None:
    """Draw a HUD-style sci-fi face detection overlay.

    Renders:
      - Four rounded corner markers at the corners of the padded bounding box.
        Each marker is a short arc (quarter-circle) with the curve on the inside
        corner — like the focus brackets on a camera viewfinder.
      - A fully-opaque circular ring centred on the face, coloured by confidence
        (green / yellow / red).
      - Thin tick marks at cardinal points on the ring.
    *(cx, cy)* is the face centre; *half_w/half_h* the half-extents of the padded
    box.  Pixel dimensions scale with *scale* (1.0 = 640×480 baseline).
    """
    thick = max(2, round(2 * scale))
    ring_r = max(6, int(min(half_w, half_h) * 0.92))

    # ── Rounded corner markers ────────────────────────────────────────────────
    # Place the 4 corners just outside the ring so the circle fits inside.
    gap = max(6, int(10 * scale))
    m = ring_r + gap            # half-width / half-height of the marker box
    r = max(5, int(m * 0.20))   # radius of each rounded corner arc
    arm = max(5, int(m * 0.30)) # length of each straight arm extending from arc

    left   = cx - m
    right  = cx + m
    top    = cy - m
    bottom = cy + m

    # Each corner = a quarter-circle arc (curve on the inside) + two straight
    # arms running along the box edges away from the corner toward the centre.
    # (arc_center, arc_start_deg, arc_end_deg, arm1_pts, arm2_pts)
    corners = [
        # top-left  "⌐"
        ((left + r,  top + r),    180, 270,
         ((left + r, top), (left + r + arm, top)),
         ((left, top + r), (left, top + r + arm))),
        # top-right "¬"
        ((right - r, top + r),    270, 360,
         ((right - r, top), (right - r - arm, top)),
         ((right, top + r), (right, top + r + arm))),
        # bottom-left "L"
        ((left + r,  bottom - r),  90, 180,
         ((left + r, bottom), (left + r + arm, bottom)),
         ((left, bottom - r), (left, bottom - r - arm))),
        # bottom-right "⌐ mirrored"
        ((right - r, bottom - r),   0,  90,
         ((right - r, bottom), (right - r - arm, bottom)),
         ((right, bottom - r), (right, bottom - r - arm))),
    ]
    for (center, a0, a1, arm1, arm2) in corners:
        cv2.ellipse(frame, (int(center[0]), int(center[1])), (r, r),
                    0, a0, a1, bracket_color, thick, cv2.LINE_AA)
        cv2.line(frame, (int(arm1[0][0]), int(arm1[0][1])),
                 (int(arm1[1][0]), int(arm1[1][1])), bracket_color, thick, cv2.LINE_AA)
        cv2.line(frame, (int(arm2[0][0]), int(arm2[0][1])),
                 (int(arm2[1][0]), int(arm2[1][1])), bracket_color, thick, cv2.LINE_AA)

    # ── Opaque circular ring (confidence-coloured) + tick marks ──────────────
    ring_thick = 1
    cv2.circle(frame, (cx, cy), ring_r, ring_color, ring_thick, cv2.LINE_AA)
    tick_len = max(4, int(8 * scale))
    tick_thick = max(1, round(1.5 * scale))
    for angle_deg in (0, 90, 180, 270):
        angle_rad = math.radians(angle_deg)
        ix = int(cx + ring_r * math.cos(angle_rad))
        iy = int(cy + ring_r * math.sin(angle_rad))
        ox = int(cx + (ring_r + tick_len) * math.cos(angle_rad))
        oy = int(cy + (ring_r + tick_len) * math.sin(angle_rad))
        cv2.line(frame, (ix, iy), (ox, oy), ring_color, tick_thick, cv2.LINE_AA)


def _draw_overlays(frame_bgr: np.ndarray, faces: list, objects: list,
                   face_depths: dict | None = None) -> None:
    """Draw HUD-style face overlays and object rectangles in-place on a BGR frame.

    All pixel dimensions scale with the frame resolution relative to a 640×480
    baseline so overlays look the same physical size regardless of capture res.
    face_depths maps face_id → depth_m for annotating faces with range.
    """
    h, w = frame_bgr.shape[:2]
    scale = min(w / 640.0, h / 480.0)
    if face_depths is None:
        face_depths = {}
    now = time.time()

    for idx, face in enumerate(faces):
        bbox = face.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        face_id = face.get("face_id")
        match_score = float(face.get("match_score", 0.0))
        bracket_color = _face_color(face_id, idx)
        ring_color = _confidence_ring_color(match_score, face_id)
        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])

        # Padded box dimensions, then smooth the SIZE (not the centre) to
        # eliminate frame-to-frame jitter while still tracking head movement.
        raw_w = (x2 - x1) * 1.36   # 1 + 2*0.18 padding
        raw_h = (y2 - y1) * 1.44   # 1 + 2*0.22 padding
        sw, sh = _smoothed_face_size(face_id, raw_w, raw_h, now)
        cx_l = (x1 + x2) // 2
        cy_l = (y1 + y2) // 2
        half_w = max(2, int(sw / 2))
        half_h = max(2, int(sh / 2))

        _draw_hud_face(frame_bgr, cx_l, cy_l, half_w, half_h,
                       bracket_color, ring_color, scale)

        name_label = face.get("name") or (face_id and "unknown") or None
        depth_m = face_depths.get(face_id)

        if name_label or depth_m is not None:
            # Mirror marker-box geometry from _draw_hud_face
            ring_r = max(6, int(min(half_w, half_h) * 0.92))
            gap = max(6, int(10 * scale))
            m = ring_r + gap   # marker box half-extents
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = max(0.50, 0.88 * scale)
            font_thick = max(1, round(scale))

            # Build lines: name / distance (m + ft) / confidence
            lines: list[str] = []
            if name_label:
                lines.append(name_label)
            if depth_m is not None:
                depth_ft = depth_m * 3.28084
                lines.append(f"{depth_m:.2f}m ({depth_ft:.1f}ft)")
            lines.append(f"{int(match_score * 100)}%")

            # Measure each line; find widest for overflow check
            sizes = [cv2.getTextSize(ln, font, font_scale, font_thick) for ln in lines]
            max_tw = max(s[0][0] for s in sizes)
            # Line height from first line; spacing = 140% of that
            first_th = sizes[0][0][1]
            line_spacing = max(1, int(first_th * 1.45))

            # Align TOP of first line with top of corner arm (cy_l - m).
            # cv2 baseline is *bottom* of text, so baseline = top + th
            ly_first = (cy_l - m) + first_th

            # Default: just right of the marker box
            lx = cx_l + m + 2
            if lx + max_tw > w:
                lx = max(0, cx_l - m - max_tw - 2)

            for i, (ln, (sz, _)) in enumerate(zip(lines, sizes)):
                th_i = sz[1]
                # keep each subsequent line's baseline below the previous
                ly = ly_first + i * line_spacing
                ly = max(th_i, min(h - 2, ly))
                _put_text_outlined(frame_bgr, ln, (lx, ly),
                                   font, font_scale, bracket_color, font_thick,
                                   force_accent=True)

    for obj in objects:
        bbox = obj.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        box_thick = max(1, round(scale))
        cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), _CYAN, box_thick, cv2.LINE_AA)
        conf  = obj.get("confidence", 0)
        label = f"{obj.get('label', '?')} {int(conf * 100)}%"
        ly    = max(10, y1 - 4)
        font_scale = max(0.8, 1.1 * scale)
        font_thick = max(1, round(scale))   # 1 at 640×480
        _put_text_outlined(frame_bgr, label, (x1, ly),
                           cv2.FONT_HERSHEY_SIMPLEX, font_scale, _CYAN, font_thick)


def _build_servo_bg_patch(
    w: int, h: int, servo_min: float, servo_max: float
) -> tuple:
    """Pre-render the static servo-overlay elements to a BGR + mask patch.

    Returns (bgr: H×W×3 uint8, mask: H×W uint8, geom: dict).
    Called once per unique (w, h, servo_min, servo_max); result cached in
    _servo_bg_cache.  All PIL / numpy-heavy work lives here, off the hot path.
    """
    scale = min(w / 640.0, h / 480.0)
    radius    = max(20, int(60 * scale))
    lbl_size  = max(12, int(18 * scale))
    off_x     = max(radius + lbl_size * 3, int(90 * scale))
    arc_thick = max(1, round(2 * scale))
    pad       = max(10, int(14 * scale))
    tick_len  = max(3, int(8 * scale))
    dot_r     = max(2, round(3 * scale))

    # off_y: distance from frame bottom to the arc center (cy_full).
    # Arc arcs upward (needs radius+pad above cy); heading label is placed
    # just below the center dot (needs dot_r + lbl_size + pad below cy).
    off_y     = max(dot_r + lbl_size + pad + 8, int(40 * scale))
    cx_full   = w - off_x
    cy_full   = h - off_y

    bx1 = max(0, cx_full - radius - pad - lbl_size * 2)
    by1 = max(0, cy_full - radius - pad)
    bx2 = min(w, cx_full + radius + pad + lbl_size * 2)
    by2 = min(h, cy_full + dot_r + lbl_size + pad)
    ph, pw = by2 - by1, bx2 - bx1
    if ph <= 0 or pw <= 0:
        return None

    # Local (patch-space) centre coordinates
    cx_l = cx_full - bx1
    cy_l = cy_full - by1

    # Render onto a black BGRA canvas so we carry alpha per-pixel
    canvas = np.zeros((ph, pw, 4), dtype=np.uint8)

    # Gray background arc
    cv2.ellipse(canvas, (cx_l, cy_l), (radius, radius), 0, 210, 330,
                (80, 80, 80, 255), arc_thick, cv2.LINE_AA)

    # Centre tick (straight up)
    cv2.line(canvas,
             (cx_l, cy_l - radius + tick_len - 2),
             (cx_l, cy_l - radius - 2),
             (120, 120, 120, 255), max(1, round(scale)), cv2.LINE_AA)

    # Limit labels — PIL (runs once; result stays in cache)
    bgr_view = canvas[:, :, :3]   # share memory with canvas for PIL writes
    for cv2_deg, limit_val in ((210, servo_min), (330, servo_max)):
        rad = math.radians(cv2_deg)
        ex  = int(cx_l + radius * math.cos(rad))
        ey  = int(cy_l + radius * math.sin(rad))
        lbl = f"{limit_val:.0f}\u00b0"
        if cv2_deg == 210:
            _put_text_patch(bgr_view, lbl,
                            (ex - lbl_size * 3, ey - lbl_size // 2),
                            lbl_size, _CYAN)
        else:
            _put_text_patch(bgr_view, lbl,
                            (ex + 4, ey - lbl_size // 2),
                            lbl_size, _CYAN)
        # Mirror written pixels into canvas alpha so they're fully opaque
        mask = np.any(bgr_view > 0, axis=2)
        canvas[:, :, 3] = np.where(mask, 255, canvas[:, :, 3])

    bgr       = canvas[:, :, :3].copy()
    mask_uint8 = canvas[:, :, 3]  # 0 = transparent, 255 = opaque

    geom = {
        "cx": cx_full, "cy": cy_full,
        "cx_l": cx_l,  "cy_l": cy_l,
        "bx1": bx1, "by1": by1, "bx2": bx2, "by2": by2,
        "radius": radius, "arc_thick": arc_thick,
        "lbl_size": lbl_size, "dot_r": dot_r,
    }
    return bgr, mask_uint8, geom


def _draw_servo_overlay(
    frame: np.ndarray,
    angle: float,
    servo_min: float,
    servo_max: float,
) -> None:
    """Draw a servo pan-angle indicator in-place on a BGR frame.

    Hot-path GIL budget: near-zero.
    Static elements (dark panel, arc, limit labels) are pre-rendered once into
    a cached BGRA patch and alpha-composited via numpy (C, GIL-free).
    Only the needle and heading label are redrawn each frame using pure cv2
    C-functions (also GIL-free).  PIL is invoked only on a cache miss
    (i.e., once per unique servo limit pair per frame resolution).
    """
    h, w = frame.shape[:2]
    cache_key = (w, h, round(servo_min, 1), round(servo_max, 1))
    if cache_key not in _servo_bg_cache:
        result = _build_servo_bg_patch(w, h, servo_min, servo_max)
        _servo_bg_cache[cache_key] = result
    cached = _servo_bg_cache[cache_key]
    if cached is None:
        return

    bg_bgr, bg_mask, geom = cached
    bx1, by1, bx2, by2 = geom["bx1"], geom["by1"], geom["bx2"], geom["by2"]
    cx, cy     = geom["cx"],  geom["cy"]
    radius     = geom["radius"]
    arc_thick  = geom["arc_thick"]
    lbl_size   = geom["lbl_size"]
    dot_r      = geom["dot_r"]

    # ── Dark semi-transparent background panel (pure C: releases GIL) ────
    roi = frame[by1:by2, bx1:bx2]
    if roi.size == 0:
        return
    # Darken existing frame content (roi *= 0.45)
    cv2.addWeighted(roi, 0.45, np.zeros_like(roi), 0.55, 0, roi)

    # ── Alpha-composite pre-rendered arc + limit labels ───────────────────
    # cv2.copyTo is a C-call (~1ms); replaces the ~9ms float32 blend path.
    cv2.copyTo(bg_bgr, bg_mask, roi)

    # ── Pointer needle (cv2 C-call, no GIL hold) ─────────────────────────
    servo_ctr  = (servo_min + servo_max) / 2.0
    half_range = max(1.0, (servo_max - servo_min) / 2.0)
    norm = max(-1.0, min(1.0, (angle - servo_ctr) / half_range))
    pointer_rad = math.radians(270.0 + norm * 60.0)
    px = int(cx + radius * math.cos(pointer_rad))
    py = int(cy + radius * math.sin(pointer_rad))
    cv2.line(frame, (cx, cy), (px, py), _CYAN, arc_thick, cv2.LINE_AA)
    cv2.circle(frame, (cx, cy), dot_r, _CYAN, -1, cv2.LINE_AA)

    # ── Current heading label (cached PIL patch, one per integer degree) ──
    angle_int = int(round(angle))
    hdg_key   = (lbl_size, angle_int)
    if hdg_key not in _servo_hdg_cache:
        # Render once; subsequent frames reuse the pre-composited array pair
        _render_hdg_patch(hdg_key)
    hdg_data = _servo_hdg_cache.get(hdg_key)
    if hdg_data is not None:
        hdg_bgr, hdg_mask, hdg_w, hdg_h = hdg_data
        tx = cx - hdg_w // 2          # horizontally centred on arc centre
        ty = cy + dot_r + 4            # just below the centre dot
        x1_h = max(0, tx);          y1_h = max(0, ty)
        x2_h = min(w, tx + hdg_w);  y2_h = min(h, ty + hdg_h)
        pw_h = x2_h - x1_h;        ph_h = y2_h - y1_h
        if pw_h > 0 and ph_h > 0:
            src_x = x1_h - tx;  src_y = y1_h - ty
            hdg_mask_sl = hdg_mask[src_y:src_y + ph_h, src_x:src_x + pw_h]
            roi_h = frame[y1_h:y2_h, x1_h:x2_h]
            cv2.copyTo(hdg_bgr[src_y:src_y + ph_h, src_x:src_x + pw_h], hdg_mask_sl, roi_h)


def _render_hdg_patch(key: tuple) -> None:
    """Pre-render one heading label into _servo_hdg_cache[key].

    Called at most once per unique (lbl_size, angle_int) — max 360 × few
    distinct label sizes.  Uses PIL so the degree symbol renders correctly.
    """
    lbl_size, angle_int = key
    lbl   = f"{angle_int}\u00b0"
    font  = _pil_font(lbl_size)
    if font is None:
        _servo_hdg_cache[key] = None
        return
    dummy = Image.new("RGBA", (1, 1))
    bbox  = ImageDraw.Draw(dummy).textbbox((0, 0), lbl, font=font)
    pad   = 2
    tw    = bbox[2] - bbox[0] + pad * 2
    th    = bbox[3] - bbox[1] + pad * 2
    if tw <= 0 or th <= 0:
        _servo_hdg_cache[key] = None
        return
    patch = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d     = ImageDraw.Draw(patch)
    ox    = -bbox[0] + pad
    oy    = -bbox[1] + pad
    d.text((ox + 1, oy + 1), lbl, font=font, fill=(0, 0, 0, 200))
    d.text((ox, oy),          lbl, font=font, fill=(_CYAN[2], _CYAN[1], _CYAN[0], 255))
    arr      = np.array(patch, dtype=np.uint8)
    bgr      = arr[:, :, [2, 1, 0]].copy()
    mask_u8  = arr[:, :, 3]  # 0 = transparent, 255 = opaque
    _servo_hdg_cache[key] = (bgr, mask_u8, tw, th)


class VisionService(Service):
    name = "vision"
    tick_seconds = 0.033   # ~30 fps frame-publish cadence; matches camera framerate

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        camera=None,
        camera_config=None,
        servo_min_deg: float = 135.0,
        servo_max_deg: float = 215.0,
    ) -> None:
        super().__init__(bus=bus)
        self._camera = camera
        self._camera_config = camera_config
        self._latest: Optional[np.ndarray] = None
        self._latest_jpeg: Optional[bytes] = None
        self._index = 0
        self._lock = threading.Lock()
        # Detection caches — written by bus callbacks, read in run_tick
        self._det_lock = threading.Lock()
        self._latest_faces: List[dict] = []
        self._latest_objects: List[dict] = []
        self._face_depths: dict = {}   # face_id → depth_m from vision.face_depth
        self._unsubs = []
        # Rotation — initialised from camera config, updated live via bus
        self._rotation_lock = threading.Lock()
        _init_rot = 0
        if camera_config is not None:
            _init_rot = int(getattr(camera_config, "rotation_deg", 0))
        self._rotation_deg: int = _init_rot % 360
        # Servo overlay state — updated by bus callbacks
        self._servo_lock = threading.Lock()
        self._servo_angle: Optional[float] = None
        self._servo_min_deg: float = float(servo_min_deg)
        self._servo_max_deg: float = float(servo_max_deg)
        # Stream downscale resolution (0 = no downscale; set from camera config)
        self._stream_width: int = 0
        self._stream_height: int = 0
        if camera_config is not None:
            self._stream_width = int(getattr(camera_config, "stream_width", 0))
            self._stream_height = int(getattr(camera_config, "stream_height", 0))
        # Background JPEG encoder — decouples copy+draw+encode from capture tick
        self._encode_queue: queue.Queue = queue.Queue(maxsize=1)
        self._encoder_running: bool = False
        self._encoder_thread: Optional[threading.Thread] = None


    def on_start(self) -> None:
        if self._camera is None:
            from src.vision.camera import Camera, CameraConfig
            cfg = self._camera_config or CameraConfig()
            self._camera = Camera(cfg)
        # Derive tick rate from the camera's configured framerate so the
        # service loop stays in sync when framerate changes at runtime.
        if self._camera_config is not None and self._camera_config.framerate > 0:
            self.tick_seconds = 1.0 / self._camera_config.framerate
        try:
            self._camera.start()
        except Exception:
            log.exception("camera.start() failed")
            self.bus.publish("vision.error", {"reason": "start_failed"})
            return

        self._unsubs.append(
            self.bus.subscribe("vision.capture_still", self._on_capture_still)
        )
        self._unsubs.append(
            self.bus.subscribe("perception.faces", self._on_faces)
        )
        self._unsubs.append(
            self.bus.subscribe("perception.objects", self._on_objects)
        )
        self._unsubs.append(
            self.bus.subscribe("vision.face_depth", self._on_face_depth)
        )
        self._unsubs.append(
            self.bus.subscribe("object.enabled_changed", self._on_object_enabled_changed)
        )
        self._unsubs.append(
            self.bus.subscribe("camera.set_rotation", self._on_set_rotation)
        )
        self._unsubs.append(
            self.bus.subscribe("camera.set_resolution", self._on_set_resolution)
        )
        self._unsubs.append(
            self.bus.subscribe("camera.set_stream_resolution", self._on_set_stream_resolution)
        )
        self._unsubs.append(
            self.bus.subscribe("motion.position", self._on_servo_angle)
        )
        self._unsubs.append(
            self.bus.subscribe("motion.limits_changed", self._on_servo_limits)
        )
        # Spawn background encoder thread
        self._encoder_running = True
        self._encoder_thread = threading.Thread(
            target=self._encoder_loop, daemon=True, name="vision-encoder"
        )
        self._encoder_thread.start()
        log.info(
            "VisionService started; hardware_ready=%s",
            getattr(self._camera, "hardware_ready", False),
        )

    @property
    def hardware_ready(self) -> bool:
        return bool(getattr(self._camera, "hardware_ready", False))

    @property
    def rotation_deg(self) -> int:
        with self._rotation_lock:
            return self._rotation_deg

    @property
    def resolution(self) -> tuple:
        if self._camera is not None:
            return self._camera.resolution
        cfg = self._camera_config
        return (cfg.width if cfg else 640, cfg.height if cfg else 480)

    @property
    def stream_resolution(self) -> tuple:
        return (self._stream_width, self._stream_height)

    def run_tick(self) -> None:
        if self._camera is None:
            return
        # Camera background thread hasn't deposited the first frame yet —
        # this is a normal startup race, not an error condition.
        if not getattr(self._camera, "is_ready", True):
            return
        try:
            frame = self._camera.capture_frame()
        except Exception:
            log.exception("capture_frame failed")
            self.bus.publish("vision.error", {"reason": "capture_failed"})
            return

        # Apply software rotation so detection receives the correctly-oriented frame.
        with self._rotation_lock:
            rot = self._rotation_deg
        if rot:
            frame = _rotate_frame(frame, rot)

        # Store clean frame for detection consumers and bump the frame index.
        with self._lock:
            self._latest = frame
            self._index += 1
            idx = self._index

        # Publish immediately — PerceptionService and ObjectService start
        # Hailo inference on this frame without waiting for JPEG encoding.
        self.bus.publish(
            "vision.frame_ready",
            {"index": idx, "shape": tuple(frame.shape), "ts": time.time()},
        )

        # Snapshot overlay state and hand off to the encoder thread.
        # Drop the frame if the encoder is still busy (queue full).
        with self._det_lock:
            faces = self._latest_faces
            objects = self._latest_objects
            face_depths = dict(self._face_depths)
        with self._servo_lock:
            servo_angle = self._servo_angle
            servo_min = self._servo_min_deg
            servo_max = self._servo_max_deg
        try:
            self._encode_queue.put_nowait(
                (frame, faces, objects, face_depths, servo_angle, servo_min, servo_max, idx)
            )
        except queue.Full:
            pass  # encoder is behind — drop this frame silently

    def _encoder_loop(self) -> None:
        """Background thread: copy frame → draw overlays → JPEG encode → publish.

        Runs independently from the service tick so that GIL-contended cv2
        operations (copy, draw, imencode) don't stall the capture loop.
        Frames dropped when encoder can't keep up are silently discarded;
        the MJPEG stream simply delivers the most recent encoded frame.

        If stream_width / stream_height are configured and smaller than the
        capture resolution, the frame is resized first (no frame.copy() needed —
        cv2.resize creates a new array).  Detection bbox coordinates are scaled
        accordingly so overlays still align with the resized stream frame.
        Hailo inference continues to receive full-resolution frames.
        """
        while self._encoder_running:
            try:
                item = self._encode_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            frame, faces, objects, face_depths, servo_angle, servo_min, servo_max, idx = item
            # Re-read stream dims each frame so GUI/CLI changes take effect immediately.
            sw, sh = self._stream_width, self._stream_height

            fh, fw = frame.shape[:2]
            if sw > 0 and sh > 0 and (fw > sw or fh > sh):
                # Maintain source aspect ratio — fit within (sw, sh) without distortion.
                # e.g. 1920×1080 (16:9) → 640×360, not 640×480 (which would stretch).
                ar = fw / fh
                tw = sw
                th = round(sw / ar)
                if th > sh:
                    th = sh
                    tw = round(sh * ar)
                # Resize first: creates a new array, no copy() needed.
                # Scale detection coordinates to match the display resolution.
                display = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_LINEAR)
                sx, sy = tw / fw, th / fh
                scaled_faces = _scale_bboxes(faces, sx, sy)
                scaled_objects = _scale_bboxes(objects, sx, sy)
            else:
                display = frame.copy()
                scaled_faces, scaled_objects = faces, objects

            _draw_overlays(display, scaled_faces, scaled_objects, face_depths)
            if servo_angle is not None:
                _draw_servo_overlay(display, servo_angle, servo_min, servo_max)

            ok, buf = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 70])
            with self._lock:
                self._latest_jpeg = bytes(buf) if ok else None

            self.bus.publish("vision.jpeg_ready", {"index": idx})

    def on_stop(self) -> None:
        self._encoder_running = False
        if self._encoder_thread is not None:
            self._encoder_thread.join(timeout=2.0)
            self._encoder_thread = None
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:
                log.exception("camera.close failed")
        log.info("VisionService stopped")

    # ── Public accessors ───────────────────────────────────────────────

    def latest_frame(self) -> Optional[np.ndarray]:
        """Return the most recent raw frame (BGR, no overlay). Used by detection services."""
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def latest_jpeg(self) -> Optional[bytes]:
        """Return the most recent pre-encoded JPEG (with overlays). Used by WebService."""
        with self._lock:
            return self._latest_jpeg

    def frame_index(self) -> int:
        with self._lock:
            return self._index

    # ── Bus handlers ───────────────────────────────────────────────────

    def _on_faces(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._det_lock:
            self._latest_faces = list(payload.get("faces", []))

    def _on_face_depth(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._det_lock:
            self._face_depths = {
                f["face_id"]: f["depth_m"]
                for f in payload.get("faces", [])
                if f.get("face_id") and f.get("depth_m") is not None
            }

    def _on_objects(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._det_lock:
            self._latest_objects = list(payload.get("objects", []))

    def _on_object_enabled_changed(self, _topic, payload) -> None:
        """Clear cached object detections immediately when detection is disabled."""
        if not isinstance(payload, dict):
            return
        if not payload.get("enabled", True):
            with self._det_lock:
                self._latest_objects = []

    def _on_capture_still(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "path" not in payload:
            return
        path = str(payload["path"])
        try:
            self._camera.capture_still(path)
            self.bus.publish("vision.still_saved", {"path": path})
        except Exception:
            log.exception("capture_still(%s) failed", path)
            self.bus.publish("vision.error", {"reason": "still_failed"})

    def _on_set_rotation(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "rotation_deg" not in payload:
            return
        deg = int(payload["rotation_deg"]) % 360
        with self._rotation_lock:
            self._rotation_deg = deg
        log.info("Camera rotation set to %d°", deg)
        self.bus.publish("camera.rotation_changed", {"rotation_deg": deg})

    def _on_set_resolution(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "width" not in payload or "height" not in payload:
            return
        w = int(payload["width"])
        h = int(payload["height"])
        if self._camera is None:
            return
        try:
            self._camera.set_resolution(w, h)
            log.info("Camera 1 resolution changed to %dx%d", w, h)
            self.bus.publish("camera.resolution_changed", {"width": w, "height": h})
        except Exception:
            log.exception("Failed to change camera 1 resolution to %dx%d", w, h)

    def _on_set_stream_resolution(self, _topic, payload) -> None:
        """Update the MJPEG stream downscale target without restarting the camera.

        Unlike camera.set_resolution (which restarts Picamera2 and may crop the
        sensor FOV), this only changes the encoder's output size.  The camera
        continues capturing at its current full-FOV mode; the encoder resizes
        frames to (width, height) before JPEG encoding.
        """
        if not isinstance(payload, dict) or "width" not in payload or "height" not in payload:
            return
        w = int(payload["width"])
        h = int(payload["height"])
        self._stream_width = w
        self._stream_height = h
        log.info("Stream resolution changed to %dx%d", w, h)

    def _on_servo_angle(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "angle" in payload:
            with self._servo_lock:
                self._servo_angle = float(payload["angle"])

    def _on_servo_limits(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._servo_lock:
            if "min_deg" in payload:
                self._servo_min_deg = float(payload["min_deg"])
            if "max_deg" in payload:
                self._servo_max_deg = float(payload["max_deg"])
        # Invalidate the static overlay background cache so it re-renders
        # with the new limits on the next frame.
        _servo_bg_cache.clear()

