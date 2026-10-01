"""Moving clock presentation, independent of monitor origin and DPI."""

import math


IDLE_SECONDS = 180.0


class ClockMotion:
    """Per-surface position/direction, advanced only by time since last frame.

    A one-pixel measurement change must not recompute hours of travel against
    a different bounce period. Content resizing only clamps the current point.
    """

    def __init__(self, screen_index=0):
        self.screen_index = screen_index
        self.position = None
        self.direction = (1, 1)
        self.elapsed = None

    @staticmethod
    def _advance_axis(position, direction, length, content, distance):
        margin = min(24, max(0, (length - content) / 2))
        start = margin + content / 2
        span = max(0, length - 2 * start)
        if not span:
            return length / 2, direction
        offset = min(span, max(0, position - start))
        phase = offset if direction > 0 else 2 * span - offset
        phase = (phase + distance) % (2 * span)
        return (start + (phase if phase < span else 2 * span - phase),
                1 if phase < span else -1)

    def advance(self, width, height, text_width, text_height, elapsed):
        if self.position is None:
            self.position = clock_position(width, height, text_width, text_height,
                                           0, self.screen_index)
        delta = max(0, elapsed - self.elapsed) if self.elapsed is not None else 0
        self.elapsed = elapsed
        x, dx = self._advance_axis(self.position[0], self.direction[0],
                                   width, text_width, 18 * delta)
        y, dy = self._advance_axis(self.position[1], self.direction[1],
                                   height, text_height, 11 * delta)
        self.position, self.direction = (x, y), (dx, dy)
        return self.position


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
    # Readable near-white with a slow, subtle cool-to-warm tint.
    phase = (1 + math.sin(elapsed * math.tau / 240)) / 2
    red, green, blue = round(225 + 20 * phase), round(237 + 3 * phase), round(250 - 22 * phase)
    return f"#{red:02x}{green:02x}{blue:02x}"
