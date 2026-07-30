"""Small, checked wrapper around the Windows LockWorkStation API."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class WindowsLockError(RuntimeError):
    """Raised when Windows rejects a workstation lock request."""


def lock_workstation() -> None:
    """Lock the current Windows workstation or raise a checked error."""
    if os.name != "nt":
        raise WindowsLockError(
            "LockWorkStation is available only on Windows"
        )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    lock_function = user32.LockWorkStation
    lock_function.argtypes = []
    lock_function.restype = wintypes.BOOL

    if not lock_function():
        error_code = ctypes.get_last_error()
        raise WindowsLockError(
            f"LockWorkStation failed with Windows error {error_code}"
        )
