import unittest

from oled_saver import ClockMotion, clock_position, clock_color
from user_settings import AppSettings, SettingsError


class OledSaverTests(unittest.TestCase):
    def test_pixel_measurement_rounding_cannot_amplify_elapsed_time(self):
        motion = ClockMotion(1)
        previous = motion.advance(1920, 1080, 166, 135, 36000)
        for frame in range(1, 1000):
            point = motion.advance(1920, 1080, 166, 135 + frame % 2,
                                   36000 + frame * .05)
            self.assertLessEqual(abs(point[0] - previous[0]), .900001)
            # At a boundary, growing content by one pixel may clamp by 0.5px.
            self.assertLessEqual(abs(point[1] - previous[1]), 1.050001)
            previous = point

    def test_other_screen_bounces_and_resizing_do_not_change_this_screen(self):
        first, second, reference = ClockMotion(0), ClockMotion(1), ClockMotion(1)
        for frame in range(1000):
            elapsed = frame * .1
            first.advance(320 if frame % 2 else 640, 200, 240, 100, elapsed)
            actual = second.advance(2560, 1600, 260, 200, elapsed)
            self.assertEqual(actual, reference.advance(2560, 1600, 260, 200, elapsed))

    def test_stateful_motion_stays_in_bounds_after_long_gaps_and_size_changes(self):
        motion = ClockMotion(2)
        for frame in range(500):
            width, height = ((640, 360), (1080, 1920), (3840, 2160))[frame % 3]
            x, y = motion.advance(width, height, 240, 100, frame * 370)
            self.assertTrue(144 <= x <= width - 144)
            self.assertTrue(74 <= y <= height - 74)

    def test_native_canvas_fractional_scale_has_no_feedback_jitter(self):
        import tkinter as tk
        from unittest.mock import patch
        from app_lock_windows import AppLockWindow
        root = tk.Tk()
        root.withdraw()
        try:
            canvas = tk.Canvas(root)
            canvas.oled_overlay = None
            canvas.oled_scale = .8
            canvas.oled_font = ("Segoe UI Light", -83)
            canvas.oled_date_font = ("Microsoft YaHei UI", -15)
            canvas.surface_size = (1920, 1080)
            with patch("app_lock_windows.clock_text", return_value=("11:28", "9月27日 星期日")):
                AppLockWindow._show_saver_surface(canvas)
                previous = None
                for frame in range(500):
                    AppLockWindow._position_saver_clock(canvas, 36000 + frame * .05, 1)
                    point = canvas.oled_overlay.coords("oled_clock")
                    if previous is not None:
                        self.assertLessEqual(abs(point[0] - previous[0]), .900001)
                        self.assertLessEqual(abs(point[1] - previous[1]), .550001)
                    previous = point
            before = canvas.oled_overlay.motion.position
            with patch("app_lock_windows.clock_text", return_value=("23:59", "12月31日 星期四")):
                AppLockWindow._position_saver_clock(canvas, 36025, 1)
            after = canvas.oled_overlay.motion.position
            self.assertLessEqual(abs(after[0] - before[0]), .900001)
            self.assertLessEqual(abs(after[1] - before[1]), .550001)
        finally:
            root.destroy()

    def test_clock_stays_inside_each_display_over_long_runs(self):
        for width, height in ((640, 360), (1080, 1920), (1920, 1080), (3840, 2160), (5120, 1440)):
            for seconds in range(0, 86400, 37):
                x, y = clock_position(width, height, 240, 100, seconds, 2)
                self.assertTrue(120 <= x <= width - 120)
                self.assertTrue(50 <= y <= height - 50)
            self.assertNotEqual(clock_position(width, height, 240, 100, 0),
                                clock_position(width, height, 240, 100, 20))

    def test_clock_remains_readable_during_slow_tint_changes(self):
        colors = {clock_color(seconds) for seconds in range(240)}
        self.assertGreater(len(colors), 10)
        self.assertTrue(all(220 <= int(color[index:index+2], 16) <= 250
                            for color in colors for index in (1, 3, 5)))

    def test_existing_settings_enable_oled_and_opt_out_round_trips(self):
        self.assertTrue(AppSettings.from_mapping({}).app_lock_oled_protection)
        self.assertFalse(AppSettings.from_mapping({"app_lock_oled_protection": False}).app_lock_oled_protection)
        with self.assertRaises(SettingsError):
            AppSettings.from_mapping({"app_lock_oled_protection": "invalid"})
