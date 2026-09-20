import unittest

from oled_saver import clock_position, clock_color
from user_settings import AppSettings, SettingsError


class OledSaverTests(unittest.TestCase):
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
