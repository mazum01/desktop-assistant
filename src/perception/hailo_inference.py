"""
Generic Hailo HEF inference wrapper (Hailo-8 and Hailo-10H compatible).

Loads any HEF model file, exposes a single ``infer()`` call that takes a
dict of named numpy arrays and returns a dict of named float32 arrays.

Supports both:
- Hailo-8 via ``InferVStreams``
- Hailo-10H / HailoRT 5.x via ``InferModel``

Usage::

    with HailoInference("/path/to/model.hef") as engine:
        outputs = engine.infer({"input_layer1": frame_uint8})

Gracefully degrades to sim mode if hailo_platform is not installed or the
device is unreachable — callers check ``engine.hardware_ready``.
"""

from __future__ import annotations

from collections import namedtuple
import logging
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)

StreamInfo = namedtuple("StreamInfo", ["name", "shape"])

try:
    import hailo_platform as hp

    _HAILO_AVAILABLE = True
except (ImportError, OSError) as _exc:
    hp = None  # type: ignore
    _HAILO_AVAILABLE = False
    log.warning("hailo_platform not available — inference in sim mode (%s)", _exc)

# One VDevice is shared across all HailoInference instances in this process.
# Hailo-8 allows multiple configured network groups on a single VDevice; creating
# two VDevices fails with HAILO_OUT_OF_PHYSICAL_DEVICES.
# ROUND_ROBIN scheduling lets the Hailo runtime multiplex multiple models without
# hand-rolling network group activation.
_shared_vdevice = None


def _get_shared_vdevice():
    global _shared_vdevice
    if _shared_vdevice is None:
        params = hp.VDevice.create_params()
        params.scheduling_algorithm = hp.HailoSchedulingAlgorithm.ROUND_ROBIN
        _shared_vdevice = hp.VDevice(params)
    return _shared_vdevice


