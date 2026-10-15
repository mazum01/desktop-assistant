#!/usr/bin/env python3
"""Hailo hardware inference smoke test for faces, objects and mono depth.

Runs every Hailo-backed perception model on the attached accelerator
(Hailo-8 or Hailo-10H) and prints a JSON report. Exits non-zero on failure.

The accelerator must be free: stop desktop-assistant-core (and the watchdog,
so it does not restart core) before running, then start them again.

    python3 scripts/hailo_smoke_test.py [--image face.jpg] [--object-image street.jpg]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.perception.face_detector import FaceDetector  # noqa: E402
from src.perception.face_embedder import FaceEmbedder  # noqa: E402
from src.perception.hailo_inference import HailoInference  # noqa: E402
from src.perception.hailo_models import resolve_model  # noqa: E402
from src.perception.hailo_probe import probe_hailo  # noqa: E402
from src.perception.object_detector import ObjectDetector  # noqa: E402

_FACE_URL = "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/lena.jpg"
_FACE_CACHE = Path("/tmp/vera_hailo_smoke_face.jpg")
_OBJECT_URL = "https://ultralytics.com/images/bus.jpg"
_OBJECT_CACHE = Path("/tmp/vera_hailo_smoke_objects.jpg")


def _load_image(path: str | None, url: str, cache: Path) -> np.ndarray:
    if path is None:
        if not cache.exists():
            urllib.request.urlretrieve(url, cache)
        path = str(cache)
    image = cv2.imread(path)
    if image is None:
        raise SystemExit(f"Cannot read image: {path}")
    return image


def _timed(fn, frame, runs: int = 10) -> float:
    fn(frame)
    start = time.perf_counter()
    for _ in range(runs):
        fn(frame)
    return round((time.perf_counter() - start) / runs * 1000, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Image containing one frontal face (default: OpenCV sample)")
    parser.add_argument("--object-image", help="Image containing people and a bus (default: Ultralytics sample)")
    args = parser.parse_args()

    image = _load_image(args.image, _FACE_URL, _FACE_CACHE)
    object_image = _load_image(args.object_image, _OBJECT_URL, _OBJECT_CACHE)
    blank = np.zeros((480, 640, 3), np.uint8)
    noise = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    device = probe_hailo()
    report: dict = {"device": device.family, "checks": {}}
    checks = report["checks"]

    detector = FaceDetector()
    faces = detector.detect(image)
    checks["face_detection"] = {
        "backend": detector.backend,
        "model": str(resolve_model("face_detection")),
        "faces_on_sample": len(faces),
        "faces_on_blank": len(detector.detect(blank)),
        "faces_on_noise": len(detector.detect(noise)),
        "ms_per_frame": _timed(detector.detect, image),
        "ok": False,
    }
    checks["face_detection"]["ok"] = (
        detector.backend == "hailo"
        and len(faces) >= 1
        and faces[0].landmarks is not None
        and checks["face_detection"]["faces_on_blank"] == 0
        and checks["face_detection"]["faces_on_noise"] == 0
    )

    embedder = FaceEmbedder()
    similarity = None
    if embedder.hardware_ready and faces and faces[0].landmarks:
        scaled = cv2.resize(image, None, fx=0.7, fy=0.7)
        scaled_faces = detector.detect(scaled)
        if scaled_faces and scaled_faces[0].landmarks:
            first = embedder.embed(image, faces[0].landmarks)
            second = embedder.embed(scaled, scaled_faces[0].landmarks)
            similarity = round(float(first @ second), 3)
    checks["face_embedding"] = {
        "hardware": embedder.hardware_ready,
        "model": str(resolve_model("face_embedding")),
        "same_face_similarity": similarity,
        "ok": embedder.hardware_ready and similarity is not None and similarity > 0.8,
    }

    objects = ObjectDetector()
    detections = objects.detect(object_image)
    labels = sorted({d.label for d in detections})
    checks["object_detection"] = {
        "backend": objects.backend,
        "model": str(getattr(getattr(objects, "_engine", None), "_hef_path", None)),
        "labels_on_sample": labels,
        "ms_per_frame": _timed(objects.detect, object_image),
        "ok": objects.backend == "hailo" and {"person", "bus"} <= set(labels),
    }

    depth_model = resolve_model("mono_depth")
    depth_check: dict = {"model": str(depth_model), "ok": False}
    if depth_model is not None:
        engine = HailoInference(depth_model)
        depth_check["hardware"] = engine.hardware_ready
        if engine.hardware_ready:
            name = engine.input_info[0].name
            height, width = engine.input_info[0].shape[:2]
            rgb = cv2.cvtColor(cv2.resize(image, (width, height)), cv2.COLOR_BGR2RGB)
            output = np.asarray(next(iter(engine.infer({name: rgb}).values()))).squeeze()
            depth_check.update(
                shape=list(output.shape),
                finite=bool(np.isfinite(output).all()),
                std=round(float(output.std()), 4),
                ms_per_frame=_timed(lambda f: engine.infer({name: f}), rgb),
            )
            depth_check["ok"] = output.ndim == 2 and depth_check["finite"] and depth_check["std"] > 0
        engine.close()
    checks["mono_depth"] = depth_check

    report["ok"] = all(check["ok"] for check in checks.values())
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
