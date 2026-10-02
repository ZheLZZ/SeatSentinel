"""Platform failure paths without native hotkeys, windows or user data."""
from __future__ import annotations

import tempfile
import threading
import unittest
import ctypes
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from global_hotkey import GlobalHotkeyError, WindowsGlobalHotkey, WM_STOP_WAKE, _Message
from user_settings import AppSettings, SettingsError, SettingsStore


class HotkeyLifecycleTests(unittest.TestCase):
    def native_api(self):
        user = Mock()
        kernel = Mock()
        finished = threading.Event()
        kernel.GetCurrentThreadId.return_value = 12345
        user.PeekMessageW.return_value = False
        user.RegisterHotKey.return_value = True
        user.UnregisterHotKey.return_value = True
        user.GetMessageW.side_effect = lambda *_: finished.wait(3) and 0
        user.PostThreadMessageW.side_effect = lambda *_: finished.set() or True
        dll = lambda name, **_: user if name == "user32" else kernel
        return user, kernel, finished, dll

    def test_timeout_before_queue_does_not_register_later_and_can_restart(self):
        user, kernel, finished, dll = self.native_api()
        gate = threading.Event()
        kernel.GetCurrentThreadId.side_effect = lambda: gate.wait(3) and 12345
        hotkey = WindowsGlobalHotkey(Mock())
        with patch("global_hotkey.ctypes.WinDLL", side_effect=dll):
            try:
                with self.assertRaises(GlobalHotkeyError):
                    hotkey.start("Alt+B", timeout=0.1)
                old_run = hotkey._run
                self.assertIsNotNone(old_run)
                self.assertTrue(old_run.stop_event.is_set())
                with self.assertRaises(GlobalHotkeyError):
                    hotkey.start("Alt+C", timeout=0.1)
                gate.set()
                old_run.thread.join(1)
                self.assertFalse(old_run.thread.is_alive())
                user.RegisterHotKey.assert_not_called()

                finished.clear()
                hotkey.start("Alt+C")
                new_run = hotkey._run
                self.assertEqual(hotkey.hotkey, "Alt+C")
                # A delayed shutdown for the old generation cannot discard the
                # new registration or send WM_QUIT to its thread.
                hotkey._stop_run(old_run, 0.1)
                self.assertIs(hotkey._run, new_run)
                self.assertFalse(finished.is_set())
            finally:
                gate.set()
                hotkey.stop()
        self.assertIsNone(hotkey.hotkey)
        self.assertEqual(user.RegisterHotKey.call_count, 1)
        self.assertEqual(user.UnregisterHotKey.call_count, 1)

    def test_timeout_during_registration_unregisters_late_success(self):
        user, _kernel, _finished, dll = self.native_api()
        gate = threading.Event()
        user.RegisterHotKey.side_effect = lambda *_: gate.wait(3) or True
        hotkey = WindowsGlobalHotkey(Mock())
        with patch("global_hotkey.ctypes.WinDLL", side_effect=dll):
            try:
                with self.assertRaises(GlobalHotkeyError):
                    hotkey.start("Alt+B", timeout=0.1)
                run = hotkey._run
                gate.set()
                run.thread.join(1)
                self.assertFalse(run.thread.is_alive())
                user.UnregisterHotKey.assert_called_once()
                user.GetMessageW.assert_not_called()
            finally:
                gate.set()
                hotkey.stop()

    def test_dll_startup_failure_is_reported_and_does_not_look_registered(self):
        hotkey = WindowsGlobalHotkey(Mock())
        with patch("global_hotkey.ctypes.WinDLL", side_effect=OSError("synthetic failure")), \
             self.assertLogs("seat_sentinel.hotkey", level="ERROR"):
            with self.assertRaises(GlobalHotkeyError):
                hotkey.start("Alt+B")
        self.assertIsNone(hotkey.hotkey)

    def test_stale_stop_wake_does_not_stop_a_new_generation(self):
        user, _kernel, finished, dll = self.native_api()
        delivered = threading.Event()
        calls = 0
        def get_message(pointer, *_):
            nonlocal calls
            calls += 1
            if calls == 1:
                message = ctypes.cast(pointer, ctypes.POINTER(_Message)).contents
                message.message = WM_STOP_WAKE
                delivered.set()
                return 1
            return finished.wait(3) and 0
        user.GetMessageW.side_effect = get_message
        hotkey = WindowsGlobalHotkey(Mock())
        with patch("global_hotkey.ctypes.WinDLL", side_effect=dll):
            try:
                hotkey.start("Alt+B")
                self.assertTrue(delivered.wait(1))
                self.assertTrue(hotkey._run.thread.is_alive())
                self.assertEqual(hotkey.hotkey, "Alt+B")
            finally:
                hotkey.stop()
        self.assertEqual(user.PostThreadMessageW.call_args.args[1], WM_STOP_WAKE)
        user.UnregisterHotKey.assert_called_once()


