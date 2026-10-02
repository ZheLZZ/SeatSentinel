"""Offline regressions for cancellation and conservative inference handling."""

from contextlib import ExitStack
from threading import Event
import unittest
from unittest.mock import Mock, patch

import numpy as np

import config
import face_registration
import main
from app_lock import AppLockSignal
from detector import DetectorInferenceError, FaceDetection, FaceDetector
from face_identity import FaceTemplate
from user_settings import AppSettings


class DetectionOutputValidationTests(unittest.TestCase):
    def parse(self, rows):
        return FaceDetector.parse_detections(
            np.asarray(rows, dtype=np.float32).reshape(1, 1, -1, 7),
            frame_width=100, frame_height=100, confidence_threshold=.6,
        )

    def test_non_finite_valid_record_is_an_inference_failure(self):
        for field in range(7):
            for invalid in (np.nan, np.inf, -np.inf):
                with self.subTest(field=field, value=invalid):
                    row = [0, 1, .9, .1, .1, .8, .8]
                    row[field] = invalid
                    with self.assertRaises(DetectorInferenceError):
                        self.parse([row])

    def test_end_marker_ignores_unused_fields_and_following_rows(self):
        detections = self.parse([
            [0, 1, .9, .1, .1, .8, .8],
            [-1, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan],
            [np.nan] * 7,
        ])
        self.assertEqual(len(detections), 1)

    def test_low_confidence_padding_does_not_require_valid_coordinates(self):
        self.assertEqual(self.parse([
            [0, np.nan, 0, np.nan, np.inf, -np.inf, np.nan],
            [0, 1, .1, 0, 0, 0, 0],
        ]), [])


class RegistrationCancellationTests(unittest.TestCase):
    def enroll(self, *, cancel_at=None, initialization_seconds=0):
        clock = [0.0]
        stop = Event()
        count = [0]
        vector = np.zeros(256, dtype=np.float32)
        vector[0] = 1
        detector = Mock()
        detector.detect_faces.return_value = [FaceDetection(.99, 5, 5, 100, 100)]
        recognizer = Mock()
        def extract(frame, detection):
            count[0] += 1
            if cancel_at == "final_inference" and count[0] == 5:
                stop.set()
            return vector
        recognizer.extract_embedding.side_effect = extract
        def initialize(*args, **kwargs):
            clock[0] += initialization_seconds
            return recognizer
        camera = Mock()
        def read():
            clock[0] += .5
            return True, np.zeros((120, 120, 3), dtype=np.uint8)
        camera.read.side_effect = read
        store = Mock()
        store.save_embeddings.return_value = FaceTemplate(vector, 5, "test")
        def callback(update):
            if cancel_at == "save_callback" and "加密保存" in update.message:
                stop.set()
            if cancel_at == "final_sample_callback" and update.accepted_samples == 5:
                stop.set()
        with ExitStack() as stack:
            stack.enter_context(patch.object(config, "FACE_REGISTRATION_SAMPLE_COUNT", 5))
            stack.enter_context(patch("face_registration.time.monotonic", side_effect=lambda: clock[0]))
            stack.enter_context(patch("face_registration.FaceDetector", return_value=detector))
            stack.enter_context(patch("face_registration.FaceIdentityRecognizer", side_effect=initialize))
            stack.enter_context(patch("face_registration.Camera", return_value=camera))
            if cancel_at:
                with self.assertRaises(face_registration.FaceRegistrationCancelled):
                    face_registration.register_face_from_camera(AppSettings.defaults(), store, stop, callback)
            else:
                result = face_registration.register_face_from_camera(AppSettings.defaults(), store, stop, callback)
                self.assertEqual(result.sample_count, 5)
        camera.release.assert_called_once()
        detector.close.assert_called_once()
        recognizer.close.assert_called_once()
        return store, camera

    def test_cancel_final_inference_preserves_previous_template(self):
        store, _ = self.enroll(cancel_at="final_inference")
        store.save_embeddings.assert_not_called()

    def test_final_sample_and_save_callbacks_can_cancel_before_commit(self):
        for where in ("final_sample_callback", "save_callback"):
            with self.subTest(where=where):
                store, _ = self.enroll(cancel_at=where)
                store.save_embeddings.assert_not_called()

    def test_slow_initialization_does_not_consume_sampling_time(self):
        store, camera = self.enroll(initialization_seconds=61)
        store.save_embeddings.assert_called_once()
        self.assertEqual(camera.read.call_count, 5)


