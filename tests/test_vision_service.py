"""Stream overlay placement regressions."""

from unittest.mock import patch

import cv2
import numpy as np
import pytest

from src.services.vision_service import _draw_overlays


@pytest.mark.parametrize("width,height", [(320, 240), (640, 480), (1280, 720)])
@pytest.mark.parametrize("edge", ["top-left", "top-right", "bottom-right", "outside"])
@pytest.mark.parametrize("label", ["person", "traffic light", "a very long object description"])
def test_object_labels_fit_stream(width, height, edge, label):
    x, y = {
        "top-left": (0, 0),
        "top-right": (width - 12, 3),
        "bottom-right": (width - 12, height - 3),
        "outside": (-20, -10),
    }[edge]
    frame = np.zeros((height, width, 3), np.uint8)
    obj = {"bbox": [x, y, x + 30, y + 40], "label": label, "confidence": 0.93}
    with patch("src.services.vision_service._put_text_outlined") as draw:
        _draw_overlays(frame, [], [obj])
    draw.assert_called_once()
    _, text, (lx, ly), font, scale, color, thickness = draw.call_args.args
    assert text == f"{label} 93%"
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    assert lx >= 2
    assert lx + tw <= width - 2
    assert ly - th >= 2
    assert ly + baseline <= height - 2

    # Render on a larger canvas to catch actual glyph pixels outside the frame.
    border = 100
    canvas = np.zeros((height + 2 * border, width + 2 * border, 3), np.uint8)
    cv2.putText(canvas, text, (lx + border, ly + border), font, scale,
                color, thickness, cv2.LINE_AA)
    assert canvas[border:border + height, border:border + width].any()
    canvas[border:border + height, border:border + width] = 0
    assert not canvas.any()


# ── Overlay supersampling ────────────────────────────────────────────────────

def _encode_once(camera_config, frame, faces):
    import threading
    from src.core.bus import MessageBus
    from src.services.vision_service import VisionService
    svc = VisionService(bus=MessageBus(), camera_config=camera_config)
    svc._encode_queue.put((frame, None, faces, [], {}, None, 0, 180, 1, None))
    drawn = []
    with patch("src.services.vision_service._draw_overlays",
               side_effect=lambda img, f, o, d, **_: drawn.append((img.shape, f))):
        svc._encoder_running = True
        worker = threading.Thread(target=svc._encoder_loop)
        worker.start()
        try:
            for _ in range(100):
                if svc._latest_jpeg:
                    break
                threading.Event().wait(0.02)
        finally:
            svc._encoder_running = False
            worker.join(2)
    img = cv2.imdecode(np.frombuffer(svc._latest_jpeg, np.uint8), cv2.IMREAD_COLOR)
    return img, drawn


def test_overlays_are_drawn_on_upscaled_stream_frame():
    from src.vision.camera import CameraConfig
    cfg = CameraConfig(width=1280, height=720, stream_width=640, stream_height=360,
                       overlay_scale=2.0)
    frame = np.zeros((720, 1280, 3), np.uint8)
    face = {"bbox": [100, 200, 300, 400], "face_id": "a"}
    img, drawn = _encode_once(cfg, frame, [face])
    assert img.shape == (720, 1280, 3)
    (shape, faces), = drawn
    assert shape == (720, 1280, 3)
    # 1280 -> 640 stream downscale, then 2x overlay upscale: net identity.
    assert faces[0]["bbox"] == pytest.approx([100, 200, 300, 400])


def test_overlay_scale_one_keeps_stream_size():
    from src.vision.camera import CameraConfig
    cfg = CameraConfig(width=1280, height=720, stream_width=640, stream_height=360,
                       overlay_scale=1.0)
    img, drawn = _encode_once(cfg, np.zeros((720, 1280, 3), np.uint8),
                              [{"bbox": [100, 200, 300, 400]}])
    assert img.shape == (360, 640, 3)
    assert drawn[0][1][0]["bbox"] == pytest.approx([50, 100, 150, 200])


@pytest.mark.parametrize("bad", [{"overlay_scale": 0.5}, {"overlay_scale": 4.0},
                                 {"overlay_scale": float("nan")},
                                 {"stream_jpeg_quality": 10}])
def test_bad_stream_settings_are_rejected(bad):
    from src.core.bus import MessageBus
    from src.services.vision_service import VisionService
    from src.vision.camera import CameraConfig
    with pytest.raises(ValueError):
        VisionService(bus=MessageBus(), camera_config=CameraConfig(**bad))


def test_object_label_omits_confidence_when_hidden():
    frame = np.zeros((480, 640, 3), np.uint8)
    obj = {"bbox": [100, 100, 200, 200], "label": "cup", "confidence": 0.93}
    with patch("src.services.vision_service._put_text_outlined") as draw:
        _draw_overlays(frame, [], [obj], show_confidence=False)
    assert draw.call_args.args[1] == "cup"


def test_face_label_omits_match_percentage_when_hidden():
    frame = np.zeros((480, 640, 3), np.uint8)
    face = {"bbox": [100, 100, 200, 200], "face_id": "a", "name": "Sam", "match_score": 0.8}
    with patch("src.services.vision_service._put_text_outlined") as draw:
        _draw_overlays(frame, [face], [], show_confidence=False)
    assert [c.args[1] for c in draw.call_args_list] == ["Sam"]
    with patch("src.services.vision_service._put_text_outlined") as draw:
        _draw_overlays(frame, [face], [])
    assert [c.args[1] for c in draw.call_args_list] == ["Sam", "80%"]


def test_overlay_confidence_toggle_updates_service_and_announces():
    from src.core.bus import MessageBus
    from src.services.vision_service import VisionService
    from src.vision.camera import CameraConfig
    bus = MessageBus()
    svc = VisionService(bus=bus, camera_config=CameraConfig(overlay_show_confidence=False))
    assert svc.show_confidence is False
    seen = []
    bus.subscribe("vision.overlay_confidence_changed", lambda _t, p: seen.append(p))
    svc._on_set_overlay_confidence("vision.set_overlay_confidence", {"enabled": True})
    assert svc.show_confidence is True and seen == [{"enabled": True}]


def test_run_tick_skips_frames_already_processed():
    from src.core.bus import MessageBus
    from src.services.vision_service import VisionService

    class StalledCamera:
        is_ready = True
        hw_rotation_deg = 0
        frame = np.zeros((48, 64, 3), np.uint8)

        def capture_frames(self):
            return self.frame, None, 123.0

    bus = MessageBus()
    ready = []
    bus.subscribe("vision.frame_ready", lambda _t, p: ready.append(p["index"]))
    svc = VisionService(bus=bus, camera=StalledCamera())
    for _ in range(3):
        svc.run_tick()
    assert ready == [1] and svc.frame_index() == 1
