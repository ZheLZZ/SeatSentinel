"""Application configuration for SeatSentinel."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APPLICATION_TITLE = "SeatSentinel"
APPLICATION_VERSION = "0.2.5-beta"
CAMERA_INDEX = 0
# Windows 当前内置摄像头的设备名称。非空时按名称选择，不再盲用编号 0。
PREFERRED_CAMERA_NAME = "FHD Camera"
DETECTION_INTERVAL_SECONDS = 0.5
FACE_CONFIDENCE_THRESHOLD = 0.6
PREFERRED_INFERENCE_DEVICE = "NPU"
PRESENCE_MODE = "ANY_FACE"
# Open Model Zoo's face recognition demo uses a cosine-distance threshold of
# 0.30. Similarity is 1 - distance, so the corresponding minimum is 0.70.
FACE_MATCH_SIMILARITY_THRESHOLD = 0.70
IDENTITY_MATCH_CONFIRMATION_FRAMES = 2
SECOND_PERSON_CONFIRMATION_FRAMES = 2
SECOND_PERSON_REARM_CLEAR_SECONDS = 60.0
PRIVACY_BLUR_ENABLED = True
FACE_REGISTRATION_SAMPLE_COUNT = 12
FACE_REGISTRATION_TIMEOUT_SECONDS = 60.0
FACE_ABSENCE_TIMEOUT_SECONDS = 60
INPUT_IDLE_TIMEOUT_SECONDS = 60
STARTUP_GRACE_PERIOD_SECONDS = 30
LOCK_WARNING_SECONDS = 5.0
# 96% is the user-validated layered alpha for DWM Desktop Acrylic.  DWM owns
# the live blur; SeatSentinel does not capture or process desktop pixels.
PRIVACY_ACRYLIC_STRENGTH_PERCENT = 96
PRIVACY_MOUSE_POLL_MILLISECONDS = 20
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
    / "SeatSentinel"
)
LEGACY_USER_DATA_DIRECTORIES = (
    USER_DATA_DIRECTORY.parent / "AwayLock",
    USER_DATA_DIRECTORY.parent / "PresenceLock",
)
USER_SETTINGS_PATH = (
    USER_DATA_DIRECTORY / "settings.json"
    if getattr(sys, "frozen", False)
    else APPLICATION_DIRECTORY / "settings.json"
)
LEGACY_USER_SETTINGS_PATHS = (
    tuple(
        directory / "settings.json"
        for directory in LEGACY_USER_DATA_DIRECTORIES
    )
    if getattr(sys, "frozen", False)
    else ()
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
LANDMARKS_MODEL_XML_PATH = (
    BUNDLE_RESOURCE_DIRECTORY
    / "models"
    / "landmarks-regression-retail-0009.xml"
)
LANDMARKS_MODEL_BIN_PATH = (
    BUNDLE_RESOURCE_DIRECTORY
    / "models"
    / "landmarks-regression-retail-0009.bin"
)
FACE_REIDENTIFICATION_MODEL_XML_PATH = (
    BUNDLE_RESOURCE_DIRECTORY
    / "models"
    / "face-reidentification-retail-0095.xml"
)
FACE_REIDENTIFICATION_MODEL_BIN_PATH = (
    BUNDLE_RESOURCE_DIRECTORY
    / "models"
    / "face-reidentification-retail-0095.bin"
)
APPLICATION_ICON_PNG_PATH = (
    BUNDLE_RESOURCE_DIRECTORY / "assets" / "seatsentinel-icon.png"
)
APPLICATION_ICON_ICO_PATH = (
    BUNDLE_RESOURCE_DIRECTORY / "assets" / "seatsentinel-icon.ico"
)
FACE_TEMPLATE_PATH = USER_DATA_DIRECTORY / "registered-face.dat"
