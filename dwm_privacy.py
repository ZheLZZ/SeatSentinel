"""Native Windows 11 Desktop Acrylic privacy overlay.

The overlay is rendered by DWM.  SeatSentinel does not capture, resize, blur,
or upload desktop pixels.  One layered, non-activating window spans the virtual
desktop and is clipped to the work area of every monitor so taskbars remain
visible and interactive.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import platform
import sys
from ctypes import wintypes
from dataclasses import dataclass


if sys.platform != "win32":
    raise RuntimeError("The DWM privacy overlay requires Windows.")


WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
LWA_ALPHA = 0x00000002

SW_HIDE = 0
SW_SHOWNA = 8
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1

WM_ERASEBKGND = 0x0014
WM_PAINT = 0x000F
WM_NCHITTEST = 0x0084
WM_NCDESTROY = 0x0082
HTTRANSPARENT = -1

BLACK_BRUSH = 4
IDC_ARROW = 32512
RGN_OR = 2

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_DONOTROUND = 1
DWMSBT_TRANSIENTWINDOW = 3

MONITORINFOF_PRIMARY = 0x00000001
MINIMUM_DESKTOP_ACRYLIC_BUILD = 22621
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)


class Margins(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class PaintStruct(ctypes.Structure):
    _fields_ = [
        ("hdc", ctypes.c_void_p),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class WindowClassEx(ctypes.Structure):
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
class MonitorWorkArea:
    monitor: tuple[int, int, int, int]
    work: tuple[int, int, int, int]
    primary: bool


@dataclass(frozen=True)
class DwmOverlayInfo:
    handle: int
    bounds: tuple[int, int, int, int]
    work_areas: tuple[tuple[int, int, int, int], ...]
    strength_percent: int
    alpha: int
    backdrop_hresult: int
    frame_hresult: int


class DwmPrivacyError(RuntimeError):
    """Raised when Windows cannot create the native privacy surface."""


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
    user32.GetMonitorInfoW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(MonitorInfo),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.RECT),
    ]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowRgn.restype = ctypes.c_int
    user32.SetWindowRgn.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
    ]
    user32.SetWindowRgn.restype = ctypes.c_int
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WindowClassEx)]
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
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
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
    user32.UpdateWindow.argtypes = [ctypes.c_void_p]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.BeginPaint.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(PaintStruct),
    ]
    user32.BeginPaint.restype = ctypes.c_void_p
    user32.EndPaint.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(PaintStruct),
    ]
    user32.EndPaint.restype = wintypes.BOOL
    user32.FillRect.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.RECT),
        ctypes.c_void_p,
    ]
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

    get_window_long_ptr = getattr(
        user32,
        "GetWindowLongPtrW",
        user32.GetWindowLongW,
    )
    get_window_long_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int]
    get_window_long_ptr.restype = LONG_PTR
    globals()["GetWindowLongPtrW"] = get_window_long_ptr

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = ctypes.c_void_p
    gdi32.CreateRectRgn.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    gdi32.CreateRectRgn.restype = ctypes.c_void_p
    gdi32.CombineRgn.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    gdi32.CombineRgn.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.PtInRegion.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    gdi32.PtInRegion.restype = wintypes.BOOL

    dwmapi.DwmExtendFrameIntoClientArea.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(Margins),
    ]
    dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
    dwmapi.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmIsCompositionEnabled.argtypes = [
        ctypes.POINTER(wintypes.BOOL),
    ]
    dwmapi.DwmIsCompositionEnabled.restype = ctypes.c_long


_configure_win32_signatures()


def windows_build() -> int:
    try:
        return int(platform.version().split(".")[-1])
    except (IndexError, ValueError):
        return 0


def acrylic_alpha_from_strength(strength_percent: int) -> int:
    """Convert the user-tested visual strength to a layered-window alpha."""

    strength = int(strength_percent)
    if not 1 <= strength <= 100:
        raise ValueError("Acrylic strength must be between 1 and 100")
    return max(1, min(255, round(255 * strength / 100)))


def bounding_rect(
    rectangles: tuple[tuple[int, int, int, int], ...],
) -> tuple[int, int, int, int]:
    """Return an LTRB rectangle enclosing every supplied LTRB rectangle."""

    if not rectangles:
        raise ValueError("At least one rectangle is required")
    return (
        min(rect[0] for rect in rectangles),
        min(rect[1] for rect in rectangles),
        max(rect[2] for rect in rectangles),
        max(rect[3] for rect in rectangles),
    )


def relative_work_regions(
    window_bounds: tuple[int, int, int, int],
    work_areas: tuple[tuple[int, int, int, int], ...],
) -> tuple[tuple[int, int, int, int], ...]:
    """Translate screen work areas to one virtual-desktop window's origin."""

    window_left, window_top, _right, _bottom = window_bounds
    return tuple(
        (
            left - window_left,
            top - window_top,
            right - window_left,
            bottom - window_top,
        )
        for left, top, right, bottom in work_areas
    )


