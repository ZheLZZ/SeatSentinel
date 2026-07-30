"""Tests for detection parsing and the in-memory debug-frame handoff."""

from __future__ import annotations

import threading
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

import config
from app import TrayApplication
from debug_frame import DebugFrameBuffer
from detector import DetectorInferenceError, FaceDetection, FaceDetector
from single_instance import DEBUG_WINDOW_EVENT_NAME, MUTEX_NAME
from user_settings import AppSettings, SettingsStore


class ApplicationDefaultsTests(unittest.TestCase):
    def test_title_and_lock_timeouts(self) -> None:
        settings = AppSettings.defaults()
        self.assertEqual(config.APPLICATION_TITLE, "SeatSentinel")
        self.assertEqual(config.APPLICATION_VERSION, "0.1.1-beta")
        self.assertEqual(config.USER_DATA_DIRECTORY.name, "SeatSentinel")
        self.assertIn("SeatSentinel", MUTEX_NAME)
        self.assertIn("SeatSentinel", DEBUG_WINDOW_EVENT_NAME)
        self.assertEqual(settings.inference_device, "NPU")
        self.assertEqual(settings.face_absence_timeout_seconds, 60)
        self.assertEqual(settings.input_idle_timeout_seconds, 60)
        self.assertEqual(os.environ.get("DO_NOT_TRACK"), "1")
        self.assertEqual(os.environ.get("SCARF_NO_ANALYTICS"), "1")

    def test_inference_device_setting_is_normalized(self) -> None:
        settings = AppSettings.from_mapping(
            {"inference_device": "cpu"}
        )
        self.assertEqual(settings.inference_device, "CPU")

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
                    self.assertTrue(new_path.is_file())


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
