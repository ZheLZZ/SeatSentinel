"""SeatSentinel DWM Desktop Acrylic compatibility test.

This standalone tool intentionally does not import the main application.  It
creates native Win32 overlay windows so Desktop Acrylic can be tested in the
interactive user's real Windows session before the production blur path is
changed.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import platform
import sys
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass


if sys.platform != "win32":
    raise SystemExit("This compatibility test only runs on Windows.")


# Win32 constants
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

GWL_EXSTYLE = -20
LWA_ALPHA = 0x00000002
RGN_OR = 2

SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNA = 8
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1

WM_ERASEBKGND = 0x0014
WM_PAINT = 0x000F
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1

BLACK_BRUSH = 4
IDC_ARROW = 32512

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_DONOTROUND = 1
DWMSBT_TRANSIENTWINDOW = 3

MONITORINFOF_PRIMARY = 0x00000001
VK_F8 = 0x77


LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", ctypes.c_void_p),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


@dataclass(frozen=True)
class MonitorBounds:
    monitor: tuple[int, int, int, int]
    work: tuple[int, int, int, int]
    primary: bool


@dataclass
class OverlayWindow:
    hwnd: int
    mode: str
    work: tuple[int, int, int, int]
    backdrop_hresult: int
    frame_hresult: int


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)


def _configure_win32_signatures() -> None:
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.EnumDisplayMonitors.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        MONITORENUMPROC,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowRgn.restype = ctypes.c_int
    user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
    user32.SetWindowRgn.restype = ctypes.c_int
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.WORD
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.BeginPaint.argtypes = [ctypes.c_void_p, ctypes.POINTER(PAINTSTRUCT)]
    user32.BeginPaint.restype = ctypes.c_void_p
    user32.EndPaint.argtypes = [ctypes.c_void_p, ctypes.POINTER(PAINTSTRUCT)]
    user32.EndPaint.restype = wintypes.BOOL
    user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p]
    user32.FillRect.restype = ctypes.c_int
    user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.LoadCursorW.restype = ctypes.c_void_p
    user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p,
        wintypes.COLORREF,
        wintypes.BYTE,
        wintypes.DWORD,
    ]
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    get_window_long_ptr = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    get_window_long_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int]
    get_window_long_ptr.restype = LONG_PTR
    globals()["GetWindowLongPtrW"] = get_window_long_ptr

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetVersion.argtypes = []
    kernel32.GetVersion.restype = wintypes.DWORD
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = ctypes.c_void_p
    gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.CreateRectRgn.restype = ctypes.c_void_p
    gdi32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    gdi32.CombineRgn.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.PtInRegion.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.PtInRegion.restype = wintypes.BOOL
    gdi32.GetRgnBox.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    gdi32.GetRgnBox.restype = ctypes.c_int

    dwmapi.DwmExtendFrameIntoClientArea.argtypes = [ctypes.c_void_p, ctypes.POINTER(MARGINS)]
    dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
    dwmapi.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmIsCompositionEnabled.argtypes = [ctypes.POINTER(wintypes.BOOL)]
    dwmapi.DwmIsCompositionEnabled.restype = ctypes.c_long


_configure_win32_signatures()


def _rect_tuple(rect: wintypes.RECT) -> tuple[int, int, int, int]:
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


@contextlib.contextmanager
def _physical_pixel_context():
    """Use physical monitor coordinates without changing Tk's process scaling."""

    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        yield
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)


