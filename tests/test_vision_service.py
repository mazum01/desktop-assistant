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
