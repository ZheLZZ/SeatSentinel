"""Windows global-hotkey parsing and registration for SeatSentinel."""

from __future__ import annotations

import ctypes
import logging
import threading
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Optional


LOGGER = logging.getLogger("seat_sentinel.hotkey")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
HOTKEY_ID = 0x5345
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODIFIER_ALIASES = {
    "ALT": ("Alt", MOD_ALT),
    "CTRL": ("Ctrl", MOD_CONTROL),
    "CONTROL": ("Ctrl", MOD_CONTROL),
    "SHIFT": ("Shift", MOD_SHIFT),
    "WIN": ("Win", MOD_WIN),
    "WINDOWS": ("Win", MOD_WIN),
    "SUPER": ("Win", MOD_WIN),
}
_MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")
_TK_MODIFIER_KEYSYMS = {
    "CONTROL_L": "Ctrl",
    "CONTROL_R": "Ctrl",
    "ALT_L": "Alt",
    "ALT_R": "Alt",
    "META_L": "Win",
    "META_R": "Win",
    "SUPER_L": "Win",
    "SUPER_R": "Win",
    "WIN_L": "Win",
    "WIN_R": "Win",
    "SHIFT_L": "Shift",
    "SHIFT_R": "Shift",
}
_TK_STATE_MODIFIERS = (
    (0x0004, "Ctrl"),
    (0x0008, "Alt"),
    (0x0001, "Shift"),
    (0x0040, "Win"),
)
_TK_NAMED_KEYSYMS = {
    "SPACE": "Space",
    "TAB": "Tab",
    "RETURN": "Enter",
    "KP_ENTER": "Enter",
    "ESCAPE": "Esc",
    "BACKSPACE": "Backspace",
    "INSERT": "Insert",
    "DELETE": "Delete",
    "HOME": "Home",
    "END": "End",
    "PRIOR": "PageUp",
    "NEXT": "PageDown",
    "LEFT": "Left",
    "UP": "Up",
    "RIGHT": "Right",
    "DOWN": "Down",
}
_NAMED_KEYS = {
    "SPACE": ("Space", 0x20),
    "TAB": ("Tab", 0x09),
    "ENTER": ("Enter", 0x0D),
    "RETURN": ("Enter", 0x0D),
    "ESC": ("Esc", 0x1B),
    "ESCAPE": ("Esc", 0x1B),
    "BACKSPACE": ("Backspace", 0x08),
    "INSERT": ("Insert", 0x2D),
    "DELETE": ("Delete", 0x2E),
    "HOME": ("Home", 0x24),
    "END": ("End", 0x23),
    "PAGEUP": ("PageUp", 0x21),
    "PAGEDOWN": ("PageDown", 0x22),
    "LEFT": ("Left", 0x25),
    "UP": ("Up", 0x26),
    "RIGHT": ("Right", 0x27),
    "DOWN": ("Down", 0x28),
}


class GlobalHotkeyError(RuntimeError):
    """Raised when a Windows global hotkey cannot be registered."""


@dataclass(frozen=True)
class HotkeySpec:
    """A normalized global-hotkey chord and its Win32 values."""

    display: str
    modifiers: int
    virtual_key: int


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _Point),
        ("lPrivate", wintypes.DWORD),
    ]


def parse_hotkey(value: str) -> HotkeySpec:
    """Parse a user-facing chord such as ``Alt+B``."""

    raw = str(value).strip().replace("＋", "+")
    parts = [part.strip() for part in raw.split("+")]
    if not raw or any(not part for part in parts):
        raise ValueError("快捷键格式应类似 Alt+B")

    modifiers: dict[str, int] = {}
    key_name: Optional[str] = None
    virtual_key: Optional[int] = None
    for part in parts:
        normalized = part.upper().replace(" ", "")
        modifier = _MODIFIER_ALIASES.get(normalized)
        if modifier is not None:
            name, bit = modifier
            if name in modifiers:
                raise ValueError(f"快捷键中重复使用了 {name}")
            modifiers[name] = bit
            continue

        if key_name is not None:
            raise ValueError("快捷键只能包含一个普通按键")
        if len(normalized) == 1 and (
            "A" <= normalized <= "Z" or "0" <= normalized <= "9"
        ):
            key_name = normalized
            virtual_key = ord(normalized)
            continue
        if (
            normalized.startswith("F")
            and normalized[1:].isdigit()
            and 1 <= int(normalized[1:]) <= 24
        ):
            function_number = int(normalized[1:])
            key_name = f"F{function_number}"
            virtual_key = 0x70 + function_number - 1
            continue
        named_key = _NAMED_KEYS.get(normalized)
        if named_key is not None:
            key_name, virtual_key = named_key
            continue
        raise ValueError(
            "普通按键仅支持字母、数字、F1-F24 或常用功能键"
        )

    if not modifiers:
        raise ValueError("快捷键至少需要 Ctrl、Alt、Shift 或 Win 之一")
    if key_name is None or virtual_key is None:
        raise ValueError("快捷键缺少普通按键")

    ordered_modifiers = [
        name for name in _MODIFIER_ORDER if name in modifiers
    ]
    modifier_mask = 0
    for modifier_name in ordered_modifiers:
        modifier_mask |= modifiers[modifier_name]
    return HotkeySpec(
        display="+".join((*ordered_modifiers, key_name)),
        modifiers=modifier_mask,
        virtual_key=virtual_key,
    )