def _enumerate_monitors() -> list[MonitorBounds]:
    monitors: list[MonitorBounds] = []

    @MONITORENUMPROC
    def callback(hmonitor, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            monitors.append(
                MonitorBounds(
                    monitor=_rect_tuple(info.rcMonitor),
                    work=_rect_tuple(info.rcWork),
                    primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                )
            )
        return True

    with _physical_pixel_context():
        if not user32.EnumDisplayMonitors(None, None, callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    monitors.sort(key=lambda item: (not item.primary, item.monitor[0], item.monitor[1]))
    return monitors


def _windows_build() -> int:
    # platform.version() reports the real Windows kernel build for regular
    # CPython/PyInstaller processes and is not affected by the UI scale.
    try:
        return int(platform.version().split(".")[-1])
    except (ValueError, IndexError):
        version = int(kernel32.GetVersion())
        return (version >> 16) & 0xFFFF


def _hresult_hex(value: int) -> str:
    return f"0x{ctypes.c_uint32(value).value:08X}"


def _bounding_rect(rectangles: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    if not rectangles:
        raise ValueError("At least one rectangle is required.")
    return (
        min(rect[0] for rect in rectangles),
        min(rect[1] for rect in rectangles),
        max(rect[2] for rect in rectangles),
        max(rect[3] for rect in rectangles),
    )


def _apply_window_region(
    hwnd: int,
    window_bounds: tuple[int, int, int, int],
    visible_areas: list[tuple[int, int, int, int]],
) -> None:
    """Clip one virtual-desktop window to monitor work areas.

    SetWindowRgn takes ownership of the combined region on success. Each work
    area therefore remains visible while taskbars and gaps between monitors are
    excluded from the overlay.
    """

    window_left, window_top, _right, _bottom = window_bounds
    combined = gdi32.CreateRectRgn(0, 0, 0, 0)
    if not combined:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for left, top, right, bottom in visible_areas:
            part = gdi32.CreateRectRgn(
                left - window_left,
                top - window_top,
                right - window_left,
                bottom - window_top,
            )
            if not part:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not gdi32.CombineRgn(combined, combined, part, RGN_OR):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                gdi32.DeleteObject(part)
        if not user32.SetWindowRgn(hwnd, combined, True):
            raise ctypes.WinError(ctypes.get_last_error())
        combined = None
    finally:
        if combined:
            gdi32.DeleteObject(combined)


_PASSTHROUGH_WINDOWS: set[int] = set()


@WNDPROC
def _window_proc(hwnd, message, wparam, lparam):
    hwnd_value = int(hwnd or 0)
    if message == WM_NCHITTEST and hwnd_value in _PASSTHROUGH_WINDOWS:
        return HTTRANSPARENT
    if message == WM_ERASEBKGND:
        return 1
    if message == WM_PAINT:
        paint = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(paint))
        if hdc:
            user32.FillRect(hdc, ctypes.byref(paint.rcPaint), gdi32.GetStockObject(BLACK_BRUSH))
        user32.EndPaint(hwnd, ctypes.byref(paint))
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


_WINDOW_CLASS_NAME = f"SeatSentinelDwmAcrylicTest_{os.getpid()}"
_WINDOW_CLASS_REGISTERED = False


def _register_window_class() -> None:
    global _WINDOW_CLASS_REGISTERED
    if _WINDOW_CLASS_REGISTERED:
        return

    instance = kernel32.GetModuleHandleW(None)
    window_class = WNDCLASSEXW()
    window_class.cbSize = ctypes.sizeof(window_class)
    window_class.style = 0
    window_class.lpfnWndProc = _window_proc
    window_class.hInstance = instance
    window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
    window_class.hbrBackground = gdi32.GetStockObject(BLACK_BRUSH)
    window_class.lpszClassName = _WINDOW_CLASS_NAME
    if not user32.RegisterClassExW(ctypes.byref(window_class)):
        raise ctypes.WinError(ctypes.get_last_error())
    _WINDOW_CLASS_REGISTERED = True


def _set_dwm_attribute(hwnd: int, attribute: int, value: int) -> int:
    payload = ctypes.c_int(value)
    return int(
        dwmapi.DwmSetWindowAttribute(
            hwnd, attribute, ctypes.byref(payload), ctypes.sizeof(payload)
        )
    )


def _create_overlay(
    bounds: tuple[int, int, int, int],
    mode: str,
    *,
    visible: bool = True,
    alpha: int = 255,
    visible_areas: list[tuple[int, int, int, int]] | None = None,
) -> OverlayWindow:
    _register_window_class()
    left, top, right, bottom = bounds
    width = max(1, right - left)
    height = max(1, bottom - top)

    ex_style = WS_EX_TOOLWINDOW
    if mode in {"B", "C", "D"}:
        ex_style |= WS_EX_NOACTIVATE | WS_EX_TRANSPARENT
    if mode in {"C", "D"}:
        ex_style |= WS_EX_LAYERED

    window_style = WS_POPUP | (WS_VISIBLE if visible else 0)
    with _physical_pixel_context():
        hwnd_raw = user32.CreateWindowExW(
            ex_style,
            _WINDOW_CLASS_NAME,
            "SeatSentinel DWM Acrylic Test",
            window_style,
            left,
            top,
            width,
            height,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
    if not hwnd_raw:
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = int(hwnd_raw)

    if mode in {"B", "C", "D"}:
        _PASSTHROUGH_WINDOWS.add(hwnd)
    if visible_areas:
        try:
            with _physical_pixel_context():
                _apply_window_region(hwnd, bounds, visible_areas)
        except Exception:
            user32.DestroyWindow(hwnd)
            _PASSTHROUGH_WINDOWS.discard(hwnd)
            raise
    if mode in {"C", "D"} and not user32.SetLayeredWindowAttributes(
        hwnd, 0, max(1, min(255, int(alpha))), LWA_ALPHA
    ):
        user32.DestroyWindow(hwnd)
        _PASSTHROUGH_WINDOWS.discard(hwnd)
        raise ctypes.WinError(ctypes.get_last_error())

    # Extending the frame through the complete client area gives DWM a surface
    # on which it can draw the selected system backdrop.
    margins = MARGINS(-1, -1, -1, -1)
    frame_hresult = int(dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)))
    _set_dwm_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND)
    _set_dwm_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    backdrop_hresult = _set_dwm_attribute(
        hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW
    )

    if visible:
        with _physical_pixel_context():
            flags = SWP_SHOWWINDOW
            if mode in {"B", "C", "D"}:
                flags |= SWP_NOACTIVATE
            if not user32.SetWindowPos(
                hwnd,
                ctypes.c_void_p(HWND_TOPMOST),
                left,
                top,
                width,
                height,
                flags,
            ):
                user32.DestroyWindow(hwnd)
                _PASSTHROUGH_WINDOWS.discard(hwnd)
                raise ctypes.WinError(ctypes.get_last_error())
        user32.ShowWindow(hwnd, SW_SHOW if mode == "A" else SW_SHOWNA)
        if mode == "A":
            user32.SetForegroundWindow(hwnd)

    return OverlayWindow(
        hwnd=hwnd,
        mode=mode,
        work=bounds,
        backdrop_hresult=backdrop_hresult,
        frame_hresult=frame_hresult,
    )


