"""Query the current Windows session lock state."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


WTS_CURRENT_SERVER_HANDLE = wintypes.HANDLE(0)
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_SESSION_INFO_EX = 25
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1

WINSTATIONNAME_LENGTH = 32
USERNAME_LENGTH = 20
DOMAIN_LENGTH = 17


class SessionMonitorError(RuntimeError):
    """Raised when the current Windows session state is unavailable."""


class WTSINFOEX_LEVEL1_W(ctypes.Structure):
    """ctypes equivalent of WTSINFOEX_LEVEL1_W."""

    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        (
            "WinStationName",
            wintypes.WCHAR * (WINSTATIONNAME_LENGTH + 1),
        ),
        ("UserName", wintypes.WCHAR * (USERNAME_LENGTH + 1)),
        ("DomainName", wintypes.WCHAR * (DOMAIN_LENGTH + 1)),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class WTSINFOEX_LEVEL_W(ctypes.Union):
    """ctypes equivalent of the WTSINFOEX_LEVEL_W union."""

    _fields_ = [("WTSInfoExLevel1", WTSINFOEX_LEVEL1_W)]


class WTSINFOEXW(ctypes.Structure):
    """ctypes equivalent of WTSINFOEXW."""

    _fields_ = [
        ("Level", wintypes.DWORD),
        ("Data", WTSINFOEX_LEVEL_W),
    ]


class SessionMonitor:
    """Read whether the current Windows user session is locked."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SessionMonitorError(
                "Windows session monitoring is available only on Windows"
            )

        self._wtsapi32 = ctypes.WinDLL(
            "wtsapi32",
            use_last_error=True,
        )
        self._query_session_information = (
            self._wtsapi32.WTSQuerySessionInformationW
        )
        self._query_session_information.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._query_session_information.restype = wintypes.BOOL

        self._free_memory = self._wtsapi32.WTSFreeMemory
        self._free_memory.argtypes = [ctypes.c_void_p]
        self._free_memory.restype = None

    def is_locked(self) -> bool:
        """Return True for locked and False for unlocked."""
        buffer = ctypes.c_void_p()
        bytes_returned = wintypes.DWORD()

        succeeded = self._query_session_information(
            WTS_CURRENT_SERVER_HANDLE,
            WTS_CURRENT_SESSION,
            WTS_SESSION_INFO_EX,
            ctypes.byref(buffer),
            ctypes.byref(bytes_returned),
        )
        if not succeeded:
            error_code = ctypes.get_last_error()
            raise SessionMonitorError(
                "WTSQuerySessionInformationW failed with Windows error "
                f"{error_code}"
            )

        try:
            if not buffer.value:
                raise SessionMonitorError(
                    "Windows returned an empty session information buffer"
                )
            if bytes_returned.value < ctypes.sizeof(WTSINFOEXW):
                raise SessionMonitorError(
                    "Windows returned an incomplete session information "
                    f"buffer ({bytes_returned.value} bytes)"
                )

            info = ctypes.cast(
                buffer,
                ctypes.POINTER(WTSINFOEXW),
            ).contents
            if info.Level != 1:
                raise SessionMonitorError(
                    f"Unsupported WTSINFOEX level: {info.Level}"
                )

            session_flags = int(
                info.Data.WTSInfoExLevel1.SessionFlags
            )
            if session_flags == WTS_SESSIONSTATE_LOCK:
                return True
            if session_flags == WTS_SESSIONSTATE_UNLOCK:
                return False
            raise SessionMonitorError(
                f"Unknown Windows session flag: {session_flags}"
            )
        finally:
            if buffer.value:
                self._free_memory(buffer)
