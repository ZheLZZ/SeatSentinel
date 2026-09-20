"""Regression tests for application-lock authentication and OS-lock separation."""

import tempfile
import json
import ctypes
import time
import threading
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import config
import main
from app_lock import (AppLockSignal, UnlockGate, hash_password,
                      validate_password_record, verify_password)
from app_lock_windows import AwakeRequest, InputGuard, KeyboardData, block_key
from user_settings import AppSettings, SettingsError, SettingsStore
from app import TrayApplication, LOCK_MODE_LABELS
from private_test_unlock import load_private_test_unlock


class CredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = hash_password("Example!482")

    def test_salted_hash_and_wrong_password(self):
        self.assertNotIn("Example!482", self.record)
        self.assertNotEqual(self.record, hash_password("Example!482"))
        self.assertTrue(verify_password("Example!482", self.record))
        self.assertFalse(verify_password("Wrong!482", self.record))

    def test_bad_record_and_password_bounds(self):
        for value in ("", "pbkdf2_sha256$9999999999$00$00", self.record[:-1]):
            self.assertFalse(validate_password_record(value))
            self.assertFalse(verify_password("Example!482", value))
        for password in ("short", "x" * 129, "has space", "中文密码12345"):
            with self.assertRaises(ValueError):
                hash_password(password)

    def test_cooldown_blocks_even_correct_password_until_expiry(self):
        now = [0.0]
        gate = UnlockGate(self.record, clock=lambda: now[0])
        for _ in range(5):
            self.assertFalse(gate.attempt("Incorrect"))
        self.assertEqual(gate.retry_seconds, 5)
        self.assertFalse(gate.attempt("Example!482"))
        now[0] = 5.0
        self.assertTrue(gate.attempt("Example!482"))

    def test_legacy_defaults_and_password_required(self):
        self.assertEqual(AppSettings.from_mapping({}).lock_mode, "SYSTEM")
        with self.assertRaises(SettingsError):
            AppSettings.from_mapping({"lock_mode": "APPLICATION"})
        with self.assertRaises(SettingsError):
            AppSettings.from_mapping({"app_lock_password_hash": "broken"})

    def test_settings_roundtrip_contains_no_plaintext(self):
        settings = AppSettings.from_mapping({
            "lock_mode": "APPLICATION", "app_lock_password_hash": self.record,
        })
        self.assertNotIn(self.record, repr(settings))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path=path)
            store.save(settings)
            self.assertEqual(store.load(), settings)
            self.assertNotIn("Example!482", path.read_text())


class SignalTests(unittest.TestCase):
    def test_worker_cancellation_never_unlocks_displayed_lock(self):
        signal = AppLockSignal()
        signal.request()
        signal.complete("locked")
        stop = threading.Event()
        stop.set()
        self.assertEqual(signal.wait(stop), "cancelled")
        self.assertTrue(signal.busy)
        self.assertEqual(signal.state, "locked")

    def test_pending_request_can_cancel(self):
        signal = AppLockSignal()
        signal.request()
        stop = threading.Event()
        stop.set()
        self.assertEqual(signal.wait(stop), "cancelled")
        self.assertFalse(signal.busy)

    def test_request_is_idempotent_while_locked(self):
        signal = AppLockSignal()
        signal.request()
        signal.complete("locked")
        signal.request()
        self.assertEqual(signal.state, "locked")
        signal.complete("unlocked")
        self.assertEqual(signal.wait(None), "unlocked")


