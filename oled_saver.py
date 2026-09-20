"""Low-brightness clock motion, independent of monitor origin and DPI."""

import math


IDLE_SECONDS = 60.0


def clock_position(width, height, text_width, text_height, elapsed, screen_index=0):
    """Bounce within the display, including portrait and small surfaces."""
    def axis(length, content, speed, phase):
        margin = min(24, max(0, (length - content) / 2))
        start = margin + content / 2
        span = max(0, length - 2 * start)
        if span == 0:
            return length / 2
        travel = (max(0, elapsed) * speed + span * phase) % (2 * span)
        return start + (travel if travel <= span else 2 * span - travel)

    return (axis(width, text_width, 18, (0.31 + screen_index * 0.23) % 1),
            axis(height, text_height, 11, (0.27 + screen_index * 0.19) % 1))


def clock_color(elapsed):
    level = round(48 + 12 * math.sin(elapsed * math.tau / 120))
    return f"#{level:02x}{level:02x}{level:02x}"