@contextlib.contextmanager
def _physical_pixel_context():
    previous = user32.SetThreadDpiAwarenessContext(
        ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    )
    if not previous:
        raise DwmPrivacyError(
            "Windows did not enter per-monitor physical-pixel mode"
        )
    try:
        yield
    finally:
        if not user32.SetThreadDpiAwarenessContext(previous):
            raise DwmPrivacyError(
                "Windows did not restore the previous DPI context"
            )


def _rect_tuple(rectangle: wintypes.RECT) -> tuple[int, int, int, int]:
    return (
        int(rectangle.left),
        int(rectangle.top),
        int(rectangle.right),
        int(rectangle.bottom),
    )


def enumerate_monitor_work_areas() -> tuple[MonitorWorkArea, ...]:
    monitors: list[MonitorWorkArea] = []

    @MONITORENUMPROC
    def callback(hmonitor, _hdc, _rect, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            monitors.append(
                MonitorWorkArea(
                    monitor=_rect_tuple(info.rcMonitor),
                    work=_rect_tuple(info.rcWork),
                    primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                )
            )
        return True

    with _physical_pixel_context():
        if not user32.EnumDisplayMonitors(None, None, callback, 0):
            raise DwmPrivacyError(
                f"Unable to enumerate monitors: {ctypes.get_last_error()}"
            )
    if not monitors:
        raise DwmPrivacyError("Windows did not report an active monitor")
    monitors.sort(
        key=lambda item: (
            not item.primary,
            item.monitor[0],
            item.monitor[1],
        )
    )
    return tuple(monitors)


def monitor_work_area_signature() -> tuple[tuple[int, int, int, int], ...]:
    return tuple(item.work for item in enumerate_monitor_work_areas())


_PASSTHROUGH_WINDOWS: set[int] = set()


@WNDPROC
def _window_proc(hwnd, message, wparam, lparam):
    handle = int(hwnd or 0)
    if message == WM_NCHITTEST and handle in _PASSTHROUGH_WINDOWS:
        return HTTRANSPARENT
    if message == WM_ERASEBKGND:
        return 1
    if message == WM_PAINT:
        paint = PaintStruct()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(paint))
        if hdc:
            user32.FillRect(
                hdc,
                ctypes.byref(paint.rcPaint),
                gdi32.GetStockObject(BLACK_BRUSH),
            )
        user32.EndPaint(hwnd, ctypes.byref(paint))
        return 0
    if message == WM_NCDESTROY:
        _PASSTHROUGH_WINDOWS.discard(handle)
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


_WINDOW_CLASS_NAME = f"SeatSentinelDwmPrivacy_{os.getpid()}"
_WINDOW_CLASS_REGISTERED = False


def _register_window_class() -> None:
    global _WINDOW_CLASS_REGISTERED
    if _WINDOW_CLASS_REGISTERED:
        return
    window_class = WindowClassEx()
    window_class.cbSize = ctypes.sizeof(window_class)
    window_class.lpfnWndProc = _window_proc
    window_class.hInstance = kernel32.GetModuleHandleW(None)
    window_class.hCursor = user32.LoadCursorW(
        None,
        ctypes.c_void_p(IDC_ARROW),
    )
    window_class.hbrBackground = gdi32.GetStockObject(BLACK_BRUSH)
    window_class.lpszClassName = _WINDOW_CLASS_NAME
    if not user32.RegisterClassExW(ctypes.byref(window_class)):
        raise DwmPrivacyError(
            f"Unable to register the DWM window class: "
            f"{ctypes.get_last_error()}"
        )
    _WINDOW_CLASS_REGISTERED = True


def _set_dwm_attribute(handle: int, attribute: int, value: int) -> int:
    payload = ctypes.c_int(value)
    return int(
        dwmapi.DwmSetWindowAttribute(
            handle,
            attribute,
            ctypes.byref(payload),
            ctypes.sizeof(payload),
        )
    )


