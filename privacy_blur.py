"""Thread-safe privacy-blur signalling and gesture decisions."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PrivacyBlurSnapshot:
    """Immutable privacy-overlay state shared with the Tk thread."""

    active: bool = False
    sequence: int = 0
    detail: str = ""
    activated_at: Optional[float] = None


@dataclass(frozen=True)
class SecondPersonPrivacyDecision:
    """Actions produced by one reliable face-analysis result."""

    activate: bool = False
    auto_dismiss: bool = False


class PrivacyBlurSignal:
    """Safely hand privacy-overlay requests between worker and UI threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = PrivacyBlurSnapshot()

    def snapshot(self) -> PrivacyBlurSnapshot:
        with self._lock:
            return self._snapshot

    def activate(self, detail: str) -> bool:
        """Activate once and return whether this call changed the state."""
        with self._lock:
            if self._snapshot.active:
                return False
            self._snapshot = PrivacyBlurSnapshot(
                active=True,
                sequence=self._snapshot.sequence + 1,
                detail=detail,
                activated_at=time.monotonic(),
            )
            return True

    def dismiss(self) -> bool:
        """Dismiss an active overlay and return whether it was active."""
        with self._lock:
            if not self._snapshot.active:
                return False
            self._snapshot = PrivacyBlurSnapshot(
                active=False,
                sequence=self._snapshot.sequence + 1,
                detail="",
                activated_at=None,
            )
            return True

    def clear(self) -> None:
        """Ensure no overlay remains active during pause or shutdown."""
        self.dismiss()


class SecondPersonPrivacyGuard:
    """Debounce a self-plus-second-person episode into one blur request."""

    def __init__(
        self,
        confirmation_frames: int = 2,
        rearm_clear_seconds: float = 60.0,
        auto_dismiss_owner_alone_seconds: float = 3.0,
    ) -> None:
        if confirmation_frames < 1:
            raise ValueError("Confirmation frames must be positive")
        if rearm_clear_seconds <= 0:
            raise ValueError("Rearm clear time must be positive")
        if auto_dismiss_owner_alone_seconds <= 0:
            raise ValueError("Auto-dismiss time must be positive")
        if auto_dismiss_owner_alone_seconds >= rearm_clear_seconds:
            raise ValueError("Auto-dismiss time must precede rearm time")
        self._confirmation_frames = confirmation_frames
        self._rearm_clear_seconds = rearm_clear_seconds
        self._auto_dismiss_owner_alone_seconds = (
            auto_dismiss_owner_alone_seconds
        )
        self._second_person_streak = 0
        self._clear_started_at: Optional[float] = None
        self._owner_alone_started_at: Optional[float] = None
        self._triggered_for_episode = False
        self._auto_dismissed_for_episode = False

    def update(
        self,
        owner_confirmed: bool,
        face_count: int,
        timestamp: Optional[float] = None,
    ) -> bool:
        """Return True once per confirmed second-person episode."""
        return self.evaluate(
            owner_confirmed,
            face_count,
            timestamp,
        ).activate

    def evaluate(
        self,
        owner_confirmed: bool,
        face_count: int,
        timestamp: Optional[float] = None,
    ) -> SecondPersonPrivacyDecision:
        """Return activation and safe automatic-dismiss decisions."""
        if face_count < 0:
            raise ValueError("Face count cannot be negative")
        now = time.monotonic() if timestamp is None else timestamp
        second_person_present = owner_confirmed and face_count >= 2
        if second_person_present:
            self._clear_started_at = None
            self._owner_alone_started_at = None
            self._second_person_streak += 1
            if (
                not self._triggered_for_episode
                and self._second_person_streak
                >= self._confirmation_frames
            ):
                self._triggered_for_episode = True
                self._auto_dismissed_for_episode = False
                return SecondPersonPrivacyDecision(activate=True)
            return SecondPersonPrivacyDecision()

        self._second_person_streak = 0
        auto_dismiss = False
        if self._triggered_for_episode:
            owner_alone = owner_confirmed and face_count == 1
            if owner_alone:
                if self._owner_alone_started_at is None:
                    self._owner_alone_started_at = now
                elif (
                    not self._auto_dismissed_for_episode
                    and now - self._owner_alone_started_at
                    >= self._auto_dismiss_owner_alone_seconds
                ):
                    auto_dismiss = True
                    self._auto_dismissed_for_episode = True
            else:
                self._owner_alone_started_at = None

            if face_count < 2:
                if self._clear_started_at is None:
                    self._clear_started_at = now
                elif (
                    now - self._clear_started_at
                    >= self._rearm_clear_seconds
                ):
                    self._triggered_for_episode = False
                    self._clear_started_at = None
                    self._owner_alone_started_at = None
                    self._auto_dismissed_for_episode = False
            else:
                self._clear_started_at = None
        else:
            self._clear_started_at = None
            self._owner_alone_started_at = None
        return SecondPersonPrivacyDecision(auto_dismiss=auto_dismiss)

    def mark_visual_state_unknown(self) -> None:
        """Break a clear-period timer when no trustworthy frame exists."""
        self._second_person_streak = 0
        self._clear_started_at = None
        self._owner_alone_started_at = None