def normalize_hotkey(value: str) -> str:
    """Return the stable display form stored in settings."""

    return parse_hotkey(value).display


def modifier_name_from_tk_keysym(keysym: str) -> Optional[str]:
    """Return the canonical modifier represented by a Tk key symbol."""
    return _TK_MODIFIER_KEYSYMS.get(str(keysym).strip().upper())


def modifier_names_from_tk_state(state: int) -> tuple[str, ...]:
    """Decode the Windows/Tk modifier mask in stable display order."""
    active = {
        name for mask, name in _TK_STATE_MODIFIERS if int(state) & mask
    }
    return tuple(name for name in _MODIFIER_ORDER if name in active)


def hotkey_from_tk_key_event(
    keysym: str,
    keycode: int,
    state: int,
    pressed_modifiers: tuple[str, ...] = (),
) -> str:
    """Build a normalized hotkey from one non-modifier Tk key event."""
    normalized_keysym = str(keysym).strip().upper()
    key_name: Optional[str] = None

    # On Windows, Tk's keycode is the virtual-key value. Prefer it for
    # letters and digits so Shift+1 records Shift+1 instead of ``exclam``.
    numeric_keycode = int(keycode)
    if 0x30 <= numeric_keycode <= 0x39:
        key_name = chr(numeric_keycode)
    elif 0x41 <= numeric_keycode <= 0x5A:
        key_name = chr(numeric_keycode)
    elif len(normalized_keysym) == 1 and (
        "A" <= normalized_keysym <= "Z"
        or "0" <= normalized_keysym <= "9"
    ):
        key_name = normalized_keysym
    elif (
        normalized_keysym.startswith("F")
        and normalized_keysym[1:].isdigit()
        and 1 <= int(normalized_keysym[1:]) <= 24
    ):
        key_name = normalized_keysym
    else:
        key_name = _TK_NAMED_KEYSYMS.get(normalized_keysym)

    if key_name is None:
        raise ValueError(
            "暂不支持该按键；请选择字母、单个数字、F1-F24 或常用功能键"
        )

    active_modifiers = set(modifier_names_from_tk_state(state))
    active_modifiers.update(pressed_modifiers)
    ordered_modifiers = [
        name for name in _MODIFIER_ORDER if name in active_modifiers
    ]
    return normalize_hotkey("+".join((*ordered_modifiers, key_name)))


class WindowsGlobalHotkey:
    """Own one RegisterHotKey binding and its native message thread."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._spec: Optional[HotkeySpec] = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready_event = threading.Event()
        self._startup_error: Optional[GlobalHotkeyError] = None

    @property
    def hotkey(self) -> Optional[str]:
        return self._spec.display if self._spec is not None else None

    def start(self, hotkey: str | HotkeySpec, timeout: float = 2.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise GlobalHotkeyError("全局快捷键已经注册")
        spec = parse_hotkey(hotkey) if isinstance(hotkey, str) else hotkey
        self._spec = spec
        self._startup_error = None
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._message_loop,
            args=(spec,),
            name="seat-sentinel-hotkey",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=max(0.1, timeout)):
            self.stop()
            raise GlobalHotkeyError("等待 Windows 注册快捷键超时")
        if self._startup_error is not None:
            error = self._startup_error
            if self._thread is not None:
                self._thread.join(timeout=timeout)
            self._thread = None
            self._thread_id = 0
            raise error

    def stop(self, timeout: float = 2.0) -> None:
        thread = self._thread
        thread_id = self._thread_id
        if thread is None:
            return
        if thread.is_alive() and thread_id:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.PostThreadMessageW.restype = wintypes.BOOL
            if not user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0):
                LOGGER.warning(
                    "Unable to stop the global-hotkey message loop: %d",
                    ctypes.get_last_error(),
                )
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.1, timeout))
        if not thread.is_alive():
            self._thread = None
            self._thread_id = 0

    def _message_loop(self, spec: HotkeySpec) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(_Message),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(_Message),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.PeekMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        registered = False
        message = _Message()
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            # Ensure PostThreadMessageW has a queue to target during shutdown.
            user32.PeekMessageW(
                ctypes.byref(message), None, 0, 0, PM_NOREMOVE
            )
            ctypes.set_last_error(0)
            registered = bool(
                user32.RegisterHotKey(
                    None,
                    HOTKEY_ID,
                    spec.modifiers | MOD_NOREPEAT,
                    spec.virtual_key,
                )
            )
            if not registered:
                error_code = ctypes.get_last_error()
                if error_code == ERROR_HOTKEY_ALREADY_REGISTERED:
                    detail = "该组合键已被其他程序占用"
                else:
                    detail = f"Windows 错误 {error_code}"
                self._startup_error = GlobalHotkeyError(
                    f"无法注册 {spec.display}：{detail}"
                )
                return
            self._ready_event.set()

            while True:
                result = int(
                    user32.GetMessageW(
                        ctypes.byref(message), None, 0, 0
                    )
                )
                if result <= 0:
                    if result < 0:
                        LOGGER.error(
                            "Windows global-hotkey message loop failed: %d",
                            ctypes.get_last_error(),
                        )
                    break
                if (
                    message.message == WM_HOTKEY
                    and int(message.wParam) == HOTKEY_ID
                ):
                    try:
                        self._callback()
                    except Exception:
                        LOGGER.exception("Global-hotkey callback failed")
        finally:
            if registered:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            self._ready_event.set()
