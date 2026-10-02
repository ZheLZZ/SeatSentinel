"""Focus-independent lock input through the actual hook callback."""
import ctypes
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from app_lock_windows import InputGuard, KeyboardData


class DirectInputTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.callbacks = {}
        def install(kind, callback, *_):
            self.callbacks[kind] = callback
            return kind
        for name, result in (("UnhookWindowsHookEx", True), ("PeekMessageW", False),
                             ("GetForegroundWindow", 999), ("GetAsyncKeyState", 0),
                             ("GetKeyState", 0), ("CallNextHookEx", 0)):
            self.stack.enter_context(patch("app_lock_windows.user32." + name, return_value=result))
        self.stack.enter_context(patch("app_lock_windows.user32.SetWindowsHookExW", side_effect=install))
        self.guard = InputGuard(direct_input=True)
        self.guard.handles = (123,)
        self.guard.start()
        self.addCleanup(self.guard.close)

    def key(self, vk, *, down=True, flags=0, scan=0):
        key = KeyboardData(vkCode=vk, flags=flags, scanCode=scan)
        return self.callbacks[13](0, 0x100 if down else 0x101, ctypes.addressof(key))

    def events(self):
        result = []
        while not self.guard.events.empty():
            result.append(self.guard.events.get_nowait())
        return result

    def test_foreign_focus_and_lost_alt_ctrl_release_do_not_swallow_password(self):
        self.key(0xA4, flags=0x10)
        self.key(0xA2, flags=0x10)
        # Unicode remote packets and physical characters take the same queue.
        self.assertEqual(self.key(0xE7, flags=0x10, scan=ord("中")), 1)
        def translate(vk, scan, state, buffer, *_):
            self.assertEqual(state[0x11], 0)
            self.assertEqual(state[0x12], 0)
            buffer.value = "8"
            return 1
        with patch("app_lock_windows.user32.ToUnicodeEx", side_effect=translate):
            self.assertEqual(self.key(0x38), 1)
        self.assertEqual(self.key(0x0D), 1)
        self.assertEqual(self.events(), [("text", "中"), ("text", "8"), ("submit", "")])

    def test_wake_enter_repeat_cannot_unlock_and_physical_takeover_recovers(self):
        self.guard.saver_active = True
        self.key(0x0D, flags=0x10)
        self.guard.saver_active = False
        self.key(0x0D, flags=0x10)
        self.assertEqual(self.events(), [("wake", "")])
        self.key(0x0D)  # physical input after remote key-up was lost
        self.assertEqual(self.events(), [("submit", "")])

    def test_clock_space_only_wakes_and_held_repeats_never_enter_password(self):
        def translate(vk, scan, state, buffer, *_):
            buffer.value = " "
            return 1
        with patch("app_lock_windows.user32.ToUnicodeEx", side_effect=translate) as translation:
            self.guard.saver_active = True
            self.key(0x20)
            for _ in range(5):
                self.key(0x20)
            self.key(0x20, down=False)
            translation.assert_not_called()
            self.assertEqual(self.events(), [("wake", "")])
            # A separate press in password mode remains a valid password space.
            self.key(0x20)
            self.assertEqual(self.events(), [("text", " ")])

    def test_password_keys_immediately_after_wake_are_not_lost_before_ui_tick(self):
        self.guard.saver_active = True
        self.key(0x20)
        self.key(0x20, down=False)
        # The UI has not yet read the wake event or updated the clock surface.
        self.key(0xE7, scan=ord("中"))
        self.key(0xE7, down=False)
        self.key(0x0D)
        self.assertEqual(self.events(), [("wake", ""), ("text", "中"), ("submit", "")])

    def test_clock_enter_backspace_escape_or_letter_only_wake(self):
        for vk in (0x0D, 0x08, 0x1B, 0x41):
            with self.subTest(vk=vk):
                self.guard.saver_active = True
                self.key(vk)
                self.key(vk, down=False)
                self.assertEqual(self.events(), [("wake", "")])

    def test_pointer_noise_does_not_wake_but_real_movement_and_click_do(self):
        point = ctypes.wintypes.POINT(10, 10)
        self.callbacks[14](0, 0x0200, ctypes.addressof(point))
        self.guard.saver_active = True
        self.callbacks[14](0, 0x0200, ctypes.addressof(point))
        self.assertEqual(self.events(), [])
        point.x += 10
        self.callbacks[14](0, 0x0200, ctypes.addressof(point))
        self.assertEqual(self.events(), [("wake", "")])
        self.guard.saver_active = True
        self.assertEqual(self.callbacks[14](0, 0x0201, ctypes.addressof(point)), 1)
        self.assertEqual(self.callbacks[14](0, 0x0202, ctypes.addressof(point)), 1)
        self.assertEqual(self.events(), [("wake", "")])

    def test_edit_controls_and_no_duplicate_keyup(self):
        for vk in (0x08, 0x1B, 0x0D):
            self.key(vk)
            self.key(vk, down=False)
        self.assertEqual(self.events(), [("backspace", ""), ("clear", ""), ("submit", "")])

    def test_verification_drops_input_and_close_clears_queue(self):
        self.guard.accept_input = False
        self.key(0xE7, scan=ord("x"))
        self.assertEqual(self.events(), [])
        self.guard.accept_input = True
        self.key(0xE7, scan=ord("x"))
        self.guard.close()
        self.assertEqual(self.events(), [])

    def test_shift_and_caps_translation_and_system_shortcut_blocking(self):
        self.key(0x14)
        self.key(0x14)  # autorepeat must not toggle twice
        def translate(vk, scan, state, buffer, *_):
            self.assertEqual(state[0x10], 0x80)
            self.assertEqual(state[0x14], 1)
            buffer.value = "!"
            return 1
        with patch("app_lock_windows.user32.GetAsyncKeyState", return_value=0x8000), \
             patch("app_lock_windows.user32.ToUnicodeEx", side_effect=translate):
            self.key(0x31)
            for vk in (0x5B, 0x09, 0x73):
                self.assertEqual(self.key(vk), 1)
        self.assertEqual(self.events(), [("text", "!")])


if __name__ == "__main__":
    unittest.main()
