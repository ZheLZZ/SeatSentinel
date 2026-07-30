"""Camera access with conservative failure handling."""

from __future__ import annotations

import logging
from typing import Any, Optional

import cv2
import numpy as np
from cv2_enumerate_cameras import enumerate_cameras
from numpy.typing import NDArray


LOGGER = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Raised when the camera cannot be initialized."""


class Camera:
    """Own an OpenCV camera and expose safe frame reads."""

    def __init__(
        self,
        index: int,
        width: int,
        height: int,
        preferred_name: str = "",
    ) -> None:
        self._index = index
        self._width = width
        self._height = height
        self._preferred_name = preferred_name.strip()
        self._capture: Optional[cv2.VideoCapture] = None

    def open(self) -> None:
        """Open the named camera, or the configured index when no name is set."""
        self.release()

        selected_index = self._index
        selected_backend = cv2.CAP_DSHOW
        selected_name = f"camera index {self._index}"

        if self._preferred_name:
            camera_info = self._find_preferred_camera()
            selected_index = int(camera_info.index)
            selected_backend = int(camera_info.backend)
            selected_name = str(camera_info.name)

        capture = cv2.VideoCapture(selected_index, selected_backend)
        if not capture.isOpened():
            capture.release()
            if self._preferred_name:
                raise CameraError(
                    f"Unable to open selected camera: {selected_name}"
                )

            LOGGER.warning(
                "DirectShow could not open camera %d; "
                "trying the default backend",
                selected_index,
            )
            capture = cv2.VideoCapture(selected_index)

        if not capture.isOpened():
            capture.release()
            raise CameraError(
                f"Unable to open camera index {selected_index}"
            )

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
        self._capture = capture
        LOGGER.info(
            "Using camera: %s (index=%d, requested resolution: %dx%d)",
            selected_name,
            selected_index,
            self._width,
            self._height,
        )

    def _find_preferred_camera(self) -> Any:
        """Find the configured camera by its Windows DirectShow name."""
        try:
            cameras = list(enumerate_cameras(cv2.CAP_DSHOW))
        except Exception as exc:
            raise CameraError(
                f"Unable to enumerate Windows cameras: {exc}"
            ) from exc

        if not cameras:
            raise CameraError("Windows reported no DirectShow cameras")

        available_names = [str(camera.name) for camera in cameras]
        LOGGER.info(
            "Available cameras: %s",
            ", ".join(available_names),
        )

        preferred_casefold = self._preferred_name.casefold()
        exact_matches = [
            camera
            for camera in cameras
            if str(camera.name).casefold() == preferred_casefold
        ]
        if exact_matches:
            return exact_matches[0]

        partial_matches = [
            camera
            for camera in cameras
            if preferred_casefold in str(camera.name).casefold()
        ]
        if len(partial_matches) == 1:
            return partial_matches[0]

        raise CameraError(
            f"Preferred camera '{self._preferred_name}' was not found. "
            f"Available cameras: {', '.join(available_names)}"
        )

    def read(self) -> tuple[bool, Optional[NDArray[np.uint8]]]:
        """Read one frame, returning a failure state instead of stale pixels."""
        if self._capture is None or not self._capture.isOpened():
            return False, None

        try:
            ok, frame = self._capture.read()
        except cv2.error as exc:
            LOGGER.debug("OpenCV camera read exception", exc_info=exc)
            return False, None

        if not ok or frame is None or frame.size == 0:
            return False, None
        return True, frame

    def release(self) -> None:
        """Release the camera if it is currently owned."""
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
