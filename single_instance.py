"""Windows named-mutex guard for single-instance execution."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional

import config


ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = r"Local\SeatSentinel-87D41D9A"
DEBUG_WINDOW_EVENT_NAME = r"Local\SeatSentinel-ShowDebug-87D41D9A"
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


class SingleInstanceError(RuntimeError):
    """Raised when the Windows mutex cannot be created."""


class SingleInstance:
    """Hold a named Windows mutex for the process lifetime."""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        if os.name != "nt":
            raise SingleInstanceError(
                "Single-instance protection is available only on Windows"
            )

        self._kernel32 = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        )
        self._create_mutex = self._kernel32.CreateMutexW
        self._create_mutex.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._create_mutex.restype = wintypes.HANDLE

        self._close_handle = self._kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

        self._name = name
        self._handle: Optional[int] = None

    def acquire(self) -> bool:
        """Acquire the mutex; return False if another instance owns it."""
        if self._handle is not None:
            return True

        ctypes.set_last_error(0)
        handle = self._create_mutex(None, False, self._name)
        if not handle:
            error_code = ctypes.get_last_error()
            raise SingleInstanceError(
                f"CreateMutexW failed with Windows error {error_code}"
            )

        error_code = ctypes.get_last_error()
        if error_code == ERROR_ALREADY_EXISTS:
            self._close_handle(handle)
            return False

        self._handle = int(handle)
        return True

    def release(self) -> None:
        """Close the owned mutex handle."""
        if self._handle is not None:
            self._close_handle(wintypes.HANDLE(self._handle))
            self._handle = None

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


class DebugWindowSignal:
    """Exchange an open-debug-window request between application instances."""

    def __init__(self, name: str = DEBUG_WINDOW_EVENT_NAME) -> None:
        if os.name != "nt":
            raise SingleInstanceError(
                "Debug-window signaling is available only on Windows"
            )

        self._kernel32 = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        )
        self._create_event = self._kernel32.CreateEventW
        self._create_event.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._create_event.restype = wintypes.HANDLE

        self._wait_for_single_object = (
            self._kernel32.WaitForSingleObject
        )
        self._wait_for_single_object.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        self._wait_for_single_object.restype = wintypes.DWORD

        self._close_handle = self._kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

        self._name = name
        self._handle: Optional[int] = None

    def create(self) -> None:
        """Create the auto-reset event owned by the primary instance."""
        if self._handle is not None:
            return
        handle = self._create_event(
            None,
            False,
            False,
            self._name,
        )
        if not handle:
            error_code = ctypes.get_last_error()
            raise SingleInstanceError(
                f"CreateEventW failed with Windows error {error_code}"
            )
        self._handle = int(handle)

    def consume_request(self) -> bool:
        """Return True once for each pending debug-window request."""
        if self._handle is None:
            return False
        result = self._wait_for_single_object(
            wintypes.HANDLE(self._handle),
            0,
        )
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise SingleInstanceError(
            f"WaitForSingleObject failed with result {result}"
        )

    def close(self) -> None:
        """Close the event handle."""
        if self._handle is not None:
            self._close_handle(wintypes.HANDLE(self._handle))
            self._handle = None


def request_debug_window(
    name: str = DEBUG_WINDOW_EVENT_NAME,
) -> bool:
    """Ask the existing application instance to show its debug window."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    open_event = kernel32.OpenEventW
    open_event.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    open_event.restype = wintypes.HANDLE

    set_event = kernel32.SetEvent
    set_event.argtypes = [wintypes.HANDLE]
    set_event.restype = wintypes.BOOL

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_event(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        return bool(set_event(handle))
    finally:
        close_handle(handle)


def show_already_running_message() -> None:
    """Tell the user that the existing tray process should be used."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    message_box = user32.MessageBoxW
    message_box.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    ]
    message_box.restype = ctypes.c_int
    message_box(
        None,
        f"{config.APPLICATION_TITLE}已经在运行。\n"
        "请使用任务栏通知区域中的蓝色锁形图标。",
        config.APPLICATION_TITLE,
        0x00000040,
    )
