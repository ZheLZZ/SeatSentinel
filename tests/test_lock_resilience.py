"""Offline lock recovery tests: no real windows, hooks, input or cameras."""

import contextlib
import ctypes
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import dwm_privacy
from app_lock import UnlockGate, hash_password
from app_lock_windows import AppLockWindow, InputGuard


class PartialMonitorTests(unittest.TestCase):
    def partial_topology(self, *, enum_result=False, error_code=5):
        def enumerate_displays(_dc, _rect, callback, _data):
            self.assertTrue(callback(101, None, None, 0))
            self.assertFalse(callback(102, None, None, 0))
            return enum_result

        def query_monitor(handle, pointer):
            if handle == 102:
                ctypes.set_last_error(error_code)
                return False
            info = ctypes.cast(pointer, ctypes.POINTER(dwm_privacy.MonitorInfo)).contents
            info.rcMonitor = dwm_privacy.wintypes.RECT(0, 0, 1920, 1080)
            info.rcWork = dwm_privacy.wintypes.RECT(0, 0, 1920, 1040)
            info.dwFlags = dwm_privacy.MONITORINFOF_PRIMARY
            return True

        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(dwm_privacy, "_physical_pixel_context", contextlib.nullcontext))
        stack.enter_context(patch.object(dwm_privacy.user32, "EnumDisplayMonitors", enumerate_displays))
        stack.enter_context(patch.object(dwm_privacy.user32, "GetMonitorInfoW", query_monitor))
        return stack

    def test_any_failed_monitor_rejects_the_whole_topology_even_without_error_code(self):
        for enum_result, code in ((False, 5), (True, 0)):
            with self.subTest(enum_result=enum_result, code=code), \
                    self.partial_topology(enum_result=enum_result, error_code=code):
                with self.assertRaisesRegex(dwm_privacy.DwmPrivacyError, "query a monitor"):
                    dwm_privacy.enumerate_monitor_work_areas()

    def test_lock_keeps_all_existing_covers_when_one_monitor_query_fails(self):
        lock = AppLockWindow.__new__(AppLockWindow)
        covers = [Mock(), Mock()]
        lock.windows = covers.copy()
        lock.entry, lock.button, lock._primary_canvas = Mock(), Mock(), Mock()
        lock.guard = Mock(handles=(101, 102))
        lock._canvases = [Mock(), Mock()]
        lock._signature = ("complete topology",)
        lock._last_topology_error = float("-inf")
        with self.partial_topology(), patch("app_lock_windows.tk.Toplevel") as create, \
                self.assertLogs("seat_sentinel.app_lock", level="ERROR"):
            lock._rebuild()
        self.assertEqual(lock.windows, covers)
        self.assertEqual(lock.guard.handles, (101, 102))
        self.assertEqual(lock._signature, ("complete topology",))
        create.assert_not_called()
        for cover in covers:
            cover.destroy.assert_not_called()


class InputRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.secret = "complete test password"
        cls.record = hash_password(cls.secret)

    def setUp(self):
        # Tcl variables exercise real write traces without creating any HWNDs.
        self.interpreter = tk.Tcl()
        self.unlock = Mock()
        self.error = Mock()
        self.lock = AppLockWindow(self.interpreter, self.unlock, self.error)
        self.lock.root = Mock()
        self.lock.guard = InputGuard(direct_input=True)
        self.guard = self.lock.guard
        self.guard._thread = Mock(is_alive=Mock(return_value=True))
        self.lock.gate = UnlockGate(self.record)
        self.lock.gate.attempt = Mock(wraps=self.lock.gate.attempt)
        self.lock.windows = [Mock(), Mock()]
        self.lock.entry = Mock()
        self.lock.button = Mock()
        self.lock._primary_canvas = Mock()
        self.lock._rebuild = Mock()
        self.lock._refresh_clock = Mock()
        self.lock._update_saver = Mock()
        self.lock._handles = Mock(return_value=(123,))
        self.foreground = patch("app_lock_windows.user32.GetForegroundWindow", return_value=123)
        self.foreground.start()
        self.addCleanup(self.foreground.stop)
        self.addCleanup(self.lock.close)

    def overflow(self):
        # The queued prefix would equal the real password if the overflow's
        # trailing text were silently dropped. Use ordinary text/edit events.
        self.guard._emit("text", self.secret[:-1])
        for _ in range((self.guard.events.maxsize - 2) // 2):
            self.guard._emit("text", "x")
            self.guard._emit("backspace")
        self.guard._emit("text", self.secret[-1])
        self.guard._emit("text", "extra input that must not be truncated")

    def finish_verification(self):
        deadline = time.monotonic() + 3
        while self.lock._result is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(self.lock._result)
        self.lock._tick()

    def test_overflow_keeps_covers_and_requires_complete_reentry(self):
        covers = self.lock.windows.copy()
        gate = self.lock.gate
        self.overflow()
        self.guard._emit("submit")
        self.lock._tick()
        self.assertTrue(self.lock.active)
        self.assertIsNone(self.guard._error)
        self.assertEqual(self.lock.password.get(), "")
        self.assertIn("Esc", self.lock.message.get())
        self.lock._submit()  # Clicking unlock cannot verify a truncated value.
        self.guard._emit("text", self.secret)  # Nor can an old burst's suffix.
        self.guard._emit("submit")
        self.lock._tick()
        gate.attempt.assert_not_called()
        self.error.assert_not_called()
        for cover in covers:
            cover.destroy.assert_not_called()
        self.guard._emit("clear")
        self.guard._emit("text", self.secret)
        self.guard._emit("submit")
        self.lock._tick()
        self.finish_verification()
        gate.attempt.assert_called_once_with(self.secret)
        self.unlock.assert_called_once()
        self.assertFalse(self.lock.active)

    def test_mouse_submit_observes_an_overflow_before_the_ui_tick(self):
        self.lock.password.set(self.secret)
        self.overflow()
        self.lock._submit()
        self.lock.gate.attempt.assert_not_called()
        self.assertTrue(self.lock.active)
        self.assertEqual(self.lock.password.get(), "")

    def test_overflow_between_batch_read_and_submit_invalidates_old_batch(self):
        self.guard._emit("text", self.secret)
        self.guard._emit("submit")
        take_events = self.guard.take_events

        def interleaved_batch():
            batch = take_events()
            self.overflow()
            # Even a fast Esc after overflow cannot revive the earlier batch.
            self.guard._emit("clear")
            return batch

        with patch.object(self.guard, "take_events", side_effect=interleaved_batch):
            self.lock._drain_input()
        self.lock.gate.attempt.assert_not_called()
        self.assertEqual(self.lock.password.get(), "")
        self.lock._drain_input()
        self.guard._emit("text", self.secret)
        self.guard._emit("submit")
        self.lock._drain_input()
        self.finish_verification()
        self.unlock.assert_called_once()

    def test_complete_paste_recovers_and_discards_old_pending_input(self):
        self.overflow()
        self.lock.root.clipboard_get.return_value = self.secret
        self.lock._paste_password()
        self.assertEqual(self.lock.password.get(), self.secret)
        self.lock._submit()
        self.finish_verification()
        self.unlock.assert_called_once()

    def test_overflow_preserves_a_pending_wake_and_clears_partial_surrogate(self):
        self.lock._saver_active = True
        self.lock._pending_surrogate = "\ud83d"
        self.guard.saver_active = True
        self.guard._wake_saver()
        self.overflow()
        self.lock._drain_input()
        self.assertFalse(self.lock._saver_active)
        self.assertFalse(self.guard.saver_active)
        self.assertEqual(self.lock._pending_surrogate, "")
        self.assertTrue(self.lock._input_needs_reset)

    def test_batch_updates_password_once_and_preserves_unicode_editing_order(self):
        for _ in range(200):
            self.guard._emit("text", "a")
        self.guard._emit("text", "\ud83d")
        self.guard._emit("text", "\udd11")
        self.guard._emit("backspace")
        self.guard._emit("text", "中")
        self.lock._drain_input()
        self.assertEqual(self.lock.password.get(), "a" * 200 + "中")
        self.lock.entry.configure.assert_called_once()
        self.guard._emit("clear")
        self.guard._emit("text", "\ud83d")
        self.lock._drain_input()
        self.guard._emit("text", "\udd11")
        self.lock._drain_input()
        self.assertEqual(self.lock.password.get(), "🔑")


if __name__ == "__main__":
    unittest.main()