class SettingsResilienceTests(unittest.TestCase):
    def test_bad_encoding_and_overflow_are_settings_errors_without_rewriting(self):
        for content in (b'{"camera_name":"\xff"}', b'{"frame_width":1e309}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                path.write_bytes(content)
                with self.assertRaises(SettingsError):
                    SettingsStore(path=path).load()
                self.assertEqual(path.read_bytes(), content)

    def test_bad_legacy_encoding_is_settings_error_and_preserves_both_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            legacy = Path(directory) / "legacy.json"
            content = b'{"camera_name":"\xff"}'
            legacy.write_bytes(content)
            with self.assertRaises(SettingsError):
                SettingsStore(path=path, legacy_path=legacy).load()
            self.assertFalse(path.exists())
            self.assertEqual(legacy.read_bytes(), content)

    def test_conversion_error_does_not_echo_entered_value(self):
        sensitive_value = "synthetic-secret-do-not-display"
        with self.assertRaises(SettingsError) as caught:
            AppSettings.from_mapping({"frame_width": sensitive_value})
        self.assertNotIn(sensitive_value, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_concurrent_saves_publish_whole_files_without_temporary_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            values = [replace(AppSettings.defaults(), camera_name=name) for name in ("Camera A", "Camera B")]
            store = SettingsStore(path=path)
            barrier = threading.Barrier(2)
            errors = []
            import user_settings
            original_fsync = user_settings.os.fsync
            def flush(descriptor):
                original_fsync(descriptor)
                barrier.wait(3)
            def save(value):
                try:
                    store.save(value)
                except Exception as exc:
                    errors.append(exc)
            with patch("user_settings.os.fsync", side_effect=flush):
                threads = [threading.Thread(target=save, args=(value,)) for value in values]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertIn(store.load(), values)
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["settings.json"])

    def test_read_and_publication_share_a_lock_across_store_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            reader_store = SettingsStore(path=path)
            writer_store = SettingsStore(path=path)
            baseline = reader_store.load()  # Missing-file initialization is reentrant.
            updated = replace(baseline, camera_name="Updated Camera")
            read_entered = threading.Event()
            release_read = threading.Event()
            write_prepared = threading.Event()
            replaced = threading.Event()
            values, errors = [], []
            import user_settings
            original_read = Path.read_text
            original_fsync = user_settings.os.fsync
            original_replace = user_settings.os.replace
            def read(current, *args, **kwargs):
                if current == path:
                    read_entered.set()
                    if not release_read.wait(3):
                        raise RuntimeError("Synthetic read gate timed out")
                return original_read(current, *args, **kwargs)
            def flush(descriptor):
                original_fsync(descriptor)
                write_prepared.set()
            def publish(source, destination):
                replaced.set()
                return original_replace(source, destination)
            def run(operation):
                try:
                    values.append(operation())
                except Exception as exc:
                    errors.append(exc)
            with patch.object(Path, "read_text", read), \
                 patch("user_settings.os.fsync", side_effect=flush), \
                 patch("user_settings.os.replace", side_effect=publish):
                reader = threading.Thread(target=run, args=(reader_store.load,))
                writer = threading.Thread(target=run, args=(lambda: writer_store.save(updated),))
                reader.start()
                try:
                    self.assertTrue(read_entered.wait(1))
                    writer.start()
                    self.assertTrue(write_prepared.wait(1))
                    self.assertFalse(replaced.wait(.05), "The writer bypassed an active read")
                finally:
                    release_read.set()
                    reader.join(3)
                    if writer.ident is not None:
                        writer.join(3)
            self.assertEqual(errors, [])
            self.assertIn(baseline, values)
            self.assertTrue(replaced.is_set())
            self.assertEqual(reader_store.load(), updated)

    @unittest.skipUnless(os.name == "nt", "Windows path comparison")
    def test_windows_case_aliases_share_the_same_settings_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = SettingsStore(path=path)
            alias = SettingsStore(path=Path(str(path).upper()))
            self.assertIs(original._io_lock, alias._io_lock)

    @staticmethod
    def windows_file_error(code):
        error = OSError("synthetic Windows file operation failure")
        error.winerror = code
        return error

    def test_replace_retries_only_transient_windows_conflicts_then_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path=path)
            store.save(AppSettings.defaults())
            original_content = path.read_bytes()
            updated = replace(AppSettings.defaults(), camera_name="Updated Camera")
            import user_settings
            original_replace = user_settings.os.replace
            errors = iter((5, 32, 33))
            def publish(source, destination):
                code = next(errors, None)
                if code is not None:
                    self.assertEqual(path.read_bytes(), original_content)
                    raise self.windows_file_error(code)
                return original_replace(source, destination)
            with patch("user_settings.os.replace", side_effect=publish) as publisher, \
                 patch("user_settings.time.sleep") as sleep:
                store.save(updated)
            self.assertEqual(publisher.call_count, 4)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [.01, .02, .04])
            self.assertEqual(store.load(), updated)
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["settings.json"])

    def test_persistent_windows_conflict_is_bounded_and_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path=path)
            store.save(AppSettings.defaults())
            original_content = path.read_bytes()
            with patch("user_settings.os.replace", side_effect=self.windows_file_error(32)) as publisher, \
                 patch("user_settings.time.sleep") as sleep:
                with self.assertRaises(SettingsError):
                    store.save(replace(AppSettings.defaults(), camera_name="Updated Camera"))
            self.assertEqual(publisher.call_count, 5)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [.01, .02, .04, .08])
            self.assertEqual(path.read_bytes(), original_content)
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["settings.json"])

    def test_nonsharing_windows_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path=path)
            store.save(AppSettings.defaults())
            original_content = path.read_bytes()
            with patch("user_settings.os.replace", side_effect=self.windows_file_error(112)) as publisher, \
                 patch("user_settings.time.sleep") as sleep:
                with self.assertRaises(SettingsError):
                    store.save(replace(AppSettings.defaults(), camera_name="Updated Camera"))
            publisher.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(path.read_bytes(), original_content)
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["settings.json"])

    def test_failed_replace_preserves_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path=path)
            store.save(AppSettings.defaults())
            original = path.read_bytes()
            with patch("user_settings.os.replace", side_effect=PermissionError("synthetic busy file")):
                with self.assertRaises(SettingsError):
                    store.save(replace(AppSettings.defaults(), camera_name="New Camera"))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["settings.json"])

    def test_invalid_unicode_save_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path=path)
            store.save(AppSettings.defaults())
            original = path.read_bytes()
            with self.assertRaises(SettingsError):
                store.save(replace(AppSettings.defaults(), camera_name="\ud800"))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["settings.json"])


if __name__ == "__main__":
    unittest.main()
