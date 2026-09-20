"""Resolution-independent layout and local wallpaper for the landscape theme."""

from dataclasses import dataclass
import time

from PIL import Image, ImageDraw, ImageFilter, ImageOps

import config


@dataclass(frozen=True)
class LockScreenLayout:
    width: int
    height: int
    scale: float
    panel: tuple[int, int, int, int]

    @classmethod
    def for_size(cls, width: int, height: int):
        scale = max(0.5, min(width / 1672, height / 941, 2.5))
        margin = min(24, width // 10, height // 10)
        panel_width = min(round(516 * scale), width - margin * 2)
        panel_height = min(round(300 * scale), round(height * 0.49))
        left = (width - panel_width) // 2
        top = min(height - margin - panel_height, round(height * 0.72 - panel_height / 2))
        return cls(width, height, scale,
                   (left, top, left + panel_width, top + panel_height))

    def point(self, x: float, y: float) -> tuple[int, int]:
        left, top, right, bottom = self.panel
        return round(left + (right - left) * x), round(top + (bottom - top) * y)


def clock_text(now=None) -> tuple[str, str]:
    now = time.localtime() if now is None else now
    weekday = "一二三四五六日"[now.tm_wday]
    return time.strftime("%H:%M", now), f"{now.tm_mon}月{now.tm_mday}日 星期{weekday}"


class LandscapeTheme:
    def __init__(self):
        with Image.open(config.LOCK_SCREEN_BACKGROUND_PATH) as source:
            self.wallpaper = source.convert("RGB")

    def render(self, layout: LockScreenLayout, primary: bool) -> Image.Image:
        """Compose the UI panel over the app wallpaper, never over desktop pixels."""
        background = ImageOps.fit(self.wallpaper, (layout.width, layout.height),
                                  method=Image.Resampling.LANCZOS)
        if not primary:
            return background
        left, top, right, bottom = layout.panel
        panel_size = (right - left, bottom - top)
        blurred = background.crop(layout.panel).filter(ImageFilter.GaussianBlur(12 * layout.scale))
        tint = Image.new("RGB", panel_size, "#263e4b")
        glass = Image.blend(blurred, tint, 0.48)
        mask = Image.new("L", panel_size)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, panel_size[0]-1, panel_size[1]-1),
                                               radius=22 * layout.scale, fill=255)
        background.paste(glass, (left, top), mask)
        draw = ImageDraw.Draw(background)
        for y1, y2, fill, outline in ((0.39, 0.58, "#718792", "#a7b9c1"),
                                     (0.67, 0.88, "#4c91a0", None)):
            draw.rounded_rectangle((*layout.point(0.10, y1), *layout.point(0.90, y2)),
                                   radius=8 * layout.scale, fill=fill, outline=outline)
        return background
