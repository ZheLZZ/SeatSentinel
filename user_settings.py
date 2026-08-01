"""Persistent user-adjustable settings for the tray application."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import config


class SettingsError(ValueError):
    """Raised when persisted or entered settings are invalid."""


@dataclass(frozen=True)
class AppSettings:
    """Settings that may be changed from the tray settings window."""

    camera_name: str
    detection_interval_seconds: float
    face_confidence_threshold: float
    inference_device: str
    presence_mode: str
    privacy_blur_enabled: bool
    face_absence_timeout_seconds: float
    input_idle_timeout_seconds: float
    startup_grace_period_seconds: float
    frame_width: int
    frame_height: int

    @classmethod
    def defaults(cls) -> "AppSettings":
        return cls(
            camera_name=config.PREFERRED_CAMERA_NAME,
            detection_interval_seconds=(
                config.DETECTION_INTERVAL_SECONDS
            ),
            face_confidence_threshold=(
                config.FACE_CONFIDENCE_THRESHOLD
            ),
            inference_device=config.PREFERRED_INFERENCE_DEVICE,
            presence_mode=config.PRESENCE_MODE,
            privacy_blur_enabled=config.PRIVACY_BLUR_ENABLED,
            face_absence_timeout_seconds=(
                config.FACE_ABSENCE_TIMEOUT_SECONDS
            ),
            input_idle_timeout_seconds=(
                config.INPUT_IDLE_TIMEOUT_SECONDS
            ),
            startup_grace_period_seconds=(
                config.STARTUP_GRACE_PERIOD_SECONDS
            ),
            frame_width=config.FRAME_WIDTH,
            frame_height=config.FRAME_HEIGHT,
        )

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AppSettings":
        defaults = asdict(cls.defaults())
        defaults.update(values)
        try:
            settings = cls(
                camera_name=str(defaults["camera_name"]).strip(),
                detection_interval_seconds=float(
                    defaults["detection_interval_seconds"]
                ),
                face_confidence_threshold=float(
                    defaults["face_confidence_threshold"]
                ),
                inference_device=str(
                    defaults["inference_device"]
                ).strip().upper(),
                presence_mode=str(
                    defaults["presence_mode"]
                ).strip().upper(),
                privacy_blur_enabled=cls._parse_boolean(
                    defaults["privacy_blur_enabled"],
                    "多人脸隐私模糊开关",
                ),
                face_absence_timeout_seconds=float(
                    defaults["face_absence_timeout_seconds"]
                ),
                input_idle_timeout_seconds=float(
                    defaults["input_idle_timeout_seconds"]
                ),
                startup_grace_period_seconds=float(
                    defaults["startup_grace_period_seconds"]
                ),
                frame_width=int(defaults["frame_width"]),
                frame_height=int(defaults["frame_height"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SettingsError(f"设置值格式不正确：{exc}") from exc
        settings.validate()
        return settings

    @staticmethod
    def _parse_boolean(value: Any, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        raise SettingsError(f"{field_name}必须为开启或关闭")

    def validate(self) -> None:
        if not self.camera_name:
            raise SettingsError("必须选择摄像头")
        if not 0.1 <= self.detection_interval_seconds <= 10.0:
            raise SettingsError("检测间隔必须在 0.1 至 10 秒之间")
        if not 0.1 <= self.face_confidence_threshold <= 1.0:
            raise SettingsError("人脸置信度必须在 0.1 至 1.0 之间")
        if self.inference_device not in {"NPU", "CPU"}:
            raise SettingsError("推理设备只能选择 NPU 或 CPU")
        if self.presence_mode not in {"ANY_FACE", "REGISTERED_FACE"}:
            raise SettingsError("在场判断只能选择任意人脸或仅本人")
        if not isinstance(self.privacy_blur_enabled, bool):
            raise SettingsError("多人脸隐私模糊开关必须为布尔值")
        if not 3.0 <= self.face_absence_timeout_seconds <= 3600.0:
            raise SettingsError("无人超时必须在 3 至 3600 秒之间")
        if not 3.0 <= self.input_idle_timeout_seconds <= 3600.0:
            raise SettingsError("键鼠空闲超时必须在 3 至 3600 秒之间")
        if not 0.0 <= self.startup_grace_period_seconds <= 3600.0:
            raise SettingsError("启动宽限期必须在 0 至 3600 秒之间")
        if not 160 <= self.frame_width <= 3840:
            raise SettingsError("画面宽度必须在 160 至 3840 之间")
        if not 120 <= self.frame_height <= 2160:
            raise SettingsError("画面高度必须在 120 至 2160 之间")

    def apply_to_runtime(self) -> None:
        """Apply settings before starting a fresh monitoring worker."""
        config.PREFERRED_CAMERA_NAME = self.camera_name
        config.DETECTION_INTERVAL_SECONDS = (
            self.detection_interval_seconds
        )
        config.FACE_CONFIDENCE_THRESHOLD = (
            self.face_confidence_threshold
        )
        config.PREFERRED_INFERENCE_DEVICE = self.inference_device
        config.PRESENCE_MODE = self.presence_mode
        config.PRIVACY_BLUR_ENABLED = self.privacy_blur_enabled
        config.FACE_ABSENCE_TIMEOUT_SECONDS = (
            self.face_absence_timeout_seconds
        )
        config.INPUT_IDLE_TIMEOUT_SECONDS = (
            self.input_idle_timeout_seconds
        )
        config.STARTUP_GRACE_PERIOD_SECONDS = (
            self.startup_grace_period_seconds
        )
        config.FRAME_WIDTH = self.frame_width
        config.FRAME_HEIGHT = self.frame_height


class SettingsStore:
    """Load and atomically save settings, including legacy migration."""

    def __init__(
        self,
        path: Path | None = None,
        legacy_path: Path | None = None,
        legacy_paths: tuple[Path, ...] | None = None,
    ) -> None:
        if legacy_path is not None and legacy_paths is not None:
            raise SettingsError(
                "legacy_path 和 legacy_paths 不能同时使用"
            )
        self.path = path or config.USER_SETTINGS_PATH
        self.legacy_paths = (
            legacy_paths
            if legacy_paths is not None
            else (
                (legacy_path,)
                if legacy_path is not None
                else (
                    config.LEGACY_USER_SETTINGS_PATHS
                    if path is None
                    else ()
                )
            )
        )

    def load(self) -> AppSettings:
        if not self.path.is_file():
            migrated = self._load_legacy_settings()
            if migrated is not None:
                self.save(migrated)
                return migrated
            settings = AppSettings.defaults()
            self.save(settings)
            return settings

        try:
            values = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError(
                f"无法读取设置文件 {self.path}：{exc}"
            ) from exc
        if not isinstance(values, dict):
            raise SettingsError("设置文件的顶层内容必须是对象")
        return AppSettings.from_mapping(values)

    def _load_legacy_settings(self) -> AppSettings | None:
        """Read settings saved under a previous application name."""
        for legacy_path in self.legacy_paths:
            if not legacy_path.is_file():
                continue
            try:
                values = json.loads(
                    legacy_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise SettingsError(
                    f"无法迁移旧版设置文件 {legacy_path}：{exc}"
                ) from exc
            if not isinstance(values, dict):
                raise SettingsError("旧版设置文件的顶层内容必须是对象")
            return AppSettings.from_mapping(values)
        return None

    def save(self, settings: AppSettings) -> None:
        settings.validate()
        temporary_path = self.path.with_suffix(
            self.path.suffix + ".tmp"
        )
        serialized = json.dumps(
            asdict(settings),
            ensure_ascii=False,
            indent=2,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                serialized + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, self.path)
        except OSError as exc:
            raise SettingsError(
                f"无法保存设置文件 {self.path}：{exc}"
            ) from exc
