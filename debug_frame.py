"""Thread-safe in-memory handoff for the latest annotated debug frame."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from numpy.typing import NDArray

from detector import FaceDetection


@dataclass(frozen=True)
class DebugFrameSnapshot:
    """An immutable description of the latest monitoring result."""

    frame_bgr: Optional[NDArray[np.uint8]]
    detections: tuple[FaceDetection, ...]
    face_detected: Optional[bool]
    presence_detected: Optional[bool]
    presence_mode: str
    device: str
    status: str
    face_absent_seconds: Optional[float]
    input_idle_seconds: Optional[float]
    startup_elapsed_seconds: Optional[float]
    should_lock: Optional[bool]
    inference_ms: Optional[float]
    sequence: int
    captured_at: Optional[float]


class DebugFrameBuffer:
    """Keep only the newest frame, protected by a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame_bgr: Optional[NDArray[np.uint8]] = None
        self._detections: tuple[FaceDetection, ...] = ()
        self._face_detected: Optional[bool] = None
        self._presence_detected: Optional[bool] = None
        self._presence_mode = ""
        self._device = ""
        self._status = "等待监控启动"
        self._face_absent_seconds: Optional[float] = None
        self._input_idle_seconds: Optional[float] = None
        self._startup_elapsed_seconds: Optional[float] = None
        self._should_lock: Optional[bool] = None
        self._inference_ms: Optional[float] = None
        self._sequence = 0
        self._captured_at: Optional[float] = None

    def publish(
        self,
        frame_bgr: NDArray[np.uint8],
        detections: Sequence[FaceDetection],
        device: str,
        status: str,
        face_absent_seconds: Optional[float] = None,
        input_idle_seconds: Optional[float] = None,
        startup_elapsed_seconds: Optional[float] = None,
        should_lock: Optional[bool] = None,
        inference_ms: Optional[float] = None,
        presence_detected: Optional[bool] = None,
        presence_mode: str = "",
    ) -> None:
        """Copy a monitoring result into the in-memory latest-frame slot."""
        if (
            frame_bgr.ndim != 3
            or frame_bgr.shape[2] != 3
            or frame_bgr.dtype != np.uint8
        ):
            raise ValueError(
                "Debug frame must be a uint8 BGR image with three channels"
            )
        frame_copy = np.ascontiguousarray(frame_bgr.copy())
        detection_copy = tuple(detections)
        with self._lock:
            self._frame_bgr = frame_copy
            self._detections = detection_copy
            self._face_detected = bool(detection_copy)
            self._presence_detected = (
                bool(detection_copy)
                if presence_detected is None
                else bool(presence_detected)
            )
            self._presence_mode = str(presence_mode)
            self._device = str(device)
            self._status = str(status)
            self._face_absent_seconds = face_absent_seconds
            self._input_idle_seconds = input_idle_seconds
            self._startup_elapsed_seconds = startup_elapsed_seconds
            self._should_lock = should_lock
            self._inference_ms = inference_ms
            self._sequence += 1
            self._captured_at = time.monotonic()

    def clear(self, status: str, device: str = "") -> None:
        """Immediately discard all image pixels and mark state as unknown."""
        with self._lock:
            self._frame_bgr = None
            self._detections = ()
            self._face_detected = None
            self._presence_detected = None
            self._presence_mode = ""
            self._device = str(device)
            self._status = str(status)
            self._face_absent_seconds = None
            self._input_idle_seconds = None
            self._startup_elapsed_seconds = None
            self._should_lock = None
            self._inference_ms = None
            self._sequence += 1
            self._captured_at = None

    def snapshot(self) -> DebugFrameSnapshot:
        """Return a copy safe for use by the Tkinter main thread."""
        with self._lock:
            frame_copy = (
                None
                if self._frame_bgr is None
                else np.ascontiguousarray(self._frame_bgr.copy())
            )
            return DebugFrameSnapshot(
                frame_bgr=frame_copy,
                detections=self._detections,
                face_detected=self._face_detected,
                presence_detected=self._presence_detected,
                presence_mode=self._presence_mode,
                device=self._device,
                status=self._status,
                face_absent_seconds=self._face_absent_seconds,
                input_idle_seconds=self._input_idle_seconds,
                startup_elapsed_seconds=self._startup_elapsed_seconds,
                should_lock=self._should_lock,
                inference_ms=self._inference_ms,
                sequence=self._sequence,
                captured_at=self._captured_at,
            )