def _apply_work_area_region(
    handle: int,
    window_bounds: tuple[int, int, int, int],
    work_areas: tuple[tuple[int, int, int, int], ...],
) -> None:
    combined = gdi32.CreateRectRgn(0, 0, 0, 0)
    if not combined:
        raise DwmPrivacyError("Unable to allocate the work-area region")
    try:
        for left, top, right, bottom in relative_work_regions(
            window_bounds,
            work_areas,
        ):
            part = gdi32.CreateRectRgn(left, top, right, bottom)
            if not part:
                raise DwmPrivacyError("Unable to allocate a monitor region")
            try:
                if not gdi32.CombineRgn(
                    combined,
                    combined,
                    part,
                    RGN_OR,
                ):
                    raise DwmPrivacyError("Unable to combine monitor regions")
            finally:
                gdi32.DeleteObject(part)
        if not user32.SetWindowRgn(handle, combined, True):
            raise DwmPrivacyError("Unable to clip the overlay to work areas")
        # Windows owns the region after SetWindowRgn succeeds.
        combined = None
    finally:
        if combined:
            gdi32.DeleteObject(combined)


def _create_overlay_window(
    work_areas: tuple[tuple[int, int, int, int], ...],
    strength_percent: int,
    *,
    visible: bool,
) -> DwmOverlayInfo:
    if windows_build() < MINIMUM_DESKTOP_ACRYLIC_BUILD:
        raise DwmPrivacyError(
            "Desktop Acrylic requires Windows 11 Build 22621 or newer"
        )
    composition_enabled = wintypes.BOOL()
    composition_hresult = int(
        dwmapi.DwmIsCompositionEnabled(
            ctypes.byref(composition_enabled)
        )
    )
    if composition_hresult < 0 or not composition_enabled.value:
        raise DwmPrivacyError("Windows desktop composition is unavailable")

    _register_window_class()
    bounds = bounding_rect(work_areas)
    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    alpha = acrylic_alpha_from_strength(strength_percent)
    ex_style = (
        WS_EX_TOOLWINDOW
        | WS_EX_NOACTIVATE
        | WS_EX_LAYERED
        | WS_EX_TRANSPARENT
    )

    with _physical_pixel_context():
        raw_handle = user32.CreateWindowExW(
            ex_style,
            _WINDOW_CLASS_NAME,
            "SeatSentinel Privacy Acrylic",
            WS_POPUP,
            left,
            top,
            width,
            height,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
    if not raw_handle:
        raise DwmPrivacyError(
            f"Unable to create the DWM overlay: {ctypes.get_last_error()}"
        )
    handle = int(raw_handle)
    _PASSTHROUGH_WINDOWS.add(handle)

    try:
        with _physical_pixel_context():
            _apply_work_area_region(handle, bounds, work_areas)
        if not user32.SetLayeredWindowAttributes(
            handle,
            0,
            alpha,
            LWA_ALPHA,
        ):
            raise DwmPrivacyError(
                f"Unable to set Acrylic strength: {ctypes.get_last_error()}"
            )

        margins = Margins(-1, -1, -1, -1)
        frame_hresult = int(
            dwmapi.DwmExtendFrameIntoClientArea(
                handle,
                ctypes.byref(margins),
            )
        )
        _set_dwm_attribute(
            handle,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            DWMWCP_DONOTROUND,
        )
        _set_dwm_attribute(
            handle,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            1,
        )
        backdrop_hresult = _set_dwm_attribute(
            handle,
            DWMWA_SYSTEMBACKDROP_TYPE,
            DWMSBT_TRANSIENTWINDOW,
        )
        if frame_hresult < 0 or backdrop_hresult < 0:
            raise DwmPrivacyError(
                "Windows rejected the Desktop Acrylic backdrop "
                f"(frame={frame_hresult}, backdrop={backdrop_hresult})"
            )

        if visible:
            with _physical_pixel_context():
                if not user32.SetWindowPos(
                    handle,
                    ctypes.c_void_p(HWND_TOPMOST),
                    left,
                    top,
                    width,
                    height,
                    SWP_NOACTIVATE | SWP_SHOWWINDOW,
                ):
                    raise DwmPrivacyError(
                        "Unable to place the DWM overlay on the virtual desktop"
                    )
            user32.ShowWindow(handle, SW_SHOWNA)
            user32.UpdateWindow(handle)

        return DwmOverlayInfo(
            handle=handle,
            bounds=bounds,
            work_areas=work_areas,
            strength_percent=int(strength_percent),
            alpha=alpha,
            backdrop_hresult=backdrop_hresult,
            frame_hresult=frame_hresult,
        )
    except Exception:
        _PASSTHROUGH_WINDOWS.discard(handle)
        user32.DestroyWindow(handle)
        raise


def _destroy_overlay_window(handle: int) -> None:
    if not handle:
        return
    _PASSTHROUGH_WINDOWS.discard(handle)
    if user32.IsWindow(handle):
        user32.ShowWindow(handle, SW_HIDE)
        user32.DestroyWindow(handle)


class DwmPrivacyOverlay:
    """Own the application's single virtual-desktop Acrylic window."""

    def __init__(self, strength_percent: int = 96) -> None:
        acrylic_alpha_from_strength(strength_percent)
        self._strength_percent = int(strength_percent)
        self._info: DwmOverlayInfo | None = None

    @property
    def info(self) -> DwmOverlayInfo | None:
        return self._info

    @property
    def work_areas(self) -> tuple[tuple[int, int, int, int], ...]:
        return self._info.work_areas if self._info is not None else ()

    def is_visible(self) -> bool:
        return bool(
            self._info is not None
            and user32.IsWindow(self._info.handle)
        )

    def show(self) -> DwmOverlayInfo:
        self.hide()
        work_areas = monitor_work_area_signature()
        self._info = _create_overlay_window(
            work_areas,
            self._strength_percent,
            visible=True,
        )
        return self._info

    def hide(self) -> None:
        info = self._info
        self._info = None
        if info is not None:
            _destroy_overlay_window(info.handle)

    def display_geometry_changed(self) -> bool:
        return monitor_work_area_signature() != self.work_areas

    def self_test(self) -> dict[str, object]:
        """Validate the tested D-mode window without showing it."""

        work_areas = monitor_work_area_signature()
        info = _create_overlay_window(
            work_areas,
            self._strength_percent,
            visible=False,
        )
        try:
            ex_style = int(GetWindowLongPtrW(info.handle, GWL_EXSTYLE))
            style_ok = all(
                ex_style & style
                for style in (
                    WS_EX_TOOLWINDOW,
                    WS_EX_NOACTIVATE,
                    WS_EX_LAYERED,
                    WS_EX_TRANSPARENT,
                )
            )
            window_rect = wintypes.RECT()
            with _physical_pixel_context():
                rect_ok = bool(
                    user32.GetWindowRect(
                        info.handle,
                        ctypes.byref(window_rect),
                    )
                )
            bounds_ok = rect_ok and _rect_tuple(window_rect) == info.bounds

            copied_region = gdi32.CreateRectRgn(0, 0, 0, 0)
            if not copied_region:
                raise DwmPrivacyError("Unable to inspect the window region")
            try:
                with _physical_pixel_context():
                    region_type = int(
                        user32.GetWindowRgn(info.handle, copied_region)
                    )
                    left, top, _right, _bottom = info.bounds
                    centers_ok = all(
                        gdi32.PtInRegion(
                            copied_region,
                            ((area[0] + area[2]) // 2) - left,
                            ((area[1] + area[3]) // 2) - top,
                        )
                        for area in work_areas
                    )
            finally:
                gdi32.DeleteObject(copied_region)

            ok = bool(
                style_ok
                and bounds_ok
                and region_type > 0
                and centers_ok
                and info.backdrop_hresult >= 0
                and info.frame_hresult >= 0
                and info.alpha
                == acrylic_alpha_from_strength(self._strength_percent)
            )
            return {
                "ok": ok,
                "monitor_count": len(work_areas),
                "bounds": info.bounds,
                "work_areas": work_areas,
                "strength_percent": info.strength_percent,
                "alpha": info.alpha,
                "backdrop_hresult": info.backdrop_hresult,
                "frame_hresult": info.frame_hresult,
                "style_ok": bool(style_ok),
                "bounds_ok": bool(bounds_ok),
                "region_type": region_type,
                "centers_ok": bool(centers_ok),
            }
        finally:
            _destroy_overlay_window(info.handle)


__all__ = [
    "DwmOverlayInfo",
    "DwmPrivacyError",
    "DwmPrivacyOverlay",
    "MonitorWorkArea",
    "acrylic_alpha_from_strength",
    "bounding_rect",
    "enumerate_monitor_work_areas",
    "monitor_work_area_signature",
    "relative_work_regions",
    "windows_build",
]
