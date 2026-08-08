"""Thread-safe sedentary-reminder signalling and presence tracking."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SedentaryReminderSnapshot:
    """Immutable reminder event shared with the Tkinter thread."""

    sequence: int = 0
    seated_seconds: float = 0.0
    detail: str = ""
    triggered_at: Optional[float] = None


class SedentaryReminderSignal:
    """Safely hand one-shot reminder events from monitoring to the UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = SedentaryReminderSnapshot()

    def snapshot(self) -> SedentaryReminderSnapshot:
        with self._lock:
            return self._snapshot

    def trigger(
        self,
        seated_seconds: float,
        detail: str,
        timestamp: Optional[float] = None,
    ) -> None:
        if seated_seconds < 0:
            raise ValueError("Seated time cannot be negative")
        with self._lock:
            self._snapshot = SedentaryReminderSnapshot(
                sequence=self._snapshot.sequence + 1,
                seated_seconds=float(seated_seconds),
                detail=str(detail),
                triggered_at=(
                    time.monotonic() if timestamp is None else timestamp
                ),
            )

    def clear(self) -> None:
        """Discard any pending reminder event."""
        with self._lock:
            if self._snapshot.triggered_at is None:
                return
            self._snapshot = SedentaryReminderSnapshot(
                sequence=self._snapshot.sequence + 1,
            )


class SedentaryTracker:
    """Emit one reminder per interval until a confirmed leave resets time."""

    def __init__(
        self,
        reminder_interval_seconds: float,
        leave_confirmation_seconds: float,
    ) -> None:
        if reminder_interval_seconds <= 0:
            raise ValueError("Reminder interval must be positive")
        if leave_confirmation_seconds <= 0:
            raise ValueError("Leave confirmation time must be positive")
        self._reminder_interval_seconds = reminder_interval_seconds
        self._leave_confirmation_seconds = leave_confirmation_seconds
        self._seated_started_at: Optional[float] = None
        self._not_present_started_at: Optional[float] = None
        self._last_observed_at: Optional[float] = None
        self._next_reminder_seconds = reminder_interval_seconds

    def reset(self) -> None:
        self._seated_started_at = None
        self._not_present_started_at = None
        self._last_observed_at = None
        self._next_reminder_seconds = self._reminder_interval_seconds

    def observe(
        self,
        present: Optional[bool],
        timestamp: Optional[float] = None,
    ) -> Optional[float]:
        """Observe presence and return seated seconds when a reminder is due.

        ``None`` is an uncertain visual/input state.  It cannot start a seated
        period or produce a reminder, and a prolonged uncertain/absent period
        resets the timer just like a confirmed departure.
        """
        now = time.monotonic() if timestamp is None else timestamp
        if self._last_observed_at is not None and now < self._last_observed_at:
            self.reset()
        self._last_observed_at = now

        if present is True:
            if self._seated_started_at is None:
                self._seated_started_at = now
                self._next_reminder_seconds = (
                    self._reminder_interval_seconds
                )
            self._not_present_started_at = None
        else:
            if self._seated_started_at is None:
                return None
            if self._not_present_started_at is None:
                self._not_present_started_at = now
            elif (
                now - self._not_present_started_at
                >= self._leave_confirmation_seconds
            ):
                self.reset()
            return None

        assert self._seated_started_at is not None
        seated_seconds = max(0.0, now - self._seated_started_at)
        if seated_seconds < self._next_reminder_seconds:
            return None

        completed_intervals = max(
            1,
            math.floor(
                seated_seconds / self._reminder_interval_seconds
            ),
        )
        self._next_reminder_seconds = (
            completed_intervals + 1
        ) * self._reminder_interval_seconds
        return seated_seconds


def format_sedentary_duration(seconds: float) -> str:
    """Format an elapsed duration for the short Chinese reminder."""
    if seconds < 0:
        raise ValueError("Duration cannot be negative")
    total_minutes = max(1, int(seconds // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"
