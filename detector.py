"""OpenVINO face detector for face-detection-retail-0004."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from numpy.typing import NDArray

# AwayLock never initializes OpenVINO conversion-tool telemetry. These
# process-local flags also disable common optional dependency analytics.
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("SCARF_NO_ANALYTICS", "1")

from openvino import Core


LOGGER = logging.getLogger(__name__)


class DetectorError(RuntimeError):
    """Raised when the detector cannot be initialized."""


class DetectorInferenceError(RuntimeError):
    """Raised when a frame cannot be inferred reliably."""


@dataclass(frozen=True)
class FaceDetection:
    """One face detection in original-frame pixel coordinates."""

    confidence: float
    xmin: int
    ymin: int
    xmax: int
    ymax: int


class FaceDetector:
    """Load and run the Open Model Zoo retail face detector."""

    def __init__(
        self,
        model_xml_path: Path,
        model_bin_path: Path,
        confidence_threshold: float,
        preferred_device: str = "NPU",
    ) -> None:
        self._confidence_threshold = confidence_threshold
        self._preferred_device = preferred_device.strip().upper()
        if self._preferred_device not in {"NPU", "CPU"}:
            raise DetectorError(
                "Preferred inference device must be NPU or CPU"
            )
        self._core: Optional[Core] = None
        self._model: Any = None
        self._compiled_model: Any = None
        self._infer_request: Any = None
        self._input_port: Any = None
        self._output_port: Any = None
        self._input_height = 0
        self._input_width = 0
        self.device = ""

        self._initialize(model_xml_path, model_bin_path)

    def _initialize(self, model_xml_path: Path, model_bin_path: Path) -> None:
        if not model_xml_path.is_file():
            raise DetectorError(f"Model XML not found: {model_xml_path}")
        if not model_bin_path.is_file():
            raise DetectorError(f"Model BIN not found: {model_bin_path}")

        try:
            self._core = Core()
            self._model = self._core.read_model(
                model=str(model_xml_path),
                weights=str(model_bin_path),
            )
        except Exception as exc:
            raise DetectorError(f"Unable to read OpenVINO model: {exc}") from exc

        inputs = self._model.inputs
        outputs = self._model.outputs
        if len(inputs) != 1:
            raise DetectorError(
                f"Expected one model input, but found {len(inputs)}"
            )
        if len(outputs) != 1:
            raise DetectorError(
                f"Expected one model output, but found {len(outputs)}"
            )

        self._input_port = inputs[0]
        self._validate_input_shape()
        self._validate_output_shape(outputs[0])
        self._compile_with_fallback()

        self._output_port = self._compiled_model.output(0)
        self._infer_request = self._compiled_model.create_infer_request()

    def _validate_input_shape(self) -> None:
        partial_shape = self._input_port.partial_shape
        if partial_shape.is_dynamic:
            raise DetectorError(
                f"Dynamic model input is not supported: {partial_shape}"
            )

        shape = tuple(int(dimension) for dimension in self._input_port.shape)
        if len(shape) != 4 or shape[0] != 1 or shape[1] != 3:
            raise DetectorError(
                "Expected face detector input in NCHW form [1, 3, H, W], "
                f"but received {shape}"
            )

        self._input_height = shape[2]
        self._input_width = shape[3]
        if self._input_height <= 0 or self._input_width <= 0:
            raise DetectorError(f"Invalid model input shape: {shape}")

    @staticmethod
    def _validate_output_shape(output_port: Any) -> None:
        partial_shape = output_port.partial_shape
        if partial_shape.is_dynamic:
            return

        shape = tuple(int(dimension) for dimension in output_port.shape)
        if (
            len(shape) != 4
            or shape[0] != 1
            or shape[1] != 1
            or shape[3] != 7
        ):
            raise DetectorError(
                "Expected detector output [1, 1, N, 7], "
                f"but received {shape}"
            )

    def _compile_with_fallback(self) -> None:
        if self._core is None:
            raise DetectorError("OpenVINO Core has not been initialized")

        if self._preferred_device == "CPU":
            try:
                self._compiled_model = self._core.compile_model(
                    self._model,
                    "CPU",
                )
                self.device = "CPU"
                LOGGER.info("Using OpenVINO CPU (selected by user)")
                return
            except Exception as exc:
                raise DetectorError(
                    f"Unable to compile the model for CPU: {exc}"
                ) from exc

        try:
            available_devices = list(self._core.available_devices)
        except Exception as exc:
            raise DetectorError(
                f"Unable to query OpenVINO devices: {exc}"
            ) from exc

        npu_device = next(
            (
                device
                for device in available_devices
                if device.upper().split(".", maxsplit=1)[0] == "NPU"
            ),
            None,
        )

        if npu_device is not None:
            try:
                self._compiled_model = self._core.compile_model(
                    self._model,
                    npu_device,
                )
                self.device = "NPU"
                LOGGER.info("Using OpenVINO NPU")
                return
            except Exception as exc:
                LOGGER.warning(
                    "NPU unavailable, falling back to CPU: %s",
                    exc,
                )
        else:
            LOGGER.info("NPU unavailable, falling back to CPU")

        try:
            self._compiled_model = self._core.compile_model(
                self._model,
                "CPU",
            )
            self.device = "CPU"
            LOGGER.info("Using OpenVINO CPU")
        except Exception as exc:
            raise DetectorError(
                f"Unable to compile the model for CPU: {exc}"
            ) from exc

    def _prepare_input(
        self,
        frame: NDArray[np.uint8],
    ) -> NDArray[np.float32]:
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise DetectorInferenceError(
                f"Expected a three-channel BGR frame, got shape {frame.shape}"
            )

        try:
            resized = cv2.resize(
                frame,
                (self._input_width, self._input_height),
                interpolation=cv2.INTER_LINEAR,
            )
        except cv2.error as exc:
            raise DetectorInferenceError(
                f"Unable to resize camera frame: {exc}"
            ) from exc

        # OpenCV already supplies BGR, which is what this model expects.
        # Convert HWC -> CHW and prepend the batch dimension to form NCHW.
        chw = np.transpose(resized, (2, 0, 1))
        nchw = np.expand_dims(chw, axis=0)
        return np.ascontiguousarray(nchw, dtype=np.float32)

    def detect_faces(
        self,
        frame: NDArray[np.uint8],
    ) -> list[FaceDetection]:
        """Return all above-threshold faces in original-frame coordinates."""
        if self._infer_request is None or self._output_port is None:
            raise DetectorInferenceError("Detector is not initialized")

        input_tensor = self._prepare_input(frame)
        try:
            results = self._infer_request.infer(
                inputs={0: input_tensor}
            )
            raw_output = np.asarray(results[0])
        except Exception as exc:
            raise DetectorInferenceError(
                f"OpenVINO inference failed: {exc}"
            ) from exc

        if (
            raw_output.ndim != 4
            or raw_output.shape[0] != 1
            or raw_output.shape[1] != 1
            or raw_output.shape[3] != 7
            or raw_output.shape[2] == 0
        ):
            raise DetectorInferenceError(
                "Unexpected detector output shape "
                f"{raw_output.shape}; expected [1, 1, N, 7]"
            )

        frame_height, frame_width = frame.shape[:2]
        return self.parse_detections(
            raw_output=raw_output,
            frame_width=frame_width,
            frame_height=frame_height,
            confidence_threshold=self._confidence_threshold,
        )

    @staticmethod
    def parse_detections(
        raw_output: NDArray[np.floating[Any]],
        frame_width: int,
        frame_height: int,
        confidence_threshold: float,
    ) -> list[FaceDetection]:
        """Parse [image_id, label, confidence, xmin, ymin, xmax, ymax]."""
        if frame_width <= 0 or frame_height <= 0:
            raise DetectorInferenceError(
                "Original frame dimensions must be positive"
            )
        output = np.asarray(raw_output)
        if (
            output.ndim != 4
            or output.shape[0] != 1
            or output.shape[1] != 1
            or output.shape[3] != 7
        ):
            raise DetectorInferenceError(
                "Unexpected detector output shape "
                f"{output.shape}; expected [1, 1, N, 7]"
            )

        parsed: list[FaceDetection] = []
        for row in output.reshape(-1, 7):
            # A negative image_id marks the end of valid detections.
            if float(row[0]) < 0:
                break

            confidence = float(row[2])
            coordinates = [float(value) for value in row[3:7]]
            if (
                not math.isfinite(confidence)
                or any(
                    not math.isfinite(value)
                    for value in coordinates
                )
                or confidence <= confidence_threshold
            ):
                continue

            xmin_norm, ymin_norm, xmax_norm, ymax_norm = coordinates
            xmin = min(
                frame_width - 1,
                max(0, int(xmin_norm * frame_width)),
            )
            ymin = min(
                frame_height - 1,
                max(0, int(ymin_norm * frame_height)),
            )
            xmax = min(
                frame_width - 1,
                max(0, int(xmax_norm * frame_width)),
            )
            ymax = min(
                frame_height - 1,
                max(0, int(ymax_norm * frame_height)),
            )
            if xmax <= xmin or ymax <= ymin:
                continue

            parsed.append(
                FaceDetection(
                    confidence=confidence,
                    xmin=xmin,
                    ymin=ymin,
                    xmax=xmax,
                    ymax=ymax,
                )
            )
        return parsed

    def has_face(self, frame: NDArray[np.uint8]) -> bool:
        """Return whether any detection is above the configured confidence."""
        return bool(self.detect_faces(frame))

    def close(self) -> None:
        """Drop OpenVINO objects so their native resources can be reclaimed."""
        self._infer_request = None
        self._compiled_model = None
        self._model = None
        self._core = None
