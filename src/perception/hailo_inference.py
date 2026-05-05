"""
Generic Hailo-8 HEF inference wrapper.

Loads any HEF model file, exposes a single ``infer()`` call that takes a
dict of named numpy arrays and returns a dict of named float32 arrays.

Usage::

    with HailoInference("/path/to/model.hef") as engine:
        outputs = engine.infer({"input_layer1": frame_uint8})

Gracefully degrades to sim mode if hailo_platform is not installed or the
device is unreachable — callers check ``engine.hardware_ready``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import hailo_platform as hp

    _HAILO_AVAILABLE = True
except (ImportError, OSError) as _exc:
    hp = None  # type: ignore
    _HAILO_AVAILABLE = False
    log.warning("hailo_platform not available — inference in sim mode (%s)", _exc)


class HailoInference:
    """
    Single-model Hailo-8 inference engine.

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
            self._vdevice = hp.VDevice()
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
            log.info(
                "HailoInference loaded: %s  inputs=%s  outputs=%s",
                self._hef_path.name,
                [i.name for i in self._input_info],
                [o.name for o in self._output_info],
            )
        except Exception as exc:
            log.warning("[sim] HailoInference init failed (%s) — sim mode", exc)
            self._cleanup()
            self._sim = True

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
        if self._sim or self._infer_pipeline is None:
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
            with self._network_group.activate():
                outputs = self._infer_pipeline.infer(batched)
        except Exception as exc:
            log.error("infer() failed: %s", exc)
            return {}

        # Strip batch dim from outputs
        return {k: v[0] if v.ndim > 0 and v.shape[0] == 1 else v for k, v in outputs.items()}

    # ── Lifecycle ──────────────────────────────────────────────────────

    def close(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        for attr in ("_infer_pipeline", "_vdevice"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.__exit__(None, None, None)
                except Exception:
                    pass
                setattr(self, attr, None)

    def __enter__(self) -> "HailoInference":
        return self

    def __exit__(self, *_) -> None:
        self.close()