def _destroy_overlay(overlay: OverlayWindow) -> None:
    _PASSTHROUGH_WINDOWS.discard(overlay.hwnd)
    if overlay.hwnd:
        user32.ShowWindow(overlay.hwnd, SW_HIDE)
        user32.DestroyWindow(overlay.hwnd)
        overlay.hwnd = 0


class AcrylicTestApp:
    COLORS = {
        "background": "#07111F",
        "panel": "#0B1A2D",
        "border": "#24425E",
        "text": "#EAF6FF",
        "muted": "#8DAFCC",
        "cyan": "#00D9FF",
        "green": "#42F5A7",
        "button": "#12314C",
        "button_hover": "#194463",
        "danger": "#C95265",
    }

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("DWM Desktop Acrylic 兼容性测试")
        self.root.configure(bg=self.COLORS["background"])
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())

        self.monitors = _enumerate_monitors()
        self.overlays: list[OverlayWindow] = []
        self.current_mode: str | None = None
        self._f8_down = False
        self.status_text = tk.StringVar(value="尚未开始测试")
        self.detail_text = tk.StringVar(value="请先在桌面打开一个有文字或图片的窗口。")
        self.strength_value = tk.IntVar(value=82)
        self.strength_text = tk.StringVar(value="毛玻璃强度：82%（推荐）")

        self._build_ui()
        self._place_window()
        self.root.after(40, self._poll_f8)

    def _label(self, parent, text, **kwargs):
        options = {
            "bg": self.COLORS["background"],
            "fg": self.COLORS["text"],
            "font": ("Microsoft YaHei UI", 10),
        }
        options.update(kwargs)
        return tk.Label(parent, text=text, **options)

    def _button(self, parent, text, command, *, accent=False):
        background = self.COLORS["cyan"] if accent else self.COLORS["button"]
        foreground = "#03121C" if accent else self.COLORS["text"]
        active_background = self.COLORS["green"] if accent else self.COLORS["button_hover"]
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=foreground,
            activebackground=active_background,
            activeforeground="#03121C" if accent else self.COLORS["text"],
            relief="flat",
            bd=0,
            padx=18,
            pady=11,
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        )

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=self.COLORS["background"], padx=26, pady=22)
        outer.pack(fill="both", expand=True)

        title_row = tk.Frame(outer, bg=self.COLORS["background"])
        title_row.pack(fill="x")
        self._label(
            title_row,
            "DWM ACRYLIC TEST",
            fg=self.COLORS["cyan"],
            font=("Consolas", 18, "bold"),
        ).pack(side="left")
        self._label(
            title_row,
            "只做显示兼容性测试",
            fg=self.COLORS["green"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="right", pady=(7, 0))

        build = _windows_build()
        system_line = f"Windows Build {build}  ·  检测到 {len(self.monitors)} 个显示器"
        self._label(
            outer,
            system_line,
            fg=self.COLORS["muted"],
            anchor="w",
        ).pack(fill="x", pady=(5, 17))

        guide = tk.Frame(
            outer,
            bg=self.COLORS["panel"],
            highlightbackground=self.COLORS["border"],
            highlightthickness=1,
            padx=18,
            pady=14,
        )
        guide.pack(fill="x")
        self._label(
            guide,
            "怎么测试",
            bg=self.COLORS["panel"],
            fg=self.COLORS["cyan"],
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        ).pack(fill="x")
        instructions = (
            "1. 先打开浏览器或文件夹，让两个屏幕都有可辨认的文字。\n"
            "2. 本轮重点测试 D；毛玻璃出现后，尝试点击、关闭或移动下面的窗口。\n"
            "3. 观察副屏是否还会灰闪，以及点击是否穿透；随时按 F8 隐藏测试层。"
        )
        self._label(
            guide,
            instructions,
            bg=self.COLORS["panel"],
            fg=self.COLORS["text"],
            justify="left",
            anchor="w",
            font=("Microsoft YaHei UI", 10),
        ).pack(fill="x", pady=(7, 0))

        strength = tk.Frame(
            outer,
            bg=self.COLORS["panel"],
            highlightbackground=self.COLORS["border"],
            highlightthickness=1,
            padx=18,
            pady=10,
        )
        strength.pack(fill="x", pady=(14, 0))
        tk.Label(
            strength,
            textvariable=self.strength_text,
            bg=self.COLORS["panel"],
            fg=self.COLORS["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Scale(
            strength,
            from_=65,
            to=100,
            orient="horizontal",
            variable=self.strength_value,
            command=lambda value: self.strength_text.set(
                f"毛玻璃强度：{int(float(value))}%"
                + ("（推荐）" if int(float(value)) == 82 else "")
            ),
            showvalue=False,
            resolution=1,
            bg=self.COLORS["panel"],
            fg=self.COLORS["text"],
            activebackground=self.COLORS["cyan"],
            troughcolor=self.COLORS["button"],
            highlightthickness=0,
            bd=0,
            sliderrelief="flat",
            length=610,
        ).pack(fill="x", pady=(4, 0))

        buttons = tk.Frame(outer, bg=self.COLORS["background"])
        buttons.pack(fill="x", pady=(18, 0))
        self._button(
            buttons,
            "测试 A：普通 Acrylic（主屏）",
            lambda: self.show_mode("A"),
        ).pack(fill="x", pady=(0, 8))
        self._button(
            buttons,
            "测试 B：无激活、点击穿透（双屏）",
            lambda: self.show_mode("B"),
        ).pack(fill="x", pady=(0, 8))
        self._button(
            buttons,
            "测试 C：原双窗口模式（用于对照）",
            lambda: self.show_mode("C"),
        ).pack(fill="x", pady=(0, 8))
        self._button(
            buttons,
            "测试 D：单窗口跨双屏（本轮推荐）",
            lambda: self.show_mode("D"),
            accent=True,
        ).pack(fill="x")

        status = tk.Frame(
            outer,
            bg=self.COLORS["panel"],
            highlightbackground=self.COLORS["border"],
            highlightthickness=1,
            padx=18,
            pady=12,
        )
        status.pack(fill="x", pady=(18, 0))
        tk.Label(
            status,
            textvariable=self.status_text,
            bg=self.COLORS["panel"],
            fg=self.COLORS["green"],
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            status,
            textvariable=self.detail_text,
            bg=self.COLORS["panel"],
            fg=self.COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
            justify="left",
            anchor="w",
            wraplength=650,
        ).pack(fill="x", pady=(5, 0))

        footer = tk.Frame(outer, bg=self.COLORS["background"])
        footer.pack(fill="x", pady=(14, 0))
        self._button(footer, "隐藏测试层（F8）", self.hide_overlays).pack(side="left")
        self._button(footer, "退出", self.close).pack(side="right")

        if build < 22621:
            self.status_text.set("当前 Windows 版本不支持这项原生效果")
            self.detail_text.set("需要 Windows 11 Build 22621 或更高版本。")

    def _place_window(self) -> None:
        self.root.update_idletasks()
        width = max(710, self.root.winfo_reqwidth())
        height = self.root.winfo_reqheight()
        primary = next((item for item in self.monitors if item.primary), self.monitors[0])
        left, top, right, bottom = primary.work
        x = left + max(0, (right - left - width) // 2)
        y = top + max(0, (bottom - top - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def show_mode(self, mode: str) -> None:
        self.hide_overlays(update_status=False)
        if _windows_build() < 22621:
            self.status_text.set("无法测试：Windows 版本低于 Build 22621")
            return

        created: list[OverlayWindow] = []
        try:
            if mode == "D":
                work_areas = [monitor.work for monitor in self.monitors]
                virtual_bounds = _bounding_rect(work_areas)
                alpha = round(255 * self.strength_value.get() / 100)
                created.append(
                    _create_overlay(
                        virtual_bounds,
                        mode,
                        visible=True,
                        alpha=alpha,
                        visible_areas=work_areas,
                    )
                )
            else:
                targets = self.monitors
                if mode == "A":
                    targets = [
                        next(
                            (item for item in self.monitors if item.primary),
                            self.monitors[0],
                        )
                    ]
                for monitor in targets:
                    created.append(_create_overlay(monitor.work, mode, visible=True))
        except Exception as exc:
            for overlay in created:
                _destroy_overlay(overlay)
            self.status_text.set(f"模式 {mode} 创建失败")
            self.detail_text.set(str(exc))
            self.root.lift()
            return

        self.overlays = created
        self.current_mode = mode
        results = ", ".join(
            f"屏幕{i + 1}: {_hresult_hex(item.backdrop_hresult)}"
            for i, item in enumerate(created)
        )
        window_description = "1 个跨屏窗口" if mode == "D" else f"{len(created)} 个测试窗口"
        self.status_text.set(f"模式 {mode} 已显示 · {window_description}")
        self.detail_text.set(
            f"DWM 返回值 {results}。按 F8 隐藏。即使返回 0x00000000，"
            "若只显示纯色，也说明系统在当前窗口组合下发生了降级。"
        )

    def hide_overlays(self, *, update_status: bool = True) -> None:
        for overlay in self.overlays:
            _destroy_overlay(overlay)
        had_overlays = bool(self.overlays)
        self.overlays.clear()
        previous_mode = self.current_mode
        self.current_mode = None
        if update_status:
            if had_overlays:
                self.status_text.set(f"模式 {previous_mode} 已隐藏")
                self.detail_text.set("可以继续测试其他模式。")
            else:
                self.status_text.set("当前没有显示测试层")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _poll_f8(self) -> None:
        try:
            pressed = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
            if pressed and not self._f8_down:
                self.hide_overlays()
            self._f8_down = pressed
            self.root.after(40, self._poll_f8)
        except tk.TclError:
            pass

    def close(self) -> None:
        self.hide_overlays(update_status=False)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _self_test() -> int:
    result: dict[str, object] = {
        "windows_build": _windows_build(),
        "supported_build": _windows_build() >= 22621,
    }
    composition = wintypes.BOOL()
    composition_hresult = int(dwmapi.DwmIsCompositionEnabled(ctypes.byref(composition)))
    result["composition_hresult"] = _hresult_hex(composition_hresult)
    result["composition_enabled"] = bool(composition.value)

    try:
        monitors = _enumerate_monitors()
        result["monitor_count"] = len(monitors)
        result["work_areas"] = [list(item.work) for item in monitors]
        if not monitors:
            raise RuntimeError("No monitors were detected.")

        primary = next((item for item in monitors if item.primary), monitors[0])
        expected_styles = {
            "A": {"layered": False, "transparent": False, "noactivate": False},
            "B": {"layered": False, "transparent": True, "noactivate": True},
            "C": {"layered": True, "transparent": True, "noactivate": True},
            "D": {"layered": True, "transparent": True, "noactivate": True},
        }
        mode_results: dict[str, list[dict[str, object]]] = {}
        modes_ok = True

        for mode in ("A", "B", "C", "D"):
            work_areas = [monitor.work for monitor in monitors]
            if mode == "D":
                cases = [(_bounding_rect(work_areas), work_areas)]
            else:
                targets = [primary] if mode == "A" else monitors
                cases = [(monitor.work, None) for monitor in targets]
            created: list[OverlayWindow] = []
            mode_results[mode] = []
            try:
                for expected_bounds, visible_areas in cases:
                    overlay = _create_overlay(
                        expected_bounds,
                        mode,
                        visible=False,
                        alpha=round(255 * 0.82) if mode == "D" else 255,
                        visible_areas=visible_areas,
                    )
                    created.append(overlay)
                    ex_style = int(GetWindowLongPtrW(overlay.hwnd, GWL_EXSTYLE))
                    actual_styles = {
                        "layered": bool(ex_style & WS_EX_LAYERED),
                        "transparent": bool(ex_style & WS_EX_TRANSPARENT),
                        "noactivate": bool(ex_style & WS_EX_NOACTIVATE),
                    }
                    window_rect = wintypes.RECT()
                    with _physical_pixel_context():
                        rect_ok = bool(user32.GetWindowRect(overlay.hwnd, ctypes.byref(window_rect)))
                    actual_bounds = _rect_tuple(window_rect) if rect_ok else None

                    region_ok = True
                    region_type = None
                    region_center_hits = None
                    region_box = None
                    if mode == "D":
                        copied_region = gdi32.CreateRectRgn(0, 0, 0, 0)
                        if not copied_region:
                            raise ctypes.WinError(ctypes.get_last_error())
                        try:
                            with _physical_pixel_context():
                                region_type = int(
                                    user32.GetWindowRgn(overlay.hwnd, copied_region)
                                )
                                region_rect = wintypes.RECT()
                                gdi32.GetRgnBox(copied_region, ctypes.byref(region_rect))
                                region_box = _rect_tuple(region_rect)
                                window_left, window_top, _right, _bottom = expected_bounds
                                region_center_hits = [
                                    gdi32.PtInRegion(
                                        copied_region,
                                        ((area[0] + area[2]) // 2) - window_left,
                                        ((area[1] + area[3]) // 2) - window_top,
                                    )
                                    for area in work_areas
                                ]
                            inside_points_ok = all(region_center_hits)
                            region_ok = region_type > 0 and inside_points_ok
                        finally:
                            gdi32.DeleteObject(copied_region)

                    item_ok = (
                        overlay.backdrop_hresult >= 0
                        and overlay.frame_hresult >= 0
                        and actual_styles == expected_styles[mode]
                        and actual_bounds == expected_bounds
                        and region_ok
                    )
                    modes_ok = modes_ok and item_ok
                    mode_results[mode].append(
                        {
                            "expected_bounds": list(expected_bounds),
                            "actual_bounds": list(actual_bounds) if actual_bounds else None,
                            "backdrop_hresult": _hresult_hex(overlay.backdrop_hresult),
                            "frame_hresult": _hresult_hex(overlay.frame_hresult),
                            "styles": actual_styles,
                            "window_region_type": region_type,
                            "window_region_box": list(region_box) if region_box else None,
                            "region_center_hits": region_center_hits,
                            "ok": item_ok,
                        }
                    )
            finally:
                for overlay in created:
                    _destroy_overlay(overlay)

        result["modes"] = mode_results
        ok = bool(
            result["supported_build"]
            and result["composition_enabled"]
            and modes_ok
        )
    except Exception as exc:
        result["error"] = repr(exc)
        ok = False

    result["ok"] = bool(ok)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="DWM Desktop Acrylic compatibility test")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate native APIs and window styles without showing the test UI.",
    )
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    AcrylicTestApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