class SettingsUiTests(unittest.TestCase):
    def application(self):
        application = TrayApplication.__new__(TrayApplication)
        application._service = Mock()
        application._service.app_lock_signal = AppLockSignal()
        application._settings_store = Mock()
        application._privacy_hotkey = object()
        application._replace_privacy_hotkey = Mock()
        application._tray_icon = Mock()
        application._face_template_store = Mock()
        return application

    def variables(self, settings, mode="APPLICATION", old="", new="Example!482"):
        values = asdict(settings)
        values.update(lock_mode=LOCK_MODE_LABELS[mode], app_lock_old_password=old,
                      app_lock_new_password=new, app_lock_confirm_password=new)
        return {key: Mock(get=Mock(return_value=value)) for key, value in values.items()}

    def test_enable_and_preserve_existing_password_without_exposing_plaintext(self):
        application = self.application()
        old = AppSettings.defaults()
        with patch.object(AppSettings, "apply_to_runtime"):
            application._save_settings(self.variables(old), Mock(), old)
            saved = application._settings_store.save.call_args.args[0]
            self.assertEqual(saved.lock_mode, "APPLICATION")
            self.assertTrue(verify_password("Example!482", saved.app_lock_password_hash))
            application._save_settings(self.variables(saved, new=""), Mock(), saved)
            self.assertEqual(application._settings_store.save.call_args.args[0], saved)

    def test_password_change_or_disable_requires_current_password(self):
        application = self.application()
        settings = AppSettings.from_mapping({"lock_mode": "APPLICATION",
                                            "app_lock_password_hash": hash_password("Example!482")})
        with patch("app.messagebox.showerror") as error:
            application._save_settings(self.variables(settings, mode="SYSTEM", new=""), Mock(), settings)
            error.assert_called_once()
            application._settings_store.save.assert_not_called()

    def test_tray_commands_cannot_pause_or_exit_a_locked_application(self):
        application = self.application()
        application._service.app_lock_signal.request()
        application._pause(None, None)
        application._request_shutdown()
        application._show_settings()
        application._show_debug_window()
        application._service.pause_async.assert_not_called()


