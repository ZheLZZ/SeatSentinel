"""Application configuration for AwayLock."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APPLICATION_TITLE = "AwayLock"
APPLICATION_VERSION = "0.1.0-beta"
CAMERA_INDEX = 0
# Windows 当前内置摄像头的设备名称。非空时按名称选择，不再盲用编号 0。
PREFERRED_CAMERA_NAME = "FHD Camera"
DETECTION_INTERVAL_SECONDS = 0.5
FACE_CONFIDENCE_THRESHOLD = 0.6
PREFERRED_INFERENCE_DEVICE = "NPU"
FACE_ABSENCE_TIMEOUT_SECONDS = 60
INPUT_IDLE_TIMEOUT_SECONDS = 60
STARTUP_GRACE_PERIOD_SECONDS = 30
FRAME_WIDTH = 640
FRAME_HEIGHT = 360

# Status and repeated-error messages are deliberately rate-limited.
STATUS_LOG_INTERVAL_SECONDS = 5.0
ERROR_LOG_INTERVAL_SECONDS = 5.0
CAMERA_RECONNECT_INTERVAL_SECONDS = 5.0
SESSION_STATE_POLL_INTERVAL_SECONDS = 0.5
SESSION_STATE_LOG_INTERVAL_SECONDS = 10.0
LOCK_TRANSITION_TIMEOUT_SECONDS = 15.0
POST_UNLOCK_RESUME_DELAY_SECONDS = 2.0
LOCK_RETRY_COOLDOWN_SECONDS = 30.0
UNLOCK_CONFIRMATION_POLLS = 2


def _application_directory() -> Path:
    """Return the source directory, or the executable directory when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APPLICATION_DIRECTORY = _application_directory()
BUNDLE_RESOURCE_DIRECTORY = Path(
    getattr(sys, "_MEIPASS", APPLICATION_DIRECTORY)
).resolve()
USER_DATA_DIRECTORY = (
    Path(
        os.environ.get(
            "LOCALAPPDATA",
            str(APPLICATION_DIRECTORY),
        )
    )
    / "AwayLock"
)
LEGACY_USER_DATA_DIRECTORY = USER_DATA_DIRECTORY.parent / "PresenceLock"
USER_SETTINGS_PATH = (
    USER_DATA_DIRECTORY / "settings.json"
    if getattr(sys, "frozen", False)
    else APPLICATION_DIRECTORY / "settings.json"
)
LEGACY_USER_SETTINGS_PATH = (
    LEGACY_USER_DATA_DIRECTORY / "settings.json"
    if getattr(sys, "frozen", False)
    else None
)
MODEL_XML_PATH = (
    BUNDLE_RESOURCE_DIRECTORY
    / "models"
    / "face-detection-retail-0004.xml"
)
MODEL_BIN_PATH = (
    BUNDLE_RESOURCE_DIRECTORY
    / "models"
    / "face-detection-retail-0004.bin"
)
