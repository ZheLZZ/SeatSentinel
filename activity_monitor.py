"""Read Windows keyboard and mouse idle time."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class ActivityMonitorError(RuntimeError):
    """Raised when Windows cannot provide last-input information."""


class LASTINPUTINFO(ctypes.Structure):
    """ctypes equivalent of the Win32 LASTINPUTINFO structure."""

    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


class ActivityMonitor:
    """Measure elapsed seconds since the last keyboard or mouse input."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise ActivityMonitorError(
                "GetLastInputInfo is available only on Windows"
            )

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._get_last_input_info = self._user32.GetLastInputInfo
        self._get_last_input_info.argtypes = [
            ctypes.POINTER(LASTINPUTINFO)
        ]
        self._get_last_input_info.restype = wintypes.BOOL

        self._get_tick_count64 = self._kernel32.GetTickCount64
        self._get_tick_count64.argtypes = []
        self._get_tick_count64.restype = ctypes.c_ulonglong

    def seconds_since_last_input(self) -> float:
        """Return system-wide input idle time in seconds."""
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)

        if not self._get_last_input_info(ctypes.byref(info)):
            error_code = ctypes.get_last_error()
            raise ActivityMonitorError(
                "GetLastInputInfo failed with Windows error "
                f"{error_code}"
            )

        # LASTINPUTINFO.dwTime is a 32-bit millisecond tick count. Mask the
        # current 64-bit tick count and subtract modulo 2^32 to handle wrap.
        current_tick_32 = self._get_tick_count64() & 0xFFFFFFFF
        elapsed_milliseconds = (
            current_tick_32 - int(info.dwTime)
        ) & 0xFFFFFFFF
        return elapsed_milliseconds / 1000.0