class MouseShakeDetector:
    """Recognize a quick, intentional back-and-forth cursor gesture."""

    def __init__(
        self,
        window_seconds: float = 0.9,
        minimum_path_pixels: float = 560.0,
        minimum_span_pixels: float = 140.0,
        minimum_reversals: int = 3,
        minimum_step_pixels: float = 8.0,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("Gesture window must be positive")
        if minimum_path_pixels <= 0 or minimum_span_pixels <= 0:
            raise ValueError("Gesture distance thresholds must be positive")
        if minimum_reversals < 1 or minimum_step_pixels <= 0:
            raise ValueError("Gesture movement thresholds must be positive")
        self._window_seconds = window_seconds
        self._minimum_path_pixels = minimum_path_pixels
        self._minimum_span_pixels = minimum_span_pixels
        self._minimum_reversals = minimum_reversals
        self._minimum_step_pixels = minimum_step_pixels
        self._points: deque[tuple[float, int, int]] = deque()

    def reset(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        self._points.clear()
        if x is not None and y is not None:
            self._points.append(
                (
                    time.monotonic() if timestamp is None else timestamp,
                    x,
                    y,
                )
            )

    def record(
        self,
        x: int,
        y: int,
        timestamp: Optional[float] = None,
    ) -> bool:
        """Record one cursor sample and return whether a shake completed."""
        now = time.monotonic() if timestamp is None else timestamp
        if self._points and now < self._points[-1][0]:
            self.reset()

        if self._points:
            _, previous_x, previous_y = self._points[-1]
            step = math.hypot(x - previous_x, y - previous_y)
            if step < self._minimum_step_pixels:
                self._prune(now)
                return False

        self._points.append((now, x, y))
        self._prune(now)
        if len(self._points) < self._minimum_reversals + 2:
            return False

        points = tuple(self._points)
        segments = [
            (current[1] - previous[1], current[2] - previous[2])
            for previous, current in zip(points, points[1:])
        ]
        path_length = sum(math.hypot(dx, dy) for dx, dy in segments)
        x_span = max(point[1] for point in points) - min(
            point[1] for point in points
        )
        y_span = max(point[2] for point in points) - min(
            point[2] for point in points
        )
        use_x_axis = sum(abs(dx) for dx, _ in segments) >= sum(
            abs(dy) for _, dy in segments
        )
        axis_steps = [
            dx if use_x_axis else dy for dx, dy in segments
        ]
        directions = [
            1 if step > 0 else -1
            for step in axis_steps
            if abs(step) >= self._minimum_step_pixels
        ]
        reversals = sum(
            previous != current
            for previous, current in zip(directions, directions[1:])
        )

        if (
            path_length >= self._minimum_path_pixels
            and max(x_span, y_span) >= self._minimum_span_pixels
            and reversals >= self._minimum_reversals
        ):
            self.reset(x, y, now)
            return True
        return False

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()
