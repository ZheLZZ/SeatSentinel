"""Tests for detection parsing and the in-memory debug-frame handoff."""

from __future__ import annotations

import threading
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

import config
from app import TrayApplication
from dwm_privacy import (
    acrylic_alpha_from_strength,
    bounding_rect,
    enumerate_monitor_work_areas,
    relative_work_regions,
)
from debug_frame import DebugFrameBuffer
from detector import DetectorInferenceError, FaceDetection, FaceDetector
from face_identity import (
    FaceIdentityRecognizer,
    FaceTemplate,
    FaceTemplateError,
    FaceTemplateStore,
)
from global_hotkey import (
    hotkey_from_tk_key_event,
    modifier_name_from_tk_keysym,
    modifier_names_from_tk_state,
    normalize_hotkey,
    parse_hotkey,
)
from main import (
    MonitorOutcome,
    camera_should_be_active,
    evaluate_lock_warning,
    evaluate_presence,
    evaluate_presence_auto_standby,
    monitor_until_session_pause,
    open_camera_when_session_ready,
    wait_for_camera_activation,
)
from privacy_blur import (
    MouseShakeDetector,
    PrivacyBlurSignal,
    SecondPersonPrivacyGuard,
)
from sedentary_reminder import (
    SedentaryReminderSignal,
    SedentaryTracker,
    format_sedentary_duration,
)
from single_instance import DEBUG_WINDOW_EVENT_NAME, MUTEX_NAME
from user_settings import AppSettings, SettingsError, SettingsStore