class PrivateTestUnlockTests(unittest.TestCase):
    def test_local_record_requires_matching_version_and_expiry(self):
        chord = "Ctrl+Alt+Shift+F6"
        record = {"version": config.APPLICATION_VERSION, "expires_at": 200.0,
                  "shortcut_hash": hash_password(chord)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            self.assertIsNone(load_private_test_unlock(path))
            path.write_text(json.dumps(record))
            now = [100.0]
            unlock = load_private_test_unlock(path, clock=lambda: now[0])
            self.assertTrue(unlock.attempt(chord))
            self.assertFalse(unlock.attempt("Ctrl+Alt+Shift+F8"))
            now[0] = 201.0
            self.assertFalse(unlock.attempt(chord))
            self.assertIsNone(load_private_test_unlock(path, clock=lambda: now[0]))
            record["version"] = "different-build"
            path.write_text(json.dumps(record))
            self.assertIsNone(load_private_test_unlock(path, clock=lambda: 100))
            path.write_text("broken-json")
            self.assertIsNone(load_private_test_unlock(path))


class InputAndPowerTests(unittest.TestCase):
    def test_recovery_chord_recognizes_modifiers_even_when_hooks_block_them(self):
        callbacks, candidates = {}, []
        guard = InputGuard(candidates.append)
        guard.handles = (123,)
        def install(kind, callback, module, thread):
            callbacks[kind] = callback
            return kind
        with ExitStack() as stack:
            stack.enter_context(patch("app_lock_windows.user32.SetWindowsHookExW", side_effect=install))
            stack.enter_context(patch("app_lock_windows.user32.UnhookWindowsHookEx", return_value=True))
            stack.enter_context(patch("app_lock_windows.user32.PeekMessageW", return_value=False))
            stack.enter_context(patch("app_lock_windows.user32.GetForegroundWindow", return_value=123))
            stack.enter_context(patch("app_lock_windows.user32.GetAsyncKeyState", return_value=0))
            stack.enter_context(patch("app_lock_windows.user32.CallNextHookEx", return_value=0))
            guard.start()
            try:
                for vk in (0xA2, 0xA4, 0xA0, 0x75, 0x75):
                    key = KeyboardData(vkCode=vk)
                    callbacks[13](0, 0x0100, ctypes.addressof(key))
                self.assertEqual(candidates, ["Ctrl+Alt+Shift+F6"])
                # Auto-repeat must not enqueue more guesses; release re-arms.
                for vk in (0x75, 0xA0, 0xA4, 0xA2):
                    key = KeyboardData(vkCode=vk)
                    callbacks[13](0, 0x0101, ctypes.addressof(key))
                key = KeyboardData(vkCode=0x75)
                callbacks[13](0, 0x0100, ctypes.addressof(key))
                self.assertEqual(len(candidates), 1)
            finally:
                guard.close()

    def test_shortcuts_and_focus_escape_are_blocked(self):
        for key in (0x5B, 0x5C, 0x1B, 0x73):
            self.assertTrue(block_key(key, alt=False, ctrl=False, own_focus=True))
        self.assertTrue(block_key(0x09, alt=True, ctrl=False, own_focus=True))
        self.assertTrue(block_key(0x1B, alt=False, ctrl=True, own_focus=True))
        self.assertTrue(block_key(0x41, alt=False, ctrl=False, own_focus=False))
        for key in (0x41, 0x38, 0x08, 0x0D):
            self.assertFalse(block_key(key, alt=False, ctrl=False, own_focus=True))

    def test_wake_request_restores_previous_flags_without_repetition(self):
        with patch("app_lock_windows.kernel32.SetThreadExecutionState",
                   return_value=0x80000000) as api:
            request = AwakeRequest()
            request.update(True)
            request.update(True)
            request.update(False)
            self.assertEqual([call.args[0] for call in api.call_args_list],
                             [0x80000003, 0x80000000])

    def test_rejected_wake_request_is_not_reported_active(self):
        with patch("app_lock_windows.kernel32.SetThreadExecutionState", return_value=0):
            request = AwakeRequest()
            with self.assertRaises(RuntimeError):
                request.update(True)
            self.assertFalse(request.active)


class MonitoringIntegrationTests(unittest.TestCase):
    def _monitor(self, mode, input_idle=100):
        camera = Mock()
        camera.read.return_value = (True, np.zeros((32, 32, 3), dtype=np.uint8))
        detector = Mock(device="CPU")
        detector.detect_faces.return_value = []
        activity = Mock()
        activity.seconds_since_last_input.return_value = input_idle
        session = Mock()
        # Second cycle exits if recent input cancels the first lock attempt.
        session.is_locked.side_effect = [False, True]
        with ExitStack() as stack:
            for key, value in {"LOCK_MODE": mode, "PRESENCE_MODE": "ANY_FACE",
                               "CAMERA_MONITORING_MODE": "CONTINUOUS",
                               "PRIVACY_BLUR_ENABLED": False,
                               "DETECTION_INTERVAL_SECONDS": 0}.items():
                stack.enter_context(patch.object(config, key, value))
            stack.enter_context(patch("main.evaluate_lock_warning", return_value=(0, 0, True)))
            lock = stack.enter_context(patch("main.lock_workstation"))
            outcome = main.monitor_until_session_pause(
                camera, detector, activity, session, app_lock_signal=AppLockSignal()
            )
            return outcome, lock.call_count

    def test_application_mode_never_calls_windows_lock(self):
        outcome, lock_calls = self._monitor("APPLICATION")
        self.assertEqual(outcome, main.MonitorOutcome.APP_LOCK_REQUESTED)
        self.assertEqual(lock_calls, 0)

    def test_system_mode_retains_windows_lock(self):
        outcome, lock_calls = self._monitor("SYSTEM")
        self.assertEqual(outcome, main.MonitorOutcome.LOCK_REQUESTED)
        self.assertEqual(lock_calls, 1)

    def test_last_moment_input_cancels_application_lock(self):
        outcome, lock_calls = self._monitor("APPLICATION", input_idle=0)
        self.assertEqual(outcome, main.MonitorOutcome.SESSION_LOCKED)
        self.assertEqual(lock_calls, 0)

    def test_unlock_releases_camera_and_resumes_without_wts_lock_wait(self):
        signal = AppLockSignal()
        order = []
        def unlock(stop_event):
            order.append("password")
            signal.complete("unlocked")
            return "unlocked"
        camera = Mock()
        camera.release.side_effect = lambda: order.append("camera_released")
        with ExitStack() as stack:
            stack.enter_context(patch.object(config, "PRESENCE_MODE", "ANY_FACE"))
            stack.enter_context(patch("main.FaceDetector", return_value=Mock(device="CPU")))
            stack.enter_context(patch("main.Camera", return_value=camera))
            ready = stack.enter_context(patch("main.wait_for_session_ready", return_value=False))
            stack.enter_context(patch("main.wait_for_camera_activation", return_value=True))
            stack.enter_context(patch("main.open_camera_when_session_ready", return_value=True))
            stack.enter_context(patch("main.monitor_until_session_pause", side_effect=[
                main.MonitorOutcome.APP_LOCK_REQUESTED, main.MonitorOutcome.STOP_REQUESTED]))
            stack.enter_context(patch.object(signal, "wait", side_effect=unlock))
            self.assertEqual(main.run(app_lock_signal=signal), 0)
            self.assertEqual(order[:2], ["camera_released", "password"])
            self.assertTrue(all(not call.kwargs["require_lock_transition"]
                                for call in ready.call_args_list))


if __name__ == "__main__":
    unittest.main()
