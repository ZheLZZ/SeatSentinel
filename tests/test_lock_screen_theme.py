"""Layout bounds and date rollover for independent monitor surfaces."""

import time
import unittest

from lock_screen_theme import LockScreenLayout, LandscapeTheme, clock_text


class ThemeTests(unittest.TestCase):
    def test_controls_stay_inside_small_portrait_ultrawide_and_4k_displays(self):
        for width, height in ((640, 360), (800, 600), (1920, 1080), (1080, 1920),
                              (3840, 2160), (5120, 1440), (1280, 720)):
            with self.subTest(size=(width, height)):
                layout = LockScreenLayout.for_size(width, height)
                left, top, right, bottom = layout.panel
                self.assertTrue(0 <= left < right <= width)
                self.assertTrue(height * .3 < top < bottom <= height)
                for x, y in ((.1, .39), (.9, .58), (.1, .67), (.9, .88), (.5, .945)):
                    px, py = layout.point(x, y)
                    self.assertTrue(left <= px <= right and top <= py <= bottom)

    def test_midnight_updates_both_date_and_weekday(self):
        before = time.strptime("2026-09-20 23:59", "%Y-%m-%d %H:%M")
        after = time.strptime("2026-09-21 00:00", "%Y-%m-%d %H:%M")
        self.assertEqual(clock_text(before), ("23:59", "9月20日 星期日"))
        self.assertEqual(clock_text(after), ("00:00", "9月21日 星期一"))

    def test_wallpaper_fills_each_display_and_only_primary_has_password_panel(self):
        theme = LandscapeTheme()
        for size in ((640, 360), (540, 960)):
            layout = LockScreenLayout.for_size(*size)
            primary, secondary = theme.render(layout, True), theme.render(layout, False)
            self.assertEqual(primary.size, size)
            self.assertEqual(secondary.size, size)
            self.assertEqual(primary.getpixel((0, 0)), secondary.getpixel((0, 0)))
            self.assertNotEqual(primary.getpixel(layout.point(.5, .775)),
                                secondary.getpixel(layout.point(.5, .775)))


if __name__ == "__main__":
    unittest.main()