class HailoInference:
    """
    Single-model Hailo inference engine (Hailo-8 and Hailo-10H compatible).

    Parameters
    ----------
    hef_path : str | Path
        Path to the compiled HEF model file.
    """

    def __init__(self, hef_path: str | Path) -> None:
        self._hef_path = Path(hef_path)
        self._vdevice = None
        self._network_group = None
        self._infer_pipeline = None

        # Hailo-10H InferModel attributes
        self._infer_model = None
        self._configured_infer_model = None
        self._bindings = None
        self._io_buffers: Dict[str, np.ndarray] = {}
        self._is_infer_model_api = False

        self._input_info: list = []
        self._output_info: list = []
        self._sim = not _HAILO_AVAILABLE

        if self._sim:
            log.warning("[sim] HailoInference — hailo_platform unavailable")
            return

        if not self._hef_path.exists():
            log.warning("[sim] HEF not found: %s — inference in sim mode", self._hef_path)
            self._sim = True
            return

        try:
            self._vdevice = _get_shared_vdevice()
            self._init_runtime()
        except Exception as exc:
            log.warning("[sim] HailoInference init failed (%s) — sim mode", exc)
            self._cleanup()
            self._sim = True

    def _init_runtime(self) -> None:
        # Determine if we should use InferModel API (Hailo-10H) or VStreams (Hailo-8).
        # On Hailo-10H / HailoRT 5.x, vdevice.configure(hef) returns HAILO_NOT_IMPLEMENTED,
        # so we probe or detect InferModel capability.
        from src.perception.hailo_probe import probe_hailo
        dev = probe_hailo()
        prefer_infer_model = dev.is_present and dev.is_hailo10

        if prefer_infer_model and hasattr(self._vdevice, "create_infer_model"):
            self._init_infer_model()
        else:
            try:
                self._init_vstreams()
            except Exception as exc:
                if hasattr(self._vdevice, "create_infer_model"):
                    log.info("VStreams init failed (%s); trying InferModel API", exc)
                    self._init_infer_model()
                else:
                    raise

    def _init_infer_model(self) -> None:
        """Initialize via InferModel API (Hailo-10H / HailoRT 5.x)."""
        self._infer_model = self._vdevice.create_infer_model(str(self._hef_path))
        self._configured_infer_model = self._infer_model.configure()
        self._bindings = self._configured_infer_model.create_bindings()

        self._input_info = [
            StreamInfo(inp.name, tuple(inp.shape))
            for inp in self._infer_model.inputs
        ]
        self._output_info = [
            StreamInfo(out.name, tuple(out.shape))
            for out in self._infer_model.outputs
        ]

        # Allocate reusable I/O buffers and bind them to the InferStreams
        for inp in self._infer_model.inputs:
            dtype = np.uint8 if inp.format.type == hp.FormatType.UINT8 else np.float32
            buf = np.empty(tuple(inp.shape), dtype=dtype)
            self._io_buffers[inp.name] = buf
            self._bindings.input(inp.name).set_buffer(buf)

        for out in self._infer_model.outputs:
            dtype = np.float32 if out.format.type == hp.FormatType.FLOAT32 else np.uint8
            buf = np.empty(tuple(out.shape), dtype=dtype)
            self._io_buffers[out.name] = buf
            self._bindings.output(out.name).set_buffer(buf)

        self._is_infer_model_api = True
        log.info(
            "HailoInference (InferModel) loaded: %s  inputs=%s  outputs=%s",
            self._hef_path.name,
            [i.name for i in self._input_info],
            [o.name for o in self._output_info],
        )

    def _init_vstreams(self) -> None:
        """Initialize via InferVStreams API (Hailo-8 / legacy)."""
        hef = hp.HEF(str(self._hef_path))
        configured = self._vdevice.configure(hef)
        self._network_group = configured[0]
        self._input_info = self._network_group.get_input_vstream_infos()
        self._output_info = self._network_group.get_output_vstream_infos()
        inp_params = hp.InputVStreamParams.make_from_network_group(
            self._network_group,
            quantized=False,
            format_type=hp.FormatType.UINT8,
        )
        out_params = hp.OutputVStreamParams.make_from_network_group(
            self._network_group,
            quantized=False,
            format_type=hp.FormatType.FLOAT32,
        )
        self._infer_pipeline = hp.InferVStreams(
            self._network_group, inp_params, out_params
        )
        self._infer_pipeline.__enter__()
        self._is_infer_model_api = False
        log.info(
            "HailoInference (VStreams) loaded: %s  inputs=%s  outputs=%s",
            self._hef_path.name,
            [i.name for i in self._input_info],
            [o.name for o in self._output_info],
        )

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    @property
    def input_info(self) -> list:
        return self._input_info

    @property
    def output_info(self) -> list:
        return self._output_info

    def input_shape(self) -> Optional[tuple]:
        """Return (H, W, C) of the first input, or None in sim mode."""
        if self._input_info:
            return tuple(self._input_info[0].shape)
        return None

    # ── Inference ──────────────────────────────────────────────────────

    def infer(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        """
        Run one inference pass.

        Parameters
        ----------
        inputs : dict
            Keys are input layer names (or shortened names without the
            model-name prefix). Values are numpy arrays matching the
            expected shape/dtype (uint8 for image inputs).

        Returns
        -------
        dict
            Keys are output layer names, values are float32 numpy arrays.
            In sim mode returns an empty dict.
        """
        if self._sim:
            return {}

        if self._is_infer_model_api:
            return self._infer_model_run(inputs)
        return self._infer_vstreams_run(inputs)

    def _infer_model_run(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        if self._configured_infer_model is None or self._bindings is None:
            return {}

        for info in self._input_info:
            short = info.name.split("/")[-1]
            if info.name in inputs:
                arr = inputs[info.name]
            elif short in inputs:
                arr = inputs[short]
            else:
                log.error(
                    "infer(): missing input '%s' (provided: %s)",
                    info.name,
                    list(inputs.keys()),
                )
                return {}

            # Strip leading batch dim if caller passed (1, H, W, C)
            expected_ndim = len(info.shape)
            if arr.ndim == expected_ndim + 1 and arr.shape[0] == 1:
                arr = arr[0]

            buf = self._io_buffers[info.name]
            if arr.shape != buf.shape:
                log.error("infer(): shape mismatch for '%s': got %s expected %s",
                          info.name, arr.shape, buf.shape)
                return {}
            np.copyto(buf, arr)

        try:
            self._configured_infer_model.run([self._bindings], 1000)
        except Exception as exc:
            log.error("InferModel run failed: %s", exc)
            return {}

        result = {}
        for info in self._output_info:
            out_buf = self._io_buffers[info.name]
            result[info.name] = np.copy(out_buf)
        return result

    def _infer_vstreams_run(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        if self._infer_pipeline is None:
            return {}

        # Allow callers to pass short names without the "model/" prefix.
        full_inputs: Dict[str, np.ndarray] = {}
        for info in self._input_info:
            short = info.name.split("/")[-1]
            if info.name in inputs:
                full_inputs[info.name] = inputs[info.name]
            elif short in inputs:
                full_inputs[info.name] = inputs[short]
            else:
                log.error(
                    "infer(): missing input '%s' (provided: %s)",
                    info.name,
                    list(inputs.keys()),
                )
                return {}

        # Ensure batch dimension
        batched = {
            k: v[np.newaxis] if v.ndim == len(self._input_info[0].shape) else v
            for k, v in full_inputs.items()
        }

        try:
            outputs = self._infer_pipeline.infer(batched)
        except Exception as exc:
            log.error("infer() failed: %s", exc)
            return {}

        # Strip batch dim from outputs; NMS outputs may be lists — strip batch dim there too.
        result = {}
        for k, v in outputs.items():
            if isinstance(v, np.ndarray):
                result[k] = v[0] if v.ndim > 0 and v.shape[0] == 1 else v
            elif isinstance(v, list) and len(v) == 1:
                result[k] = v[0]  # strip batch dim from list-type NMS outputs
            else:
                result[k] = v
        return result

    # ── Lifecycle ──────────────────────────────────────────────────────

    def close(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        # Clean up InferModel API objects in correct reverse order
        self._io_buffers.clear()
        self._bindings = None
        self._configured_infer_model = None
        self._infer_model = None

        # Clean up VStreams pipeline
        obj = getattr(self, "_infer_pipeline", None)
        if obj is not None:
            try:
                obj.__exit__(None, None, None)
            except Exception:
                pass
            self._infer_pipeline = None

        # Do NOT close the shared VDevice — other HailoInference instances may still
        # be using it.
        self._vdevice = None

    def __enter__(self) -> "HailoInference":
        return self

    def __exit__(self, *_) -> None:
        self.close()