class ApplicationDefaultsTests(unittest.TestCase):
    def test_title_and_lock_timeouts(self) -> None:
        settings = AppSettings.defaults()
        self.assertEqual(config.APPLICATION_TITLE, "SeatSentinel")
        self.assertEqual(config.APPLICATION_VERSION, "0.2.10-beta")
        self.assertEqual(config.USER_DATA_DIRECTORY.name, "SeatSentinel")
        self.assertIn("SeatSentinel", MUTEX_NAME)
        self.assertIn("SeatSentinel", DEBUG_WINDOW_EVENT_NAME)
        self.assertEqual(settings.inference_device, "NPU")
        self.assertEqual(settings.presence_mode, "ANY_FACE")
        self.assertEqual(settings.camera_monitoring_mode, "CONTINUOUS")
        self.assertEqual(settings.camera_activation_idle_seconds, 20.0)
        self.assertEqual(
            settings.camera_presence_auto_standby_seconds,
            10.0,
        )
        self.assertEqual(
            settings.camera_presence_recheck_interval_seconds,
            60.0,
        )
        self.assertTrue(settings.privacy_blur_enabled)
        self.assertEqual(settings.privacy_blur_hotkey, "Alt+B")
        self.assertTrue(settings.sedentary_reminder_enabled)
        self.assertEqual(settings.face_absence_timeout_seconds, 60)
        self.assertEqual(settings.input_idle_timeout_seconds, 60)
        self.assertEqual(config.LOCK_WARNING_SECONDS, 5.0)
        self.assertEqual(
            config.SEDENTARY_REMINDER_INTERVAL_SECONDS,
            1800.0,
        )
        self.assertEqual(
            config.SEDENTARY_LEAVE_CONFIRMATION_SECONDS,
            30.0,
        )
        self.assertEqual(config.SEDENTARY_REMINDER_DISPLAY_SECONDS, 1.0)
        self.assertEqual(config.SECOND_PERSON_AUTO_DISMISS_SECONDS, 3.0)
        self.assertEqual(config.PRIVACY_ACRYLIC_STRENGTH_PERCENT, 96)
        self.assertEqual(os.environ.get("DO_NOT_TRACK"), "1")
        self.assertEqual(os.environ.get("SCARF_NO_ANALYTICS"), "1")

    def test_inference_device_setting_is_normalized(self) -> None:
        settings = AppSettings.from_mapping(
            {"inference_device": "cpu"}
        )
        self.assertEqual(settings.inference_device, "CPU")

    def test_privacy_blur_hotkey_is_normalized(self) -> None:
        settings = AppSettings.from_mapping(
            {"privacy_blur_hotkey": " shift + ctrl + f8 "}
        )
        self.assertEqual(settings.privacy_blur_hotkey, "Ctrl+Shift+F8")
        self.assertEqual(normalize_hotkey("windows+page down"), "Win+PageDown")

        parsed = parse_hotkey("Alt+B")
        self.assertEqual(parsed.display, "Alt+B")
        self.assertEqual(parsed.virtual_key, ord("B"))

    def test_privacy_blur_hotkey_requires_a_modifier_and_one_key(self) -> None:
        for invalid in ("B", "Alt", "Alt+B+C", "Ctrl+Alt+未知"):
            with self.subTest(hotkey=invalid):
                with self.assertRaises(SettingsError):
                    AppSettings.from_mapping(
                        {"privacy_blur_hotkey": invalid}
                    )

    def test_hotkey_capture_supports_letters_digits_and_function_keys(
        self,
    ) -> None:
        self.assertEqual(
            hotkey_from_tk_key_event("1", 0x31, 0x0008),
            "Alt+1",
        )
        self.assertEqual(
            hotkey_from_tk_key_event("k", 0x4B, 0x0004),
            "Ctrl+K",
        )
        self.assertEqual(
            hotkey_from_tk_key_event(
                "F8",
                0x77,
                0,
                ("Ctrl", "Shift"),
            ),
            "Ctrl+Shift+F8",
        )
        self.assertEqual(
            hotkey_from_tk_key_event("exclam", 0x31, 0x0001),
            "Shift+1",
        )

    def test_hotkey_capture_requires_a_modifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少需要"):
            hotkey_from_tk_key_event("B", 0x42, 0)

    def test_hotkey_capture_recognizes_modifier_key_events(self) -> None:
        self.assertEqual(modifier_name_from_tk_keysym("Control_L"), "Ctrl")
        self.assertEqual(modifier_name_from_tk_keysym("Alt_R"), "Alt")
        self.assertEqual(modifier_name_from_tk_keysym("Super_L"), "Win")
        self.assertEqual(
            modifier_names_from_tk_state(0x0001 | 0x0004),
            ("Ctrl", "Shift"),
        )

    def test_registered_face_mode_setting_is_normalized(self) -> None:
        settings = AppSettings.from_mapping(
            {"presence_mode": "registered_face"}
        )
        self.assertEqual(settings.presence_mode, "REGISTERED_FACE")

    def test_privacy_blur_setting_is_normalized_and_persisted(self) -> None:
        disabled = AppSettings.from_mapping(
            {"privacy_blur_enabled": "false"}
        )
        self.assertFalse(disabled.privacy_blur_enabled)

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            store = SettingsStore(path=path)
            store.save(disabled)
            loaded = store.load()
            self.assertFalse(loaded.privacy_blur_enabled)
            self.assertFalse(
                json.loads(path.read_text(encoding="utf-8"))[
                    "privacy_blur_enabled"
                ]
            )

    def test_sedentary_reminder_switch_is_persisted(self) -> None:
        disabled = AppSettings.from_mapping(
            {"sedentary_reminder_enabled": "false"}
        )
        self.assertFalse(disabled.sedentary_reminder_enabled)

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            store = SettingsStore(path=path)
            store.save(disabled)
            loaded = store.load()
            self.assertFalse(loaded.sedentary_reminder_enabled)
            self.assertFalse(
                json.loads(path.read_text(encoding="utf-8"))[
                    "sedentary_reminder_enabled"
                ]
            )

    def test_idle_triggered_camera_mode_is_normalized(self) -> None:
        settings = AppSettings.from_mapping(
            {
                "camera_monitoring_mode": "idle_triggered",
                "camera_activation_idle_seconds": 20,
                "camera_presence_auto_standby_seconds": 10,
                "camera_presence_recheck_interval_seconds": 60,
                "privacy_blur_enabled": False,
            }
        )
        self.assertEqual(
            settings.camera_monitoring_mode,
            "IDLE_TRIGGERED",
        )
        self.assertEqual(settings.camera_activation_idle_seconds, 20.0)
        self.assertEqual(
            settings.camera_presence_auto_standby_seconds,
            10.0,
        )
        self.assertEqual(
            settings.camera_presence_recheck_interval_seconds,
            60.0,
        )

    def test_idle_triggered_mode_disables_privacy_blur(self) -> None:
        settings = AppSettings.from_mapping(
            {
                "camera_monitoring_mode": "IDLE_TRIGGERED",
                "privacy_blur_enabled": True,
            }
        )
        self.assertFalse(settings.privacy_blur_enabled)

        invalid = replace(
            AppSettings.defaults(),
            camera_monitoring_mode="IDLE_TRIGGERED",
            privacy_blur_enabled=True,
        )
        with self.assertRaises(SettingsError):
            invalid.validate()

    def test_legacy_settings_are_migrated(self) -> None:
        for legacy_name in ("AwayLock", "PresenceLock"):
            with self.subTest(legacy_name=legacy_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    new_path = root / "SeatSentinel" / "settings.json"
                    legacy_paths = (
                        root / "AwayLock" / "settings.json",
                        root / "PresenceLock" / "settings.json",
                    )
                    legacy_path = (
                        root / legacy_name / "settings.json"
                    )
                    legacy_path.parent.mkdir(parents=True)
                    legacy_path.write_text(
                        json.dumps(
                            {
                                "camera_name": "FHD Camera",
                                "inference_device": "CPU",
                                "face_absence_timeout_seconds": 75,
                            }
                        ),
                        encoding="utf-8",
                    )

                    settings = SettingsStore(
                        path=new_path,
                        legacy_paths=legacy_paths,
                    ).load()

                    self.assertEqual(settings.inference_device, "CPU")
                    self.assertEqual(
                        settings.face_absence_timeout_seconds,
                        75,
                    )
                    self.assertEqual(
                        settings.camera_monitoring_mode,
                        "CONTINUOUS",
                    )
                    self.assertTrue(new_path.is_file())
                    self.assertTrue(settings.sedentary_reminder_enabled)


class DebugCameraSelectorTests(unittest.TestCase):
    def test_selection_is_saved_and_monitoring_is_restarted(self) -> None:
        current_settings = AppSettings.defaults()

        class FakeVariable:
            def get(self) -> str:
                return "USB Camera"

        class FakeSettingsStore:
            def __init__(self) -> None:
                self.saved: AppSettings | None = None

            def load(self) -> AppSettings:
                return current_settings

            def save(self, settings: AppSettings) -> None:
                self.saved = settings

        class FakeService:
            def __init__(self) -> None:
                self.restart_count = 0

            def restart_async(self) -> None:
                self.restart_count += 1

        application = TrayApplication.__new__(TrayApplication)
        application._debug_camera_variable = FakeVariable()
        application._debug_status_variable = None
        application._debug_window = None
        settings_store = FakeSettingsStore()
        service = FakeService()
        application._settings_store = settings_store
        application._service = service

        application._on_debug_camera_selected()

        self.assertIsNotNone(settings_store.saved)
        assert settings_store.saved is not None
        self.assertEqual(settings_store.saved.camera_name, "USB Camera")
        self.assertEqual(service.restart_count, 1)


class TrayPrivacyToggleTests(unittest.TestCase):
    def test_toggle_persists_hides_active_layer_and_restarts_monitoring(self) -> None:
        current = replace(
            AppSettings.defaults(),
            privacy_blur_enabled=True,
        )

        class FakeSettingsStore:
            saved: AppSettings | None = None

            def load(self) -> AppSettings:
                return current

            def save(self, settings: AppSettings) -> None:
                self.saved = settings

        class FakeService:
            clear_count = 0
            restart_count = 0

            def is_running(self) -> bool:
                return True

            def clear_privacy_blur(self) -> None:
                self.clear_count += 1

            def restart_async(self) -> None:
                self.restart_count += 1

        class FakeOverlay:
            hide_count = 0

            def hide(self) -> None:
                self.hide_count += 1

        class FakeShake:
            reset_count = 0

            def reset(self) -> None:
                self.reset_count += 1

        class FakeVariable:
            value: bool | None = None

            def set(self, value: bool) -> None:
                self.value = value

        class FakeTrayIcon:
            update_count = 0

            def update_menu(self) -> None:
                self.update_count += 1

        application = TrayApplication.__new__(TrayApplication)
        settings_store = FakeSettingsStore()
        service = FakeService()
        overlay = FakeOverlay()
        shake = FakeShake()
        variable = FakeVariable()
        tray_icon = FakeTrayIcon()
        application._settings_store = settings_store
        application._service = service
        application._privacy_blur_overlay = overlay
        application._privacy_mouse_shake = shake
        application._settings_privacy_blur_variable = variable
        application._tray_icon = tray_icon

        previous_runtime_value = config.PRIVACY_BLUR_ENABLED
        try:
            application._toggle_privacy_blur_setting()
        finally:
            config.PRIVACY_BLUR_ENABLED = previous_runtime_value

        self.assertIsNotNone(settings_store.saved)
        assert settings_store.saved is not None
        self.assertFalse(settings_store.saved.privacy_blur_enabled)
        self.assertEqual(service.clear_count, 1)
        self.assertEqual(service.restart_count, 1)
        self.assertEqual(overlay.hide_count, 1)
        self.assertEqual(shake.reset_count, 1)
        self.assertFalse(variable.value)
        self.assertEqual(tray_icon.update_count, 1)


class TrayManualPrivacyToggleTests(unittest.TestCase):
    def test_manual_toggle_works_without_camera_monitoring(self) -> None:
        class FakeService:
            dismiss_count = 0

            def privacy_blur_snapshot(self):
                return PrivacyBlurSignal().snapshot()

            def dismiss_privacy_blur(self, status_detail: str = "") -> bool:
                self.dismiss_count += 1
                return False

        class FakeOverlay:
            hide_count = 0

            def hide(self) -> None:
                self.hide_count += 1

        class FakeShake:
            def reset(self) -> None:
                return None

        class FakeTrayIcon:
            update_count = 0

            def update_menu(self) -> None:
                self.update_count += 1

        application = TrayApplication.__new__(TrayApplication)
        service = FakeService()
        overlay = FakeOverlay()
        tray_icon = FakeTrayIcon()
        refresh_count = 0

        def refresh_blur(schedule_next: bool = True) -> None:
            nonlocal refresh_count
            refresh_count += 1

        application._service = service
        application._privacy_blur_overlay = overlay
        application._privacy_mouse_shake = FakeShake()
        application._tray_icon = tray_icon
        application._manual_privacy_blur_active = False
        application._refresh_privacy_blur = refresh_blur

        application._toggle_manual_privacy_blur()
        self.assertTrue(application._manual_privacy_blur_active)
        self.assertEqual(refresh_count, 1)

        application._toggle_manual_privacy_blur()
        self.assertFalse(application._manual_privacy_blur_active)
        self.assertEqual(service.dismiss_count, 1)
        self.assertEqual(overlay.hide_count, 1)
        self.assertEqual(tray_icon.update_count, 2)


class DetectionParsingTests(unittest.TestCase):
    def test_parses_thresholded_and_clipped_pixel_boxes(self) -> None:
        raw_output = np.array(
            [
                [
                    [
                        [0, 1, 0.91, -0.10, 0.20, 1.20, 0.80],
                        [0, 1, 0.59, 0.10, 0.10, 0.50, 0.50],
                        [0, 1, 0.99, 0.80, 0.70, 0.20, 0.10],
                        [-1, 0, 0.00, 0.00, 0.00, 0.00, 0.00],
                    ]
                ]
            ],
            dtype=np.float32,
        )

        detections = FaceDetector.parse_detections(
            raw_output=raw_output,
            frame_width=200,
            frame_height=100,
            confidence_threshold=0.60,
        )

        self.assertEqual(len(detections), 1)
        detection = detections[0]
        self.assertAlmostEqual(detection.confidence, 0.91, places=5)
        self.assertEqual(
            (
                detection.xmin,
                detection.ymin,
                detection.xmax,
                detection.ymax,
            ),
            (0, 20, 199, 80),
        )

    def test_rejects_unexpected_output_shape(self) -> None:
        with self.assertRaises(DetectorInferenceError):
            FaceDetector.parse_detections(
                raw_output=np.zeros((1, 7), dtype=np.float32),
                frame_width=640,
                frame_height=360,
                confidence_threshold=0.60,
            )


class CameraActivationDecisionTests(unittest.TestCase):
    def test_continuous_mode_does_not_depend_on_input_idle_time(self) -> None:
        self.assertTrue(
            camera_should_be_active("CONTINUOUS", None, 20.0)
        )

    def test_idle_triggered_mode_activates_at_twenty_seconds(self) -> None:
        self.assertFalse(
            camera_should_be_active("IDLE_TRIGGERED", None, 20.0)
        )
        self.assertFalse(
            camera_should_be_active("IDLE_TRIGGERED", 19.9, 20.0)
        )
        self.assertTrue(
            camera_should_be_active("IDLE_TRIGGERED", 20.0, 20.0)
        )

    def test_standby_waits_until_idle_threshold(self) -> None:
        class FakeActivityMonitor:
            def __init__(self) -> None:
                self.values = iter((0.0, 19.9, 20.0))

            def seconds_since_last_input(self) -> float:
                return next(self.values)

        class FakeSessionMonitor:
            def is_locked(self) -> bool:
                return False

        previous_mode = config.CAMERA_MONITORING_MODE
        previous_activation_seconds = (
            config.CAMERA_ACTIVATION_IDLE_SECONDS
        )
        previous_poll_seconds = config.SESSION_STATE_POLL_INTERVAL_SECONDS
        debug_buffer = DebugFrameBuffer()
        try:
            config.CAMERA_MONITORING_MODE = "IDLE_TRIGGERED"
            config.CAMERA_ACTIVATION_IDLE_SECONDS = 20.0
            config.SESSION_STATE_POLL_INTERVAL_SECONDS = 0.0
            ready = wait_for_camera_activation(
                FakeActivityMonitor(),  # type: ignore[arg-type]
                FakeSessionMonitor(),  # type: ignore[arg-type]
                debug_frame_buffer=debug_buffer,
            )
        finally:
            config.CAMERA_MONITORING_MODE = previous_mode
            config.CAMERA_ACTIVATION_IDLE_SECONDS = (
                previous_activation_seconds
            )
            config.SESSION_STATE_POLL_INTERVAL_SECONDS = (
                previous_poll_seconds
            )

        self.assertTrue(ready)
        self.assertEqual(
            debug_buffer.snapshot().input_idle_seconds,
            19.9,
        )

    def test_input_recheck_prevents_camera_open(self) -> None:
        class FakeCamera:
            open_count = 0

            def open(self) -> None:
                self.open_count += 1

        class FakeActivityMonitor:
            def seconds_since_last_input(self) -> float:
                return 0.0

        class FakeSessionMonitor:
            def is_locked(self) -> bool:
                return False

        camera = FakeCamera()
        previous_mode = config.CAMERA_MONITORING_MODE
        previous_activation_seconds = (
            config.CAMERA_ACTIVATION_IDLE_SECONDS
        )
        try:
            config.CAMERA_MONITORING_MODE = "IDLE_TRIGGERED"
            config.CAMERA_ACTIVATION_IDLE_SECONDS = 20.0
            opened = open_camera_when_session_ready(
                camera,  # type: ignore[arg-type]
                FakeSessionMonitor(),  # type: ignore[arg-type]
                activity_monitor=FakeActivityMonitor(),  # type: ignore[arg-type]
            )
        finally:
            config.CAMERA_MONITORING_MODE = previous_mode
            config.CAMERA_ACTIVATION_IDLE_SECONDS = (
                previous_activation_seconds
            )

        self.assertFalse(opened)
        self.assertEqual(camera.open_count, 0)

    def test_input_activity_releases_camera_before_reading_another_frame(
        self,
    ) -> None:
        class FakeCamera:
            read_count = 0

            def read(self) -> tuple[bool, None]:
                self.read_count += 1
                return False, None

        class FakeDetector:
            device = "CPU"

        class FakeActivityMonitor:
            def seconds_since_last_input(self) -> float:
                return 0.0

        class FakeSessionMonitor:
            def is_locked(self) -> bool:
                return False

        camera = FakeCamera()
        previous_mode = config.CAMERA_MONITORING_MODE
        previous_activation_seconds = (
            config.CAMERA_ACTIVATION_IDLE_SECONDS
        )
        previous_presence_mode = config.PRESENCE_MODE
        previous_privacy_blur = config.PRIVACY_BLUR_ENABLED
        try:
            config.CAMERA_MONITORING_MODE = "IDLE_TRIGGERED"
            config.CAMERA_ACTIVATION_IDLE_SECONDS = 20.0
            config.PRESENCE_MODE = "ANY_FACE"
            config.PRIVACY_BLUR_ENABLED = False
            outcome = monitor_until_session_pause(
                camera,  # type: ignore[arg-type]
                FakeDetector(),  # type: ignore[arg-type]
                FakeActivityMonitor(),  # type: ignore[arg-type]
                FakeSessionMonitor(),  # type: ignore[arg-type]
            )
        finally:
            config.CAMERA_MONITORING_MODE = previous_mode
            config.CAMERA_ACTIVATION_IDLE_SECONDS = (
                previous_activation_seconds
            )
            config.PRESENCE_MODE = previous_presence_mode
            config.PRIVACY_BLUR_ENABLED = previous_privacy_blur

        self.assertIs(outcome, MonitorOutcome.INPUT_ACTIVE)
        self.assertEqual(camera.read_count, 0)

    def test_presence_standby_waits_until_recheck_deadline(self) -> None:
        class FakeActivityMonitor:
            def seconds_since_last_input(self) -> float:
                return 60.0

        class FakeSessionMonitor:
            def is_locked(self) -> bool:
                return False

        previous_mode = config.CAMERA_MONITORING_MODE
        previous_activation_seconds = (
            config.CAMERA_ACTIVATION_IDLE_SECONDS
        )
        previous_poll_seconds = config.SESSION_STATE_POLL_INTERVAL_SECONDS
        try:
            config.CAMERA_MONITORING_MODE = "IDLE_TRIGGERED"
            config.CAMERA_ACTIVATION_IDLE_SECONDS = 20.0
            config.SESSION_STATE_POLL_INTERVAL_SECONDS = 0.0
            with patch(
                "main.time.monotonic",
                side_effect=(100.0, 105.0),
            ):
                ready = wait_for_camera_activation(
                    FakeActivityMonitor(),  # type: ignore[arg-type]
                    FakeSessionMonitor(),  # type: ignore[arg-type]
                    presence_recheck_not_before=105.0,
                )
        finally:
            config.CAMERA_MONITORING_MODE = previous_mode
            config.CAMERA_ACTIVATION_IDLE_SECONDS = (
                previous_activation_seconds
            )
            config.SESSION_STATE_POLL_INTERVAL_SECONDS = (
                previous_poll_seconds
            )

        self.assertTrue(ready)

    def test_real_input_cancels_presence_recheck_delay(self) -> None:
        class FakeActivityMonitor:
            def __init__(self) -> None:
                self.values = iter((0.0, 20.0))

            def seconds_since_last_input(self) -> float:
                return next(self.values)

        class FakeSessionMonitor:
            def is_locked(self) -> bool:
                return False

        previous_mode = config.CAMERA_MONITORING_MODE
        previous_activation_seconds = (
            config.CAMERA_ACTIVATION_IDLE_SECONDS
        )
        previous_poll_seconds = config.SESSION_STATE_POLL_INTERVAL_SECONDS
        try:
            config.CAMERA_MONITORING_MODE = "IDLE_TRIGGERED"
            config.CAMERA_ACTIVATION_IDLE_SECONDS = 20.0
            config.SESSION_STATE_POLL_INTERVAL_SECONDS = 0.0
            with patch(
                "main.time.monotonic",
                side_effect=(100.0, 101.0),
            ):
                ready = wait_for_camera_activation(
                    FakeActivityMonitor(),  # type: ignore[arg-type]
                    FakeSessionMonitor(),  # type: ignore[arg-type]
                    presence_recheck_not_before=200.0,
                )
        finally:
            config.CAMERA_MONITORING_MODE = previous_mode
            config.CAMERA_ACTIVATION_IDLE_SECONDS = (
                previous_activation_seconds
            )
            config.SESSION_STATE_POLL_INTERVAL_SECONDS = (
                previous_poll_seconds
            )

        self.assertTrue(ready)


class PresenceAutoStandbyDecisionTests(unittest.TestCase):
    def test_requires_ten_seconds_of_uninterrupted_presence(self) -> None:
        started_at, should_standby = evaluate_presence_auto_standby(
            True,
            cycle_time=100.0,
            confirmed_since=None,
            required_seconds=10.0,
        )
        self.assertEqual(started_at, 100.0)
        self.assertFalse(should_standby)

        started_at, should_standby = evaluate_presence_auto_standby(
            True,
            cycle_time=109.9,
            confirmed_since=started_at,
            required_seconds=10.0,
        )
        self.assertEqual(started_at, 100.0)
        self.assertFalse(should_standby)

        reset_at, should_standby = evaluate_presence_auto_standby(
            False,
            cycle_time=110.0,
            confirmed_since=started_at,
            required_seconds=10.0,
        )
        self.assertIsNone(reset_at)
        self.assertFalse(should_standby)

        unknown_at, should_standby = evaluate_presence_auto_standby(
            None,
            cycle_time=110.5,
            confirmed_since=started_at,
            required_seconds=10.0,
        )
        self.assertIsNone(unknown_at)
        self.assertFalse(should_standby)

        restarted_at, should_standby = evaluate_presence_auto_standby(
            True,
            cycle_time=111.0,
            confirmed_since=reset_at,
            required_seconds=10.0,
        )
        restarted_at, should_standby = evaluate_presence_auto_standby(
            True,
            cycle_time=121.0,
            confirmed_since=restarted_at,
            required_seconds=10.0,
        )
        self.assertEqual(restarted_at, 111.0)
        self.assertTrue(should_standby)

    def test_idle_monitor_returns_presence_standby_outcome(self) -> None:
        class FakeCamera:
            def read(self) -> tuple[bool, np.ndarray]:
                return True, np.zeros((32, 32, 3), dtype=np.uint8)

        class FakeDetector:
            device = "CPU"

            def detect_faces(
                self,
                _frame: np.ndarray,
            ) -> list[FaceDetection]:
                return [FaceDetection(0.99, 1, 1, 10, 10)]

        class FakeActivityMonitor:
            def seconds_since_last_input(self) -> float:
                return 60.0

        class FakeSessionMonitor:
            def is_locked(self) -> bool:
                return False

        previous_mode = config.CAMERA_MONITORING_MODE
        previous_presence_mode = config.PRESENCE_MODE
        previous_standby_seconds = (
            config.CAMERA_PRESENCE_AUTO_STANDBY_SECONDS
        )
        previous_detection_interval = config.DETECTION_INTERVAL_SECONDS
        try:
            config.CAMERA_MONITORING_MODE = "IDLE_TRIGGERED"
            config.PRESENCE_MODE = "ANY_FACE"
            config.CAMERA_PRESENCE_AUTO_STANDBY_SECONDS = 0.0
            config.DETECTION_INTERVAL_SECONDS = 0.0
            outcome = monitor_until_session_pause(
                FakeCamera(),  # type: ignore[arg-type]
                FakeDetector(),  # type: ignore[arg-type]
                FakeActivityMonitor(),  # type: ignore[arg-type]
                FakeSessionMonitor(),  # type: ignore[arg-type]
            )
        finally:
            config.CAMERA_MONITORING_MODE = previous_mode
            config.PRESENCE_MODE = previous_presence_mode
            config.CAMERA_PRESENCE_AUTO_STANDBY_SECONDS = (
                previous_standby_seconds
            )
            config.DETECTION_INTERVAL_SECONDS = previous_detection_interval

        self.assertIs(
            outcome,
            MonitorOutcome.PRESENCE_CONFIRMED_STANDBY,
        )


class LockWarningDecisionTests(unittest.TestCase):
    def test_countdown_requires_five_continuous_seconds(self) -> None:
        started_at, remaining, should_lock = evaluate_lock_warning(
            True,
            cycle_time=100.0,
            warning_started_at=None,
            warning_duration_seconds=5.0,
        )
        self.assertEqual(started_at, 100.0)
        self.assertEqual(remaining, 5)
        self.assertFalse(should_lock)

        started_at, remaining, should_lock = evaluate_lock_warning(
            True,
            cycle_time=103.2,
            warning_started_at=started_at,
            warning_duration_seconds=5.0,
        )
        self.assertEqual(remaining, 2)
        self.assertFalse(should_lock)

        started_at, remaining, should_lock = evaluate_lock_warning(
            True,
            cycle_time=105.0,
            warning_started_at=started_at,
            warning_duration_seconds=5.0,
        )
        self.assertEqual(remaining, 0)
        self.assertTrue(should_lock)

    def test_countdown_is_cancelled_when_conditions_recover(self) -> None:
        started_at, _, _ = evaluate_lock_warning(
            True,
            cycle_time=10.0,
            warning_started_at=None,
            warning_duration_seconds=5.0,
        )
        started_at, remaining, should_lock = evaluate_lock_warning(
            False,
            cycle_time=12.0,
            warning_started_at=started_at,
            warning_duration_seconds=5.0,
        )
        self.assertIsNone(started_at)
        self.assertIsNone(remaining)
        self.assertFalse(should_lock)


class SedentaryReminderDecisionTests(unittest.TestCase):
    def test_reminds_at_each_thirty_minute_boundary(self) -> None:
        tracker = SedentaryTracker(1800.0, 30.0)

        self.assertIsNone(tracker.observe(True, timestamp=100.0))
        self.assertIsNone(tracker.observe(True, timestamp=1899.9))
        self.assertEqual(tracker.observe(True, timestamp=1900.0), 1800.0)
        self.assertIsNone(tracker.observe(True, timestamp=1900.5))
        self.assertEqual(tracker.observe(True, timestamp=3700.0), 3600.0)

    def test_confirmed_leave_resets_but_short_gap_does_not(self) -> None:
        tracker = SedentaryTracker(1800.0, 30.0)

        tracker.observe(True, timestamp=0.0)
        tracker.observe(False, timestamp=900.0)
        tracker.observe(True, timestamp=929.9)
        self.assertEqual(tracker.observe(True, timestamp=1800.0), 1800.0)

        tracker.observe(None, timestamp=2000.0)
        tracker.observe(None, timestamp=2030.0)
        self.assertIsNone(tracker.observe(True, timestamp=2031.0))
        self.assertIsNone(tracker.observe(True, timestamp=3830.9))
        self.assertEqual(tracker.observe(True, timestamp=3831.0), 1800.0)

    def test_signal_and_duration_text_preserve_elapsed_time(self) -> None:
        signal = SedentaryReminderSignal()
        signal.trigger(
            5400.0,
            "已经连续坐了 1 小时 30 分钟，请注意起身活动",
            timestamp=25.0,
        )

        snapshot = signal.snapshot()
        self.assertEqual(snapshot.sequence, 1)
        self.assertEqual(snapshot.seated_seconds, 5400.0)
        self.assertEqual(snapshot.triggered_at, 25.0)
        self.assertEqual(format_sedentary_duration(1800.0), "30 分钟")
        self.assertEqual(
            format_sedentary_duration(5400.0),
            "1 小时 30 分钟",
        )
        signal.clear()
        cleared = signal.snapshot()
        self.assertEqual(cleared.sequence, 2)
        self.assertIsNone(cleared.triggered_at)
        self.assertEqual(cleared.detail, "")


class PresenceDecisionTests(unittest.TestCase):
    def test_any_face_mode_keeps_existing_behavior(self) -> None:
        present, streak = evaluate_presence(
            [FaceDetection(0.9, 1, 1, 10, 10)],
            "ANY_FACE",
            previous_identity_match_streak=9,
            required_identity_confirmations=2,
        )
        self.assertTrue(present)
        self.assertEqual(streak, 0)

    def test_registered_mode_ignores_unknown_faces(self) -> None:
        present, streak = evaluate_presence(
            [
                FaceDetection(
                    0.9,
                    1,
                    1,
                    10,
                    10,
                    identity_similarity=0.42,
                    is_registered_person=False,
                )
            ],
            "REGISTERED_FACE",
            previous_identity_match_streak=0,
            required_identity_confirmations=2,
        )
        self.assertFalse(present)
        self.assertEqual(streak, 0)

    def test_registered_mode_requires_consecutive_matches(self) -> None:
        detection = FaceDetection(
            0.9,
            1,
            1,
            10,
            10,
            identity_similarity=0.82,
            is_registered_person=True,
        )
        first_present, streak = evaluate_presence(
            [detection],
            "REGISTERED_FACE",
            previous_identity_match_streak=0,
            required_identity_confirmations=2,
        )
        second_present, streak = evaluate_presence(
            [detection],
            "REGISTERED_FACE",
            previous_identity_match_streak=streak,
            required_identity_confirmations=2,
        )
        self.assertFalse(first_present)
        self.assertTrue(second_present)
        self.assertEqual(streak, 2)

    def test_embedding_normalization_rejects_wrong_dimensions(self) -> None:
        with self.assertRaises(FaceTemplateError):
            FaceTemplateStore.normalize_embedding(
                np.ones(255, dtype=np.float32)
            )

    def test_recognizer_annotates_registered_and_unknown_faces(self) -> None:
        recognizer = FaceIdentityRecognizer.__new__(FaceIdentityRecognizer)
        registered_embedding = np.zeros(256, dtype=np.float32)
        registered_embedding[0] = 1.0
        unknown_embedding = np.zeros(256, dtype=np.float32)
        unknown_embedding[1] = 1.0
        embeddings = iter((registered_embedding, unknown_embedding))
        recognizer.extract_embedding = lambda frame, detection: next(
            embeddings
        )
        template = FaceTemplate(
            embedding=registered_embedding,
            sample_count=12,
            created_at="test",
        )
        detections = recognizer.recognize_faces(
            np.zeros((20, 20, 3), dtype=np.uint8),
            [
                FaceDetection(0.9, 1, 1, 9, 9),
                FaceDetection(0.9, 10, 1, 18, 9),
            ],
            template,
            similarity_threshold=0.70,
        )
        self.assertTrue(detections[0].is_registered_person)
        self.assertFalse(detections[1].is_registered_person)
        self.assertAlmostEqual(detections[0].identity_similarity, 1.0)
        self.assertAlmostEqual(detections[1].identity_similarity, 0.0)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
    def test_template_is_dpapi_protected_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "registered-face.dat"
            store = FaceTemplateStore(path)
            source = np.arange(1, 257, dtype=np.float32)
            saved = store.save_embeddings(
                [source + value for value in range(5)]
            )
            loaded = store.load()
            self.assertEqual(saved.sample_count, 5)
            self.assertEqual(loaded.embedding.shape, (256,))
            self.assertAlmostEqual(
                float(np.linalg.norm(loaded.embedding)),
                1.0,
                places=5,
            )
            self.assertNotIn(
                b"embedding_f32_base64",
                path.read_bytes(),
            )


class PrivacyBlurDecisionTests(unittest.TestCase):
    def test_any_face_monitor_activates_blur_without_face_template(
        self,
    ) -> None:
        class FakeCamera:
            def read(self) -> tuple[bool, np.ndarray]:
                return True, np.zeros((32, 32, 3), dtype=np.uint8)

        class FakeDetector:
            device = "CPU"

            def detect_faces(
                self,
                _frame: np.ndarray,
            ) -> list[FaceDetection]:
                return [
                    FaceDetection(0.99, 1, 1, 10, 10),
                    FaceDetection(0.98, 12, 1, 22, 10),
                ]

        class FakeActivityMonitor:
            def seconds_since_last_input(self) -> float:
                return 0.0

        class FakeSessionMonitor:
            def __init__(self) -> None:
                self.check_count = 0

            def is_locked(self) -> bool:
                self.check_count += 1
                return self.check_count >= 3

        signal = PrivacyBlurSignal()
        statuses: list[tuple[str, str]] = []
        previous_presence_mode = config.PRESENCE_MODE
        previous_camera_mode = config.CAMERA_MONITORING_MODE
        previous_privacy_blur = config.PRIVACY_BLUR_ENABLED
        previous_detection_interval = config.DETECTION_INTERVAL_SECONDS
        try:
            config.PRESENCE_MODE = "ANY_FACE"
            config.CAMERA_MONITORING_MODE = "CONTINUOUS"
            config.PRIVACY_BLUR_ENABLED = True
            config.DETECTION_INTERVAL_SECONDS = 0.0
            outcome = monitor_until_session_pause(
                FakeCamera(),  # type: ignore[arg-type]
                FakeDetector(),  # type: ignore[arg-type]
                FakeActivityMonitor(),  # type: ignore[arg-type]
                FakeSessionMonitor(),  # type: ignore[arg-type]
                privacy_blur_signal=signal,
                status_callback=lambda state, detail: statuses.append(
                    (state, detail)
                ),
            )
        finally:
            config.PRESENCE_MODE = previous_presence_mode
            config.CAMERA_MONITORING_MODE = previous_camera_mode
            config.PRIVACY_BLUR_ENABLED = previous_privacy_blur
            config.DETECTION_INTERVAL_SECONDS = (
                previous_detection_interval
            )

        self.assertIs(outcome, MonitorOutcome.SESSION_LOCKED)
        self.assertIn(
            (
                "privacy_blur",
                "检测到至少两个人 · 快速甩动鼠标恢复",
            ),
            statuses,
        )

    def test_multiple_faces_activate_without_registered_owner(self) -> None:
        guard = SecondPersonPrivacyGuard(
            confirmation_frames=2,
            rearm_clear_seconds=60.0,
        )

        self.assertFalse(
            guard.update(False, 2, timestamp=0.0)
        )
        self.assertTrue(
            guard.update(False, 2, timestamp=0.5)
        )
        self.assertFalse(
            guard.update(False, 2, timestamp=1.0)
        )

    def test_single_unregistered_face_never_auto_dismisses(self) -> None:
        guard = SecondPersonPrivacyGuard(
            confirmation_frames=2,
            rearm_clear_seconds=60.0,
            auto_dismiss_owner_alone_seconds=3.0,
        )
        guard.evaluate(False, 2, timestamp=0.0)
        guard.evaluate(False, 2, timestamp=0.5)

        self.assertFalse(
            guard.evaluate(False, 1, timestamp=1.0).auto_dismiss
        )
        self.assertFalse(
            guard.evaluate(False, 1, timestamp=10.0).auto_dismiss
        )

    def test_rearms_after_under_two_people_for_sixty_seconds(self) -> None:
        guard = SecondPersonPrivacyGuard(
            confirmation_frames=2,
            rearm_clear_seconds=60.0,
        )
        guard.update(True, 2, timestamp=0.0)
        self.assertTrue(guard.update(True, 2, timestamp=0.5))

        self.assertFalse(guard.update(False, 1, timestamp=1.0))
        self.assertFalse(guard.update(False, 0, timestamp=31.0))
        self.assertFalse(guard.update(False, 1, timestamp=61.0))
        self.assertFalse(guard.update(True, 2, timestamp=61.5))
        self.assertTrue(guard.update(True, 2, timestamp=62.0))

    def test_auto_dismisses_after_owner_is_alone_for_three_seconds(self) -> None:
        guard = SecondPersonPrivacyGuard(
            confirmation_frames=2,
            rearm_clear_seconds=60.0,
            auto_dismiss_owner_alone_seconds=3.0,
        )
        guard.evaluate(True, 2, timestamp=0.0)
        self.assertTrue(
            guard.evaluate(True, 2, timestamp=0.5).activate
        )

        self.assertFalse(
            guard.evaluate(True, 1, timestamp=1.0).auto_dismiss
        )
        self.assertFalse(
            guard.evaluate(True, 1, timestamp=3.9).auto_dismiss
        )
        self.assertTrue(
            guard.evaluate(True, 1, timestamp=4.0).auto_dismiss
        )
        self.assertFalse(
            guard.evaluate(True, 1, timestamp=4.5).auto_dismiss
        )

    def test_auto_dismiss_requires_confirmed_owner_alone(self) -> None:
        guard = SecondPersonPrivacyGuard(
            confirmation_frames=2,
            rearm_clear_seconds=60.0,
            auto_dismiss_owner_alone_seconds=3.0,
        )
        guard.evaluate(True, 2, timestamp=0.0)
        guard.evaluate(True, 2, timestamp=0.5)

        self.assertFalse(
            guard.evaluate(False, 1, timestamp=1.0).auto_dismiss
        )
        self.assertFalse(
            guard.evaluate(False, 1, timestamp=5.0).auto_dismiss
        )
        self.assertFalse(
            guard.evaluate(True, 1, timestamp=5.5).auto_dismiss
        )
        self.assertFalse(
            guard.evaluate(True, 1, timestamp=8.4).auto_dismiss
        )
        self.assertTrue(
            guard.evaluate(True, 1, timestamp=8.5).auto_dismiss
        )

    def test_unknown_or_second_person_restarts_auto_dismiss_timer(self) -> None:
        guard = SecondPersonPrivacyGuard(
            confirmation_frames=2,
            rearm_clear_seconds=60.0,
            auto_dismiss_owner_alone_seconds=3.0,
        )
        guard.evaluate(True, 2, timestamp=0.0)
        guard.evaluate(True, 2, timestamp=0.5)
        guard.evaluate(True, 1, timestamp=1.0)
        guard.mark_visual_state_unknown()
        guard.evaluate(True, 1, timestamp=3.0)
        guard.evaluate(True, 2, timestamp=5.0)

        self.assertFalse(
            guard.evaluate(True, 1, timestamp=6.0).auto_dismiss
        )
        self.assertFalse(
            guard.evaluate(True, 1, timestamp=8.9).auto_dismiss
        )
        self.assertTrue(
            guard.evaluate(True, 1, timestamp=9.0).auto_dismiss
        )

    def test_two_people_or_unknown_frame_restarts_rearm_timer(self) -> None:
        guard = SecondPersonPrivacyGuard(
            confirmation_frames=2,
            rearm_clear_seconds=60.0,
        )
        guard.update(True, 2, timestamp=0.0)
        self.assertTrue(guard.update(True, 2, timestamp=0.5))
        guard.update(False, 1, timestamp=1.0)
        guard.update(False, 2, timestamp=40.0)
        guard.update(False, 0, timestamp=41.0)
        guard.mark_visual_state_unknown()
        self.assertFalse(guard.update(False, 0, timestamp=100.0))
        self.assertFalse(guard.update(False, 1, timestamp=160.0))
        self.assertFalse(guard.update(True, 2, timestamp=160.5))
        self.assertTrue(guard.update(True, 2, timestamp=161.0))

    def test_signal_activation_and_dismissal_are_latched(self) -> None:
        signal = PrivacyBlurSignal()
        self.assertTrue(signal.activate("second person"))
        first = signal.snapshot()
        self.assertTrue(first.active)
        self.assertEqual(first.detail, "second person")
        self.assertFalse(signal.activate("duplicate"))
        self.assertTrue(signal.dismiss())
        self.assertFalse(signal.snapshot().active)
        self.assertFalse(signal.dismiss())

    def test_quick_back_and_forth_mouse_path_dismisses(self) -> None:
        detector = MouseShakeDetector()
        detected = False
        for index, x in enumerate((0, 190, -20, 200, -30)):
            detected = detector.record(x, 100, timestamp=index * 0.08)
        self.assertTrue(detected)

    def test_fast_one_way_mouse_motion_is_not_a_shake(self) -> None:
        detector = MouseShakeDetector()
        results = [
            detector.record(x, 100, timestamp=index * 0.08)
            for index, x in enumerate((0, 200, 400, 600, 800))
        ]
        self.assertNotIn(True, results)

    def test_slow_back_and_forth_mouse_motion_is_not_a_shake(self) -> None:
        detector = MouseShakeDetector()
        results = [
            detector.record(x, 100, timestamp=index * 0.5)
            for index, x in enumerate((0, 190, -20, 200, -30))
        ]
        self.assertNotIn(True, results)

    def test_monitor_union_handles_negative_and_vertical_coordinates(self) -> None:
        self.assertEqual(
            bounding_rect(
                (
                    (-1920, 0, 0, 1080),
                    (0, -200, 2560, 1240),
                )
            ),
            (-1920, -200, 2560, 1240),
        )

    def test_work_areas_are_relative_to_one_cross_screen_window(self) -> None:
        self.assertEqual(
            relative_work_regions(
                (-1920, -200, 2560, 1240),
                (
                    (-1920, 0, 0, 1040),
                    (0, -200, 2560, 1160),
                ),
            ),
            (
                (0, 200, 1920, 1240),
                (1920, 0, 4480, 1360),
            ),
        )

    def test_user_validated_acrylic_strength_maps_to_layered_alpha(self) -> None:
        self.assertEqual(acrylic_alpha_from_strength(96), 245)

    @unittest.skipUnless(os.name == "nt", "Windows monitors are required")
    def test_windows_monitor_enumeration_reports_valid_bounds(self) -> None:
        monitors = enumerate_monitor_work_areas()
        self.assertGreaterEqual(len(monitors), 1)
        self.assertTrue(
            all(
                monitor.work[2] > monitor.work[0]
                and monitor.work[3] > monitor.work[1]
                for monitor in monitors
            )
        )


class DebugFrameBufferTests(unittest.TestCase):
    def test_publish_snapshot_copy_and_clear(self) -> None:
        buffer = DebugFrameBuffer()
        source = np.full((12, 16, 3), 7, dtype=np.uint8)
        detection = FaceDetection(0.88, 1, 2, 10, 9)

        buffer.publish(
            frame_bgr=source,
            detections=[detection],
            device="NPU",
            status="检测到人脸",
            face_absent_seconds=0.0,
            input_idle_seconds=2.5,
            startup_elapsed_seconds=31.0,
            should_lock=False,
            inference_ms=7.8,
        )
        source.fill(99)

        first = buffer.snapshot()
        self.assertIsNotNone(first.frame_bgr)
        assert first.frame_bgr is not None
        self.assertTrue(np.all(first.frame_bgr == 7))
        self.assertTrue(first.face_detected)
        self.assertTrue(first.presence_detected)
        self.assertEqual(first.detections, (detection,))
        self.assertEqual(first.face_absent_seconds, 0.0)
        self.assertEqual(first.input_idle_seconds, 2.5)
        self.assertEqual(first.startup_elapsed_seconds, 31.0)
        self.assertFalse(first.should_lock)
        self.assertEqual(first.inference_ms, 7.8)

        first.frame_bgr.fill(33)
        second = buffer.snapshot()
        assert second.frame_bgr is not None
        self.assertTrue(np.all(second.frame_bgr == 7))

        buffer.clear("Windows 已锁定", device="NPU")
        cleared = buffer.snapshot()
        self.assertIsNone(cleared.frame_bgr)
        self.assertIsNone(cleared.face_detected)
        self.assertEqual(cleared.detections, ())
        self.assertEqual(cleared.device, "NPU")
        self.assertIsNone(cleared.inference_ms)
        self.assertIsNone(cleared.should_lock)
        self.assertIsNone(cleared.input_idle_seconds)

        buffer.clear(
            "摄像头待机",
            device="NPU",
            input_idle_seconds=7.5,
        )
        standby = buffer.snapshot()
        self.assertIsNone(standby.frame_bgr)
        self.assertEqual(standby.input_idle_seconds, 7.5)
        self.assertIsNone(standby.inference_ms)
        self.assertIsNone(standby.should_lock)

    def test_concurrent_reads_never_observe_torn_frames(self) -> None:
        buffer = DebugFrameBuffer()
        failures: list[str] = []
        writer_finished = threading.Event()

        def writer() -> None:
            for value in range(1, 101):
                frame = np.full(
                    (24, 32, 3),
                    value,
                    dtype=np.uint8,
                )
                buffer.publish(frame, (), "CPU", f"frame-{value}")
            writer_finished.set()

        def reader() -> None:
            while not writer_finished.is_set():
                snapshot = buffer.snapshot()
                if snapshot.frame_bgr is None:
                    continue
                first_value = snapshot.frame_bgr[0, 0, 0]
                if not np.all(snapshot.frame_bgr == first_value):
                    failures.append("Observed a partially updated frame")
                    return

        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)
        writer_thread.start()
        reader_thread.start()
        writer_thread.join(timeout=5)
        reader_thread.join(timeout=5)

        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(reader_thread.is_alive())
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
