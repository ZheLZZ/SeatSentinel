"""Normalize a transparent master image and create Windows icon assets."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _normalize(source: Path) -> Image.Image:
    image = Image.open(source).convert("RGBA")
    bounds = image.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("Source image has no visible pixels")
    subject = image.crop(bounds)
    padding = round(max(subject.size) * 0.06)
    side = max(subject.size) + 2 * padding
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(
        subject,
        ((side - subject.width) // 2, (side - subject.height) // 2),
    )
    return square.resize((1024, 1024), Image.Resampling.LANCZOS)


def _create_preview(master: Image.Image, output: Path) -> None:
    preview = Image.new("RGB", (520, 180), (245, 247, 250))
    draw = ImageDraw.Draw(preview)
    draw.rectangle((260, 0, 520, 180), fill=(16, 24, 40))
    draw.text((18, 20), "Light taskbar", fill=(30, 41, 59))
    draw.text((278, 20), "Dark taskbar", fill=(226, 232, 240))
    sizes = (16, 24, 32, 48)
    for start_x in (18, 278):
        offset = 0
        for size in sizes:
            icon = master.resize((size, size), Image.Resampling.LANCZOS)
            position = (start_x + offset, 64 + (48 - size) // 2)
            preview.paste(icon, position, icon)
            offset += size + 34
    preview.save(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--png", type=Path, required=True)
    parser.add_argument("--ico", type=Path, required=True)
    parser.add_argument("--preview", type=Path, required=True)
    arguments = parser.parse_args()

    master = _normalize(arguments.source)
    arguments.png.parent.mkdir(parents=True, exist_ok=True)
    master.save(arguments.png)
    master.save(
        arguments.ico,
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES],
    )
    _create_preview(master, arguments.preview)


if __name__ == "__main__":
    main()