class MonitorCancellationTests(unittest.TestCase):
    def run_monitor(self, stop_at=None, mode="SYSTEM", registered=False,
                    switch_config=False, invalid_output=False):
        stop = Event()
        clock = [0.0]
        statuses = []
        camera = Mock()
        def read():
            clock[0] += 1
            if stop_at == "read":
                stop.set()
            return True, np.zeros((100, 100, 3), dtype=np.uint8)
        camera.read.side_effect = read
        detector = Mock(device="CPU")
        def detect(frame):
            if stop_at == "detect" or (stop_at == "final_detect" and clock[0] == 3):
                stop.set()
            if switch_config:
                config.PRESENCE_MODE = "REGISTERED_FACE"
                config.LOCK_MODE = "APPLICATION"
                return [FaceDetection(.99, 5, 5, 90, 90)]
            if invalid_output:
                return FaceDetector.parse_detections(
                    np.full((1, 1, 200, 7), np.nan, dtype=np.float32),
                    frame_width=100, frame_height=100, confidence_threshold=.6,
                )
            return []
        detector.detect_faces.side_effect = detect
        identity = Mock(device="CPU")
        def recognize(*args):
            if stop_at == "recognize" or (stop_at == "final_recognize" and clock[0] == 3):
                stop.set()
            return []
        identity.recognize_faces.side_effect = recognize
        input_calls = [0]
        activity = Mock()
        def input_idle():
            input_calls[0] += 1
            if stop_at == "final_input" and input_calls[0] == 4:
                stop.set()
            return 100
        activity.seconds_since_last_input.side_effect = input_idle
        session = Mock()
        session.is_locked.side_effect = [False, False, False, True]
        def report(state, detail):
            statuses.append((state, detail))
            if state == "locking" and stop_at == "locking_callback":
                stop.set()
        settings = dict(
            LOCK_MODE=mode, PRESENCE_MODE="REGISTERED_FACE" if registered else "ANY_FACE",
            CAMERA_MONITORING_MODE="CONTINUOUS", PRIVACY_BLUR_ENABLED=False,
            DETECTION_INTERVAL_SECONDS=0, FACE_ABSENCE_TIMEOUT_SECONDS=1,
            INPUT_IDLE_TIMEOUT_SECONDS=1, STARTUP_GRACE_PERIOD_SECONDS=0,
            LOCK_WARNING_SECONDS=1,
        )
        with ExitStack() as stack:
            for key, value in settings.items():
                stack.enter_context(patch.object(config, key, value))
            stack.enter_context(patch("main.time.monotonic", side_effect=lambda: clock[0]))
            lock = stack.enter_context(patch("main.lock_workstation"))
            outcome = main.monitor_until_session_pause(
                camera, detector, activity, session, stop_event=stop,
                identity_recognizer=identity if registered else None,
                face_template=Mock() if registered else None,
                app_lock_signal=AppLockSignal(), status_callback=report,
            )
        return outcome, lock.call_count, statuses

    def test_stop_after_capture_detection_and_identity_inference(self):
        for where in ("read", "detect", "recognize", "final_detect", "final_recognize"):
            with self.subTest(where=where):
                outcome, locks, _ = self.run_monitor(stop_at=where, registered="recognize" in where)
                self.assertEqual(outcome, main.MonitorOutcome.STOP_REQUESTED)
                self.assertEqual(locks, 0)

    def test_stop_before_final_system_or_application_lock_request(self):
        for mode in ("SYSTEM", "APPLICATION"):
            for where in ("final_input", "locking_callback"):
                with self.subTest(mode=mode, where=where):
                    outcome, locks, _ = self.run_monitor(stop_at=where, mode=mode)
                    self.assertEqual(outcome, main.MonitorOutcome.STOP_REQUESTED)
                    self.assertEqual(locks, 0)

    def test_runtime_mode_change_does_not_reinterpret_current_frames(self):
        outcome, locks, statuses = self.run_monitor(switch_config=True)
        self.assertEqual(outcome, main.MonitorOutcome.SESSION_LOCKED)
        self.assertEqual(locks, 0)
        self.assertTrue(any("在场=是" in detail for _, detail in statuses))

    def test_invalid_detector_output_never_counts_as_absence(self):
        outcome, locks, statuses = self.run_monitor(invalid_output=True)
        self.assertEqual(outcome, main.MonitorOutcome.SESSION_LOCKED)
        self.assertEqual(locks, 0)
        self.assertTrue(any(state == "inference_error" for state, _ in statuses))

    def test_stop_while_camera_opens_releases_it(self):
        stop = Event()
        camera = Mock()
        camera.open.side_effect = stop.set
        session = Mock()
        session.is_locked.return_value = False
        with patch.object(config, "CAMERA_MONITORING_MODE", "CONTINUOUS"):
            self.assertIsNone(main.open_camera_when_session_ready(camera, session, stop_event=stop))
        camera.release.assert_called_once()

    def test_stop_while_camera_releases_cancels_queued_application_lock(self):
        stop = Event()
        camera = Mock()
        camera.release.side_effect = stop.set
        signal = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(config, "PRESENCE_MODE", "ANY_FACE"))
            stack.enter_context(patch("main.FaceDetector", return_value=Mock(device="CPU")))
            stack.enter_context(patch("main.Camera", return_value=camera))
            stack.enter_context(patch("main.ActivityMonitor"))
            stack.enter_context(patch("main.SessionMonitor"))
            stack.enter_context(patch("main.wait_for_session_ready", return_value=False))
            stack.enter_context(patch("main.wait_for_camera_activation", return_value=True))
            stack.enter_context(patch("main.open_camera_when_session_ready", return_value=True))
            stack.enter_context(patch("main.monitor_until_session_pause",
                                      return_value=main.MonitorOutcome.APP_LOCK_REQUESTED))
            self.assertEqual(main.run(stop_event=stop, app_lock_signal=signal), 0)
        signal.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
