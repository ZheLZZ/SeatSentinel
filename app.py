"""System-tray application for SeatSentinel."""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import replace
from ctypes import wintypes
from tkinter import messagebox, ttk
from typing import Callable, Optional

import cv2
import numpy as np
import pystray
from cv2_enumerate_cameras import enumerate_cameras
from PIL import Image, ImageDraw, ImageEnhance, ImageTk

import main as monitoring
import config
from debug_frame import DebugFrameBuffer, DebugFrameSnapshot
from detector import FaceDetection, FaceDetector
from dwm_privacy import DwmPrivacyError, DwmPrivacyOverlay
from face_identity import (
    FaceIdentityRecognizer,
    FaceTemplateError,
    FaceTemplateStore,
)
from face_registration import (
    FaceRegistrationCancelled,
    FaceRegistrationError,
    FaceRegistrationUpdate,
    register_face_from_camera,
)
from global_hotkey import GlobalHotkeyError, WindowsGlobalHotkey
from privacy_blur import (
    MouseShakeDetector,
    PrivacyBlurSignal,
    PrivacyBlurSnapshot,
)
from sedentary_reminder import (
    SedentaryReminderSignal,
    SedentaryReminderSnapshot,
)
from session_monitor import SessionMonitor
from single_instance import (
    DebugWindowSignal,
    SingleInstance,
    SingleInstanceError,
    request_debug_window,
    show_already_running_message,
)
from user_settings import AppSettings, SettingsError, SettingsStore


LOGGER = logging.getLogger("seat_sentinel.tray")
PRESENCE_MODE_LABELS = {
    "ANY_FACE": "任意人脸",
    "REGISTERED_FACE": "仅本人（需注册）",
}
PRESENCE_MODE_VALUES = {
    label: value for value, label in PRESENCE_MODE_LABELS.items()
}
CAMERA_MONITORING_MODE_LABELS = {
    "CONTINUOUS": "持续监测",
    "IDLE_TRIGGERED": "键鼠空闲后监测",
}
CAMERA_MONITORING_MODE_VALUES = {
    label: value
    for value, label in CAMERA_MONITORING_MODE_LABELS.items()
}

GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
GA_ROOT = 2
GW_OWNER = 4
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1


class _CursorPoint(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def _native_top_level_handle(window: tk.Misc) -> int:
    """Return Tk's real Win32 top-level wrapper handle."""
    window.update_idletasks()
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    child_handle = wintypes.HWND(window.winfo_id())
    root_handle = user32.GetAncestor(child_handle, GA_ROOT)
    return int(root_handle or child_handle.value or 0)


def _get_window_long_ptr(handle: int, index: int) -> int:
    user32 = ctypes.windll.user32
    function = user32.GetWindowLongPtrW
    function.argtypes = [wintypes.HWND, ctypes.c_int]
    function.restype = ctypes.c_ssize_t
    return int(function(wintypes.HWND(handle), index))


def _set_window_long_ptr(handle: int, index: int, value: int) -> None:
    user32 = ctypes.windll.user32
    function = user32.SetWindowLongPtrW
    function.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    function.restype = ctypes.c_ssize_t
    ctypes.set_last_error(0)
    result = function(wintypes.HWND(handle), index, value)
    error_code = ctypes.get_last_error()
    if result == 0 and error_code != 0:
        raise OSError(error_code, "SetWindowLongPtrW failed")


def _configure_taskbar_window(window: tk.Misc) -> int:
    """Make a borderless Tk top-level an unowned taskbar application."""
    handle = _native_top_level_handle(window)
    if not handle:
        raise OSError("Unable to resolve the debug window handle")
    current_style = _get_window_long_ptr(handle, GWL_EXSTYLE)
    taskbar_style = (
        current_style | WS_EX_APPWINDOW
    ) & ~WS_EX_TOOLWINDOW
    _set_window_long_ptr(handle, GWLP_HWNDPARENT, 0)
    _set_window_long_ptr(handle, GWL_EXSTYLE, taskbar_style)

    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    flags = (
        SWP_NOSIZE
        | SWP_NOMOVE
        | SWP_NOZORDER
        | SWP_NOACTIVATE
        | SWP_FRAMECHANGED
    )
    if not user32.SetWindowPos(
        wintypes.HWND(handle),
        wintypes.HWND(0),
        0,
        0,
        0,
        0,
        flags,
    ):
        raise OSError("Unable to refresh the debug window taskbar style")
    return handle


def _taskbar_window_state(window: tk.Misc) -> tuple[bool, bool]:
    """Return whether APPWINDOW is set and the native owner is absent."""
    handle = _native_top_level_handle(window)
    style = _get_window_long_ptr(handle, GWL_EXSTYLE)
    user32 = ctypes.windll.user32
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    owner = user32.GetWindow(wintypes.HWND(handle), GW_OWNER)
    has_taskbar_style = bool(style & WS_EX_APPWINDOW) and not bool(
        style & WS_EX_TOOLWINDOW
    )
    return has_taskbar_style, not bool(owner)


def _configure_windows_app_identity() -> None:
    """Give Windows a stable identity for taskbar grouping."""
    shell32 = ctypes.windll.shell32
    function = shell32.SetCurrentProcessExplicitAppUserModelID
    function.argtypes = [wintypes.LPCWSTR]
    function.restype = ctypes.c_long
    result = int(function("ZheZZ.SeatSentinel"))
    if result < 0:
        raise OSError(result, "Unable to set the Windows application ID")


def _configure_overlay_window(window: tk.Misc) -> int:
    """Keep a notification visible without focus or a taskbar button."""
    handle = _native_top_level_handle(window)
    if not handle:
        raise OSError("Unable to resolve the warning overlay handle")
    current_style = _get_window_long_ptr(handle, GWL_EXSTYLE)
    overlay_style = (
        current_style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    ) & ~WS_EX_APPWINDOW
    _set_window_long_ptr(handle, GWL_EXSTYLE, overlay_style)

    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    flags = (
        SWP_NOSIZE
        | SWP_NOMOVE
        | SWP_NOACTIVATE
        | SWP_FRAMECHANGED
        | SWP_SHOWWINDOW
    )
    if not user32.SetWindowPos(
        wintypes.HWND(handle),
        wintypes.HWND(HWND_TOPMOST),
        0,
        0,
        0,
        0,
        flags,
    ):
        raise OSError("Unable to show the lock warning without focus")
    return handle


def _cursor_position() -> tuple[int, int]:
    """Read the system cursor without installing a global input hook."""
    point = _CursorPoint()
    user32 = ctypes.windll.user32
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_CursorPoint)]
    user32.GetCursorPos.restype = wintypes.BOOL
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise OSError("Unable to read the mouse cursor position")
    return int(point.x), int(point.y)


class MonitoringService:
    """Start, pause, resume, and restart the monitoring worker safely."""

    def __init__(self, settings_store: SettingsStore) -> None:
        self._settings_store = settings_store
        self._state_lock = threading.RLock()
        self._transition_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._status_state = "stopped"
        self._status_detail = "监控尚未启动"
        self._shutting_down = False
        self._debug_frame_buffer = DebugFrameBuffer()
        self._privacy_blur_signal = PrivacyBlurSignal()
        self._sedentary_reminder_signal = SedentaryReminderSignal()

    def status_detail(self) -> str:
        with self._state_lock:
            return self._status_detail

    def status_snapshot(self) -> tuple[str, str]:
        with self._state_lock:
            return self._status_state, self._status_detail

    def is_running(self) -> bool:
        with self._state_lock:
            return bool(
                self._thread is not None
                and self._thread.is_alive()
                and self._stop_event is not None
                and not self._stop_event.is_set()
            )

    def debug_snapshot(self) -> DebugFrameSnapshot:
        """Return the latest frame copied safely for the Tkinter thread."""
        return self._debug_frame_buffer.snapshot()

    def privacy_blur_snapshot(self) -> PrivacyBlurSnapshot:
        return self._privacy_blur_signal.snapshot()

    def sedentary_reminder_snapshot(self) -> SedentaryReminderSnapshot:
        return self._sedentary_reminder_signal.snapshot()

    def clear_sedentary_reminder(self) -> None:
        self._sedentary_reminder_signal.clear()

    def dismiss_privacy_blur(
        self,
        status_detail: str = "已通过甩动鼠标解除隐私模糊 · 继续监控",
    ) -> bool:
        dismissed = self._privacy_blur_signal.dismiss()
        if dismissed:
            self._update_status(
                "monitoring",
                status_detail,
            )
        return dismissed

    def clear_privacy_blur(self) -> None:
        self._privacy_blur_signal.clear()

    def _update_status(self, state: str, detail: str) -> None:
        with self._state_lock:
            if self._shutting_down:
                return
            self._status_state = state
            self._status_detail = detail

    def start_async(self) -> None:
        threading.Thread(
            target=self._transition,
            args=(True, False),
            name="seat-sentinel-start",
            daemon=True,
        ).start()

    def pause_async(self) -> None:
        self._privacy_blur_signal.clear()
        self._sedentary_reminder_signal.clear()
        self._update_status("pausing", "正在暂停并释放摄像头")
        self._debug_frame_buffer.clear(
            "监控正在暂停 · 调试画面已清空"
        )
        threading.Thread(
            target=self._transition,
            args=(False, False),
            name="seat-sentinel-pause",
            daemon=True,
        ).start()

    def pause_blocking(self) -> None:
        """Stop monitoring synchronously from a non-UI worker thread."""
        self._privacy_blur_signal.clear()
        self._sedentary_reminder_signal.clear()
        self._update_status("pausing", "正在暂停并释放摄像头")
        self._debug_frame_buffer.clear(
            "监控正在暂停 · 调试画面已清空"
        )
        self._transition(False, False)

    def restart_async(self) -> None:
        self._privacy_blur_signal.clear()
        self._sedentary_reminder_signal.clear()
        self._update_status("starting", "正在应用设置并重启监控")
        self._debug_frame_buffer.clear(
            "正在重新启动监控 · 调试画面已清空"
        )
        threading.Thread(
            target=self._transition,
            args=(True, True),
            name="seat-sentinel-restart",
            daemon=True,
        ).start()

    def _transition(self, should_run: bool, force_restart: bool) -> None:
        with self._transition_lock:
            with self._state_lock:
                current_thread = self._thread
                current_stop_event = self._stop_event

            if current_thread is not None and current_thread.is_alive():
                if should_run and not force_restart and self.is_running():
                    return
                if current_stop_event is not None:
                    current_stop_event.set()
                current_thread.join(timeout=30.0)
                if current_thread.is_alive():
                    self._update_status(
                        "error",
                        "监控线程未能及时停止，请退出后重试",
                    )
                    return

            with self._state_lock:
                self._thread = None
                self._stop_event = None

            if not should_run:
                self._privacy_blur_signal.clear()
                self._sedentary_reminder_signal.clear()
                self._update_status("paused", "监控已暂停 · 摄像头已释放")
                self._debug_frame_buffer.clear(
                    "监控已暂停 · 调试画面已清空"
                )
                return

            try:
                settings = self._settings_store.load()
                settings.apply_to_runtime()
            except SettingsError as exc:
                self._update_status("error", f"设置错误：{exc}")
                return

            stop_event = threading.Event()
            worker = threading.Thread(
                target=self._worker,
                args=(stop_event,),
                name="seat-sentinel-monitor",
                daemon=True,
            )
            with self._state_lock:
                if self._shutting_down:
                    return
                self._stop_event = stop_event
                self._thread = worker
                self._status_state = "starting"
                self._status_detail = "正在启动监控"
            worker.start()

    def _worker(self, stop_event: threading.Event) -> None:
        exit_code = monitoring.run(
            stop_event=stop_event,
            status_callback=self._update_status,
            debug_frame_buffer=self._debug_frame_buffer,
            privacy_blur_signal=self._privacy_blur_signal,
            sedentary_reminder_signal=self._sedentary_reminder_signal,
        )
        with self._state_lock:
            if self._thread is threading.current_thread():
                if not stop_event.is_set() and exit_code != 0:
                    self._status_state = "error"
                    if self._status_detail == "监控已停止":
                        self._status_detail = "监控异常退出"

    def shutdown(self) -> None:
        with self._transition_lock:
            with self._state_lock:
                self._shutting_down = True
                stop_event = self._stop_event
                worker = self._thread
            if stop_event is not None:
                stop_event.set()
            if worker is not None and worker.is_alive():
                worker.join(timeout=30.0)
            self._privacy_blur_signal.clear()
            self._sedentary_reminder_signal.clear()
            self._debug_frame_buffer.clear(
                "程序正在退出 · 调试画面已清空"
            )


class TrayApplication:
    """Own the Tk settings window and Windows tray icon."""

    def __init__(
        self,
        debug_signal: Optional[DebugWindowSignal] = None,
        show_debug_on_start: bool = False,
    ) -> None:
        try:
            _configure_windows_app_identity()
        except OSError as exc:
            LOGGER.warning("Unable to set Windows taskbar identity: %s", exc)
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.protocol("WM_DELETE_WINDOW", self._root.withdraw)
        self._settings_window: Optional[tk.Toplevel] = None
        self._settings_privacy_blur_variable: Optional[
            tk.BooleanVar
        ] = None
        self._registration_window: Optional[tk.Toplevel] = None
        self._registration_cancel_event: Optional[threading.Event] = None
        self._registration_queue: Optional[
            queue.Queue[tuple[str, object]]
        ] = None
        self._registration_status_variable: Optional[tk.StringVar] = None
        self._registration_progress: Optional[ttk.Progressbar] = None
        self._registration_preview_label: Optional[tk.Label] = None
        self._registration_cancel_button: Optional[ttk.Button] = None
        self._registration_photo_image: Optional[ImageTk.PhotoImage] = None
        self._registration_resume_after = False
        self._lock_warning_window: Optional[tk.Toplevel] = None
        self._lock_warning_detail_variable: Optional[tk.StringVar] = None
        self._lock_warning_opacity = 0.0
        self._sedentary_reminder_window: Optional[tk.Toplevel] = None
        self._sedentary_reminder_detail_variable: Optional[
            tk.StringVar
        ] = None
        self._sedentary_reminder_last_sequence = 0
        self._sedentary_reminder_started_at: Optional[float] = None
        self._sedentary_reminder_opacity = 0.0
        self._privacy_blur_overlay = DwmPrivacyOverlay(
            config.PRIVACY_ACRYLIC_STRENGTH_PERCENT
        )
        self._privacy_blur_next_monitor_check_at = 0.0
        self._privacy_blur_last_sequence = -1
        self._manual_privacy_blur_active = False
        self._privacy_mouse_shake = MouseShakeDetector()
        self._privacy_cursor_error_logged = False
        self._privacy_hotkey_events: queue.SimpleQueue[bool] = (
            queue.SimpleQueue()
        )
        self._privacy_hotkey: Optional[WindowsGlobalHotkey] = None
        self._debug_window: Optional[tk.Toplevel] = None
        self._debug_status_variable: Optional[tk.StringVar] = None
        self._debug_metric_variables: dict[str, tk.StringVar] = {}
        self._debug_metric_labels: dict[str, tk.Label] = {}
        self._debug_guide_variables: dict[str, tk.StringVar] = {}
        self._debug_status_badge: Optional[tk.Label] = None
        self._debug_toggle_button: Optional[tk.Button] = None
        self._debug_device_toggle_button: Optional[tk.Button] = None
        self._debug_exit_button: Optional[tk.Button] = None
        self._debug_camera_variable: Optional[tk.StringVar] = None
        self._debug_camera_combobox: Optional[ttk.Combobox] = None
        self._debug_canvas: Optional[tk.Canvas] = None
        self._debug_photo_image: Optional[ImageTk.PhotoImage] = None
        self._debug_window_icon: Optional[ImageTk.PhotoImage] = None
        self._debug_header_icon: Optional[ImageTk.PhotoImage] = None
        self._debug_last_frame_sequence = -1
        self._debug_last_frame_time: Optional[float] = None
        self._debug_preview_fps = 0.0
        self._debug_drag_offset_x = 0
        self._debug_drag_offset_y = 0
        self._debug_signal = debug_signal
        self._show_debug_on_start = show_debug_on_start
        self._shutdown_started = threading.Event()
        self._settings_store = SettingsStore()
        self._face_template_store = FaceTemplateStore(
            config.FACE_TEMPLATE_PATH
        )
        self._service = MonitoringService(self._settings_store)
        self._tray_locked_image = self._create_icon_image(locked=True)
        self._tray_unlocked_image = self._create_icon_image(locked=False)
        self._tray_visual_state: Optional[bool] = None
        self._tray_icon = pystray.Icon(
            "seat_sentinel",
            self._tray_unlocked_image,
            config.APPLICATION_TITLE,
            menu=pystray.Menu(
                pystray.MenuItem(
                    lambda item: (
                        f"状态：{self._service.status_detail()}"
                    ),
                    lambda icon, item: None,
                    enabled=False,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "运行状态 / 调试",
                    self._show_debug_from_tray,
                    default=True,
                ),
                pystray.MenuItem(
                    "暂停监控",
                    self._pause,
                    enabled=lambda item: self._service.is_running(),
                ),
                pystray.MenuItem(
                    "恢复监控",
                    self._resume,
                    enabled=lambda item: not self._service.is_running(),
                ),
                pystray.MenuItem(
                    "多人脸隐私模糊",
                    self._toggle_privacy_blur_from_tray,
                    checked=lambda item: bool(
                        config.PRIVACY_BLUR_ENABLED
                    ),
                    enabled=lambda item: (
                        config.CAMERA_MONITORING_MODE == "CONTINUOUS"
                    ),
                ),
                pystray.MenuItem(
                    lambda item: (
                        "手动毛玻璃"
                        f"（{config.PRIVACY_BLUR_HOTKEY}）"
                    ),
                    self._toggle_manual_privacy_blur_from_tray,
                    checked=lambda item: self._manual_privacy_blur_active,
                ),
                pystray.MenuItem(
                    "设置",
                    self._show_settings_from_tray,
                ),
                pystray.MenuItem(
                    "注册 / 更新本人",
                    self._show_registration_from_tray,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._exit_from_tray),
            ),
        )

    @staticmethod
    def _create_icon_image(locked: bool = True) -> Image.Image:
        try:
            with Image.open(config.APPLICATION_ICON_PNG_PATH) as source:
                image = source.convert("RGBA").resize(
                    (64, 64),
                    Image.Resampling.LANCZOS,
                )
        except (OSError, ValueError):
            LOGGER.warning(
                "Unable to load application icon from %s; using fallback",
                config.APPLICATION_ICON_PNG_PATH,
            )
            return TrayApplication._create_fallback_icon_image(locked)

        if locked:
            return image

        paused_image = ImageEnhance.Color(image).enhance(0.10)
        paused_image = ImageEnhance.Brightness(paused_image).enhance(0.72)
        draw = ImageDraw.Draw(paused_image)
        draw.ellipse(
            (44, 44, 62, 62),
            fill=(15, 23, 42, 255),
        )
        draw.ellipse(
            (47, 47, 59, 59),
            fill=(245, 166, 35, 255),
        )
        return paused_image

    @staticmethod
    def _create_fallback_icon_image(
        locked: bool = True,
    ) -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        lock_color = (
            (31, 111, 235, 255)
            if locked
            else (245, 166, 35, 255)
        )
        draw.rounded_rectangle(
            (8, 26, 56, 58),
            radius=8,
            fill=lock_color,
        )
        if locked:
            draw.arc(
                (18, 5, 46, 39),
                start=180,
                end=360,
                fill=lock_color,
                width=8,
            )
        else:
            draw.arc(
                (26, 4, 54, 34),
                start=180,
                end=360,
                fill=lock_color,
                width=8,
            )
            draw.line(
                (54, 19, 54, 31),
                fill=lock_color,
                width=8,
            )
        draw.ellipse((28, 37, 36, 45), fill=(255, 255, 255, 255))
        draw.rectangle(
            (31, 43, 33, 51),
            fill=(255, 255, 255, 255),
        )
        return image

    def run(self) -> None:
        hotkey_warning: Optional[str] = None
        try:
            settings = self._settings_store.load()
            settings.apply_to_runtime()
        except SettingsError as exc:
            messagebox.showerror(config.APPLICATION_TITLE, str(exc))
            self._root.destroy()
            return

        try:
            self._replace_privacy_hotkey(settings.privacy_blur_hotkey)
        except GlobalHotkeyError as exc:
            hotkey_warning = str(exc)
            LOGGER.warning("Privacy blur hotkey is unavailable: %s", exc)

        self._tray_icon.run_detached()
        self._service.start_async()
        self._root.after(1000, self._refresh_tray_menu)
        self._root.after(100, self._refresh_lock_warning)
        self._root.after(50, self._refresh_sedentary_reminder)
        self._root.after(80, self._refresh_privacy_blur)
        self._root.after(50, self._poll_privacy_hotkey)
        self._root.after(250, self._poll_debug_signal)
        if hotkey_warning is not None:
            self._root.after(
                300,
                lambda detail=hotkey_warning: messagebox.showwarning(
                    "毛玻璃快捷键未启用",
                    detail + "。请在设置中换一个组合键。",
                ),
            )
        if self._show_debug_on_start:
            self._root.after(250, self._show_debug_window)
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            LOGGER.info("Ctrl+C received; closing tray application")
        finally:
            self._stop_privacy_hotkey()
            self._manual_privacy_blur_active = False
            self._hide_privacy_blur()
            self._service.shutdown()
            self._tray_icon.stop()
            try:
                self._root.destroy()
            except tk.TclError:
                pass

    def _refresh_tray_menu(self) -> None:
        try:
            self._refresh_tray_visual()
            self._tray_icon.update_menu()
        except Exception as exc:
            LOGGER.debug("Unable to refresh tray menu", exc_info=exc)
        try:
            self._root.after(1000, self._refresh_tray_menu)
        except tk.TclError:
            pass

    def _refresh_tray_visual(self) -> None:
        """Match the tray lock symbol to the monitoring state."""
        monitoring_running = self._service.is_running()
        if monitoring_running == self._tray_visual_state:
            return
        self._tray_icon.icon = (
            self._tray_locked_image
            if monitoring_running
            else self._tray_unlocked_image
        )
        self._tray_icon.title = (
            f"{config.APPLICATION_TITLE} · "
            f"{'监控运行中' if monitoring_running else '监控已暂停'}"
        )
        self._tray_visual_state = monitoring_running

    def _ensure_lock_warning_window(self) -> tk.Toplevel:
        window = self._lock_warning_window
        if window is not None and window.winfo_exists():
            return window

        window = tk.Toplevel(self._root)
        self._lock_warning_window = window
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0.0)
        window.configure(background="#0F172A")

        width = 500
        height = 122
        x = max(16, (window.winfo_screenwidth() - width) // 2)
        window.geometry(f"{width}x{height}+{x}+36")

        card = tk.Frame(
            window,
            background="#0F172A",
            highlightbackground="#1E6A78",
            highlightthickness=1,
            padx=22,
            pady=16,
        )
        card.pack(fill="both", expand=True)
        accent = tk.Frame(card, background="#25D4E8", width=4)
        accent.pack(side="left", fill="y", padx=(0, 18))
        content = tk.Frame(card, background="#0F172A")
        content.pack(side="left", fill="both", expand=True)
        tk.Label(
            content,
            text="即将自动锁屏",
            background="#0F172A",
            foreground="#F8FAFC",
            font=("Microsoft YaHei UI", 15, "bold"),
            anchor="w",
        ).pack(fill="x")
        self._lock_warning_detail_variable = tk.StringVar(
            value="5 秒后自动锁屏"
        )
        tk.Label(
            content,
            textvariable=self._lock_warning_detail_variable,
            background="#0F172A",
            foreground="#67E8F9",
            font=("Microsoft YaHei UI", 12, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(3, 0))
        tk.Label(
            content,
            text="移动鼠标、按下键盘或回到镜头前即可取消",
            background="#0F172A",
            foreground="#94A3B8",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        window.update_idletasks()
        try:
            _configure_overlay_window(window)
        except OSError as exc:
            LOGGER.warning(
                "Unable to configure non-activating lock warning: %s",
                exc,
            )
        window.withdraw()
        return window

    def _refresh_lock_warning(self, schedule_next: bool = True) -> None:
        try:
            state, detail = self._service.status_snapshot()
            warning_active = state == "lock_warning"
            window = self._lock_warning_window
            if warning_active:
                window = self._ensure_lock_warning_window()
                if self._lock_warning_detail_variable is not None:
                    self._lock_warning_detail_variable.set(detail)
                if not window.winfo_viewable():
                    window.deiconify()
                    try:
                        _configure_overlay_window(window)
                    except OSError as exc:
                        LOGGER.warning(
                            "Unable to show lock warning without focus: %s",
                            exc,
                        )
                self._lock_warning_opacity = min(
                    0.94,
                    self._lock_warning_opacity + 0.16,
                )
            elif window is not None and window.winfo_exists():
                self._lock_warning_opacity = max(
                    0.0,
                    self._lock_warning_opacity - 0.20,
                )
                if self._lock_warning_opacity == 0.0:
                    window.withdraw()

            if window is not None and window.winfo_exists():
                window.attributes("-alpha", self._lock_warning_opacity)
        except tk.TclError:
            return
        finally:
            if schedule_next:
                try:
                    self._root.after(80, self._refresh_lock_warning)
                except tk.TclError:
                    pass

    def _ensure_sedentary_reminder_window(self) -> tk.Toplevel:
        window = self._sedentary_reminder_window
        if window is not None and window.winfo_exists():
            return window

        window = tk.Toplevel(self._root)
        self._sedentary_reminder_window = window
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0.0)
        window.configure(background="#0F172A")

        width = 500
        height = 96
        x = max(16, (window.winfo_screenwidth() - width) // 2)
        window.geometry(f"{width}x{height}+{x}+36")

        card = tk.Frame(
            window,
            background="#0F172A",
            highlightbackground="#1E6A78",
            highlightthickness=1,
            padx=22,
            pady=14,
        )
        card.pack(fill="both", expand=True)
        accent = tk.Frame(card, background="#25D4E8", width=4)
        accent.pack(side="left", fill="y", padx=(0, 18))
        content = tk.Frame(card, background="#0F172A")
        content.pack(side="left", fill="both", expand=True)
        tk.Label(
            content,
            text="久坐提醒",
            background="#0F172A",
            foreground="#F8FAFC",
            font=("Microsoft YaHei UI", 15, "bold"),
            anchor="w",
        ).pack(fill="x")
        self._sedentary_reminder_detail_variable = tk.StringVar(
            value="已经连续坐了 30 分钟，请注意起身活动"
        )
        tk.Label(
            content,
            textvariable=self._sedentary_reminder_detail_variable,
            background="#0F172A",
            foreground="#67E8F9",
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        window.update_idletasks()
        try:
            _configure_overlay_window(window)
        except OSError as exc:
            LOGGER.warning(
                "Unable to configure non-activating sedentary reminder: %s",
                exc,
            )
        window.withdraw()
        return window

    def _hide_sedentary_reminder(self) -> None:
        self._sedentary_reminder_started_at = None
        self._sedentary_reminder_opacity = 0.0
        window = self._sedentary_reminder_window
        if window is not None and window.winfo_exists():
            window.attributes("-alpha", 0.0)
            window.withdraw()

    def _refresh_sedentary_reminder(
        self,
        schedule_next: bool = True,
    ) -> None:
        try:
            snapshot = self._service.sedentary_reminder_snapshot()
            now = time.monotonic()
            if snapshot.sequence != self._sedentary_reminder_last_sequence:
                self._sedentary_reminder_last_sequence = snapshot.sequence
                if (
                    snapshot.sequence > 0
                    and snapshot.triggered_at is not None
                    and snapshot.detail
                ):
                    window = self._ensure_sedentary_reminder_window()
                    if self._sedentary_reminder_detail_variable is not None:
                        self._sedentary_reminder_detail_variable.set(
                            snapshot.detail
                        )
                    self._sedentary_reminder_started_at = now
                    self._sedentary_reminder_opacity = 0.16
                    window.deiconify()
                    try:
                        _configure_overlay_window(window)
                    except OSError as exc:
                        LOGGER.warning(
                            "Unable to show sedentary reminder without "
                            "focus: %s",
                            exc,
                        )
                else:
                    self._hide_sedentary_reminder()

            window = self._sedentary_reminder_window
            started_at = self._sedentary_reminder_started_at
            if (
                window is not None
                and window.winfo_exists()
                and started_at is not None
            ):
                elapsed = max(0.0, now - started_at)
                duration = config.SEDENTARY_REMINDER_DISPLAY_SECONDS
                fade_in_seconds = min(0.18, duration / 3.0)
                fade_out_seconds = min(0.22, duration / 3.0)
                if elapsed >= duration:
                    self._sedentary_reminder_opacity = 0.0
                    self._sedentary_reminder_started_at = None
                    window.attributes("-alpha", 0.0)
                    window.withdraw()
                else:
                    if elapsed < fade_in_seconds:
                        opacity = 0.94 * max(
                            0.17,
                            elapsed / fade_in_seconds,
                        )
                    elif elapsed > duration - fade_out_seconds:
                        opacity = 0.94 * (
                            (duration - elapsed) / fade_out_seconds
                        )
                    else:
                        opacity = 0.94
                    self._sedentary_reminder_opacity = max(
                        0.0,
                        min(0.94, opacity),
                    )
                    window.attributes(
                        "-alpha",
                        self._sedentary_reminder_opacity,
                    )
        except tk.TclError:
            return
        finally:
            if schedule_next:
                try:
                    self._root.after(
                        50,
                        self._refresh_sedentary_reminder,
                    )
                except tk.TclError:
                    pass

    def _show_privacy_blur(
        self,
        snapshot: PrivacyBlurSnapshot,
    ) -> None:
        self._hide_privacy_blur()
        info = self._privacy_blur_overlay.show()
        self._privacy_blur_last_sequence = snapshot.sequence
        self._privacy_blur_next_monitor_check_at = time.monotonic() + 1.0

        try:
            cursor_x, cursor_y = _cursor_position()
            self._privacy_mouse_shake.reset(cursor_x, cursor_y)
            self._privacy_cursor_error_logged = False
        except OSError as exc:
            self._privacy_mouse_shake.reset()
            if not self._privacy_cursor_error_logged:
                LOGGER.warning(
                    "Unable to initialize the privacy mouse gesture: %s",
                    exc,
                )
                self._privacy_cursor_error_logged = True

        LOGGER.info(
            "DWM Desktop Acrylic privacy layer is visible across %d "
            "monitor(s) at %d%% strength",
            len(info.work_areas),
            info.strength_percent,
        )

    def _hide_privacy_blur(self) -> None:
        self._privacy_blur_overlay.hide()
        self._privacy_mouse_shake.reset()

    def _refresh_privacy_blur(self, schedule_next: bool = True) -> None:
        next_delay = 80
        try:
            snapshot = self._service.privacy_blur_snapshot()
            overlay_visible = self._privacy_blur_overlay.is_visible()
            blur_active = bool(
                snapshot.active or self._manual_privacy_blur_active
            )
            if blur_active:
                next_delay = config.PRIVACY_MOUSE_POLL_MILLISECONDS
                if (
                    not overlay_visible
                    or snapshot.sequence != self._privacy_blur_last_sequence
                ):
                    self._show_privacy_blur(snapshot)
                elif (
                    time.monotonic()
                    >= self._privacy_blur_next_monitor_check_at
                ):
                    if self._privacy_blur_overlay.display_geometry_changed():
                        self._show_privacy_blur(snapshot)
                    else:
                        self._privacy_blur_next_monitor_check_at = (
                            time.monotonic() + 1.0
                        )

                try:
                    cursor_x, cursor_y = _cursor_position()
                    if self._privacy_mouse_shake.record(
                        cursor_x,
                        cursor_y,
                    ):
                        self._manual_privacy_blur_active = False
                        if self._service.dismiss_privacy_blur():
                            LOGGER.info(
                                "Privacy blur dismissed by a mouse shake"
                            )
                        self._hide_privacy_blur()
                        self._refresh_manual_privacy_menu()
                    self._privacy_cursor_error_logged = False
                except OSError as exc:
                    if not self._privacy_cursor_error_logged:
                        LOGGER.warning(
                            "Unable to read the privacy mouse gesture: %s",
                            exc,
                        )
                        self._privacy_cursor_error_logged = True
            elif overlay_visible:
                self._hide_privacy_blur()
        except (DwmPrivacyError, OSError, tk.TclError, ValueError) as exc:
            LOGGER.error("Unable to maintain the DWM privacy blur: %s", exc)
            self._manual_privacy_blur_active = False
            self._hide_privacy_blur()
        finally:
            if schedule_next:
                try:
                    self._root.after(
                        max(10, next_delay),
                        self._refresh_privacy_blur,
                    )
                except tk.TclError:
                    pass

    def _poll_debug_signal(self) -> None:
        try:
            if (
                self._debug_signal is not None
                and self._debug_signal.consume_request()
            ):
                self._show_debug_window()
        except SingleInstanceError as exc:
            LOGGER.warning("Unable to read debug-window signal: %s", exc)
        try:
            self._root.after(250, self._poll_debug_signal)
        except tk.TclError:
            pass

    def _pause(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._service.pause_async()

    def _resume(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._service.start_async()

    def _toggle_privacy_blur_from_tray(
        self,
        icon: pystray.Icon,
        item: pystray.MenuItem,
    ) -> None:
        self._root.after(0, self._toggle_privacy_blur_setting)

    def _toggle_manual_privacy_blur_from_tray(
        self,
        icon: pystray.Icon,
        item: pystray.MenuItem,
    ) -> None:
        self._root.after(0, self._toggle_manual_privacy_blur)

    def _toggle_manual_privacy_blur(self) -> None:
        snapshot = self._service.privacy_blur_snapshot()
        overlay_active = bool(
            self._manual_privacy_blur_active or snapshot.active
        )
        if overlay_active:
            self._manual_privacy_blur_active = False
            self._service.dismiss_privacy_blur(
                "已通过快捷键解除隐私模糊 · 继续监控"
            )
            self._hide_privacy_blur()
            LOGGER.info("Privacy blur dismissed manually")
        else:
            self._manual_privacy_blur_active = True
            self._refresh_privacy_blur(schedule_next=False)
            LOGGER.info("Privacy blur activated manually")
        self._refresh_manual_privacy_menu()

    def _refresh_manual_privacy_menu(self) -> None:
        try:
            self._tray_icon.update_menu()
        except Exception as exc:
            LOGGER.debug(
                "Unable to update the manual privacy-blur menu item",
                exc_info=exc,
            )

    def _enqueue_privacy_hotkey(self) -> None:
        self._privacy_hotkey_events.put(True)

    def _poll_privacy_hotkey(self) -> None:
        try:
            while True:
                self._privacy_hotkey_events.get_nowait()
                self._toggle_manual_privacy_blur()
        except queue.Empty:
            pass
        try:
            self._root.after(50, self._poll_privacy_hotkey)
        except tk.TclError:
            pass

    def _replace_privacy_hotkey(self, hotkey: str) -> None:
        current = self._privacy_hotkey
        if current is not None and current.hotkey == hotkey:
            return
        candidate = WindowsGlobalHotkey(self._enqueue_privacy_hotkey)
        candidate.start(hotkey)
        self._privacy_hotkey = candidate
        if current is not None:
            current.stop()
        LOGGER.info("Privacy blur hotkey registered: %s", hotkey)

    def _stop_privacy_hotkey(self) -> None:
        hotkey = self._privacy_hotkey
        self._privacy_hotkey = None
        if hotkey is not None:
            hotkey.stop()

    def _toggle_privacy_blur_setting(self) -> None:
        was_running = self._service.is_running()
        try:
            current = self._settings_store.load()
            if current.camera_monitoring_mode != "CONTINUOUS":
                LOGGER.info(
                    "Privacy blur toggle ignored because idle-triggered "
                    "camera monitoring is selected"
                )
                return
            updated = replace(
                current,
                privacy_blur_enabled=not current.privacy_blur_enabled,
            )
            self._settings_store.save(updated)
            updated.apply_to_runtime()
        except SettingsError as exc:
            messagebox.showerror("设置错误", str(exc))
            return

        self._service.clear_privacy_blur()
        if not getattr(self, "_manual_privacy_blur_active", False):
            self._hide_privacy_blur()
        variable = self._settings_privacy_blur_variable
        if variable is not None:
            try:
                variable.set(updated.privacy_blur_enabled)
            except tk.TclError:
                self._settings_privacy_blur_variable = None
        if was_running:
            self._service.restart_async()
        try:
            self._tray_icon.update_menu()
        except Exception as exc:
            LOGGER.debug(
                "Unable to update the privacy toggle in the tray menu",
                exc_info=exc,
            )
        LOGGER.info(
            "Multi-face privacy blur %s from the tray menu",
            "enabled" if updated.privacy_blur_enabled else "disabled",
        )

    def _show_debug_from_tray(
        self,
        icon: pystray.Icon,
        item: pystray.MenuItem,
    ) -> None:
        self._root.after(0, self._show_debug_window)

    def _show_settings_from_tray(
        self,
        icon: pystray.Icon,
        item: pystray.MenuItem,
    ) -> None:
        self._root.after(0, self._show_settings)

    def _show_registration_from_tray(
        self,
        icon: pystray.Icon,
        item: pystray.MenuItem,
    ) -> None:
        self._root.after(0, self._start_face_registration)

    def _show_debug_window(self) -> None:
        if (
            self._debug_window is not None
            and self._debug_window.winfo_exists()
        ):
            self._show_debug_taskbar_entry(self._debug_window)
            self._debug_window.lift()
            self._debug_window.focus_force()
            return

        window = tk.Toplevel(self._root)
        self._debug_window = window
        window.title(f"{config.APPLICATION_TITLE} · 本地隐私守护")
        window.resizable(False, False)
        window.withdraw()
        window.overrideredirect(True)
        self._debug_window_icon = ImageTk.PhotoImage(
            self._tray_locked_image,
            master=window,
        )
        self._debug_header_icon = ImageTk.PhotoImage(
            self._tray_locked_image.resize(
                (40, 40),
                Image.Resampling.LANCZOS,
            ),
            master=window,
        )
        window.iconphoto(False, self._debug_window_icon)
        window.configure(background="#c8d5e6")
        window.protocol("WM_DELETE_WINDOW", self._close_debug_window)
        window.bind("<Escape>", lambda event: self._close_debug_window())
        window.bind("<Alt-F4>", lambda event: self._close_debug_window())

        shell = tk.Frame(window, background="#f4f7fb")
        shell.grid(
            row=0,
            column=0,
            padx=1,
            pady=1,
            sticky="nsew",
        )
        content = tk.Frame(shell, background="#f4f7fb")
        content.grid(
            row=0,
            column=0,
            padx=20,
            pady=16,
            sticky="nsew",
        )

        header = tk.Frame(content, background="#ffffff")
        header.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(0, 14),
        )
        header.grid_columnconfigure(0, weight=1)

        title_area = tk.Frame(header, background="#ffffff")
        title_area.grid(row=0, column=0, padx=(10, 0), sticky="w")
        tk.Label(
            title_area,
            image=self._debug_header_icon,
            background="#ffffff",
            borderwidth=0,
        ).pack(side="left", padx=(0, 10))
        title_label = tk.Label(
            title_area,
            text=config.APPLICATION_TITLE,
            background="#ffffff",
            foreground="#14263d",
            font=("Segoe UI", 19, "bold"),
        )
        title_label.pack(side="left")
        console_label = tk.Label(
            title_area,
            text="  │  本地隐私守护",
            background="#ffffff",
            foreground="#6d7d91",
            font=("Microsoft YaHei UI", 10),
        )
        console_label.pack(side="left", pady=(5, 0))
        self._debug_status_badge = tk.Label(
            header,
            text="● 初始化",
            background="#fff8e6",
            foreground="#b7791f",
            padx=13,
            pady=6,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self._debug_status_badge.grid(
            row=0,
            column=1,
            padx=(12, 10),
            sticky="e",
        )
        minimize_button = self._create_titlebar_button(
            header,
            text="—",
            command=self._minimize_debug_window,
            hover_background="#e8eef7",
        )
        minimize_button.grid(row=0, column=2, padx=(0, 2))
        close_button = self._create_titlebar_button(
            header,
            text="×",
            command=self._close_debug_window,
            hover_background="#c42b3a",
        )
        close_button.grid(row=0, column=3)

        for drag_widget in (
            header,
            title_area,
            title_label,
            console_label,
            self._debug_status_badge,
        ):
            self._bind_debug_window_drag(drag_widget)

        vision_column = tk.Frame(content, background="#f4f7fb")
        vision_column.grid(
            row=1,
            column=0,
            padx=(0, 16),
            sticky="n",
        )

        preview_card = tk.Frame(
            vision_column,
            background="#ffffff",
            highlightthickness=1,
            highlightbackground="#d8e2ef",
            padx=8,
            pady=8,
        )
        preview_card.grid(row=0, column=0, sticky="n")
        self._debug_canvas = tk.Canvas(
            preview_card,
            width=640,
            height=360,
            background="#e8edf3",
            highlightthickness=0,
        )
        self._debug_canvas.pack()
        self._debug_canvas.create_text(
            320,
            180,
            text="等待摄像头画面",
            fill="#7b8ba0",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self._create_debug_guide_panel(vision_column).grid(
            row=1,
            column=0,
            pady=(14, 0),
            sticky="ew",
        )

        telemetry = tk.Frame(content, background="#f4f7fb", width=306)
        telemetry.grid(row=1, column=1, sticky="ns")
        telemetry.grid_propagate(False)
        self._debug_metric_variables = {}
        self._debug_metric_labels = {}
        self._create_debug_metric_card(
            telemetry,
            "人脸检测",
            "◎",
            (
                ("人脸数量", "face_count"),
                ("检测 / 本人", "confidence"),
                ("推理耗时", "inference"),
                ("预览刷新率", "preview_fps"),
            ),
        ).pack(fill="x", pady=(0, 8))
        self._create_debug_metric_card(
            telemetry,
            "锁屏判断",
            "▣",
            (
                ("离席持续", "face_absent"),
                ("键鼠空闲", "input_idle"),
                ("宽限期", "startup_grace"),
                ("锁屏条件", "lock_condition"),
            ),
        ).pack(fill="x", pady=(0, 8))
        self._create_debug_metric_card(
            telemetry,
            "系统信息",
            "▤",
            (
                ("推理设备", "device"),
                ("画面规格", "resolution"),
                ("画面延迟", "frame_age"),
                ("摄像头", "camera"),
            ),
        ).pack(fill="x")

        status_panel = tk.Frame(
            content,
            background="#ffffff",
            highlightthickness=1,
            highlightbackground="#d8e2ef",
        )
        status_panel.grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(12, 10),
        )
        tk.Label(
            status_panel,
            text="●  状态",
            background="#ffffff",
            foreground="#2eaf5d",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(12, 10), pady=9)
        self._debug_status_variable = tk.StringVar(
            value="正在初始化监控"
        )
        tk.Label(
            status_panel,
            textvariable=self._debug_status_variable,
            background="#ffffff",
            foreground="#4d6078",
            anchor="w",
            wraplength=570,
            justify="left",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", fill="x", expand=True, pady=9)
        tk.Label(
            status_panel,
            text=(
                f"v{config.APPLICATION_VERSION}  ·  本地处理模式"
            ),
            background="#ffffff",
            foreground="#8291a4",
            font=("Microsoft YaHei UI", 8),
        ).pack(side="right", padx=12, pady=9)

        controls = tk.Frame(content, background="#f4f7fb")
        controls.grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="ew",
        )
        controls.grid_columnconfigure(1, weight=1)

        camera_controls = tk.Frame(
            controls,
            background="#f4f7fb",
        )
        camera_controls.grid(row=0, column=0, sticky="w")
        tk.Label(
            camera_controls,
            text="摄像头",
            background="#f4f7fb",
            foreground="#31465f",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(0, 7))
        try:
            debug_settings = self._settings_store.load()
            selected_camera = debug_settings.camera_name
        except SettingsError:
            selected_camera = config.PREFERRED_CAMERA_NAME
        camera_names = self._camera_names()
        if selected_camera and selected_camera not in camera_names:
            camera_names.insert(0, selected_camera)
        self._debug_camera_variable = tk.StringVar(
            value=selected_camera
        )
        debug_style = ttk.Style(window)
        debug_style.configure(
            "SeatSentinel.TCombobox",
            fieldbackground="#ffffff",
            background="#ffffff",
            foreground="#31465f",
            arrowcolor="#63758a",
            bordercolor="#cfdbea",
            lightcolor="#cfdbea",
            darkcolor="#cfdbea",
            padding=6,
        )
        debug_style.map(
            "SeatSentinel.TCombobox",
            fieldbackground=[("readonly", "#ffffff")],
            foreground=[("readonly", "#31465f")],
        )
        self._debug_camera_combobox = ttk.Combobox(
            camera_controls,
            textvariable=self._debug_camera_variable,
            values=camera_names,
            state="readonly",
            width=23,
            style="SeatSentinel.TCombobox",
            postcommand=self._refresh_debug_camera_choices,
        )
        self._debug_camera_combobox.pack(side="left")
        self._debug_camera_combobox.bind(
            "<<ComboboxSelected>>",
            self._on_debug_camera_selected,
        )

        buttons = tk.Frame(controls, background="#f4f7fb")
        buttons.grid(row=0, column=1, sticky="e")
        self._create_debug_button(
            buttons,
            text="打开日志",
            command=self._open_log_directory,
        ).pack(side="left", padx=(0, 8))
        self._create_debug_button(
            buttons,
            text="参数设置",
            command=self._show_settings,
        ).pack(side="left", padx=(0, 8))
        self._debug_toggle_button = self._create_debug_button(
            buttons,
            text="暂停监控",
            command=self._toggle_monitoring,
            primary=True,
        )
        self._debug_toggle_button.pack(
            side="left",
            padx=(0, 8),
        )
        self._debug_device_toggle_button = self._create_debug_button(
            buttons,
            text="切换推理设备",
            command=self._toggle_inference_device,
        )
        self._debug_device_toggle_button.pack(side="left", padx=(0, 8))
        self._debug_exit_button = self._create_debug_button(
            buttons,
            text="彻底退出程序",
            command=self._exit_from_debug,
            danger=True,
        )
        self._debug_exit_button.pack(side="left")

        self._refresh_debug_window()
        window.update_idletasks()
        x = max(
            0,
            (window.winfo_screenwidth() - window.winfo_width()) // 2,
        )
        y = max(
            0,
            (window.winfo_screenheight() - window.winfo_height()) // 3,
        )
        window.geometry(f"+{x}+{y}")
        self._show_debug_taskbar_entry(window)
        window.lift()
        window.focus_force()

    @staticmethod
    def _show_debug_taskbar_entry(window: tk.Toplevel) -> None:
        """Map the custom window with a visible Windows taskbar button."""
        window.withdraw()
        window.update_idletasks()
        try:
            _configure_taskbar_window(window)
        except OSError as exc:
            LOGGER.warning(
                "Unable to apply the custom taskbar style; "
                "falling back to the native title bar: %s",
                exc,
            )
            window.overrideredirect(False)
        window.deiconify()

    @staticmethod
    def _create_titlebar_button(
        parent: tk.Misc,
        text: str,
        command: Callable[[], object],
        hover_background: str,
    ) -> tk.Button:
        normal_background = "#ffffff"
        button = tk.Button(
            parent,
            text=text,
            command=command,
            background=normal_background,
            foreground="#25384f",
            activebackground=hover_background,
            activeforeground=(
                "#ffffff"
                if hover_background == "#c42b3a"
                else "#25384f"
            ),
            relief="flat",
            borderwidth=0,
            width=3,
            pady=4,
            cursor="hand2",
            takefocus=False,
            font=("Segoe UI", 12),
        )
        button.bind(
            "<Enter>",
            lambda event: button.configure(
                background=hover_background
            ),
        )
        button.bind(
            "<Leave>",
            lambda event: button.configure(
                background=normal_background
            ),
        )
        return button

    def _bind_debug_window_drag(self, widget: tk.Misc) -> None:
        widget.bind(
            "<ButtonPress-1>",
            self._start_debug_window_drag,
        )
        widget.bind(
            "<B1-Motion>",
            self._drag_debug_window,
        )

    def _start_debug_window_drag(self, event: tk.Event) -> None:
        window = self._debug_window
        if window is None or not window.winfo_exists():
            return
        self._debug_drag_offset_x = event.x_root - window.winfo_x()
        self._debug_drag_offset_y = event.y_root - window.winfo_y()

    def _drag_debug_window(self, event: tk.Event) -> None:
        window = self._debug_window
        if window is None or not window.winfo_exists():
            return
        x = event.x_root - self._debug_drag_offset_x
        y = event.y_root - self._debug_drag_offset_y
        window.geometry(f"+{x}+{y}")

    def _minimize_debug_window(self) -> None:
        window = self._debug_window
        if window is not None and window.winfo_exists():
            window.withdraw()

    def _create_debug_guide_panel(
        self,
        parent: tk.Misc,
    ) -> tk.Frame:
        panel = tk.Frame(
            parent,
            background="#ffffff",
            highlightthickness=1,
            highlightbackground="#d8e2ef",
        )
        heading = tk.Frame(panel, background="#ffffff")
        heading.grid(
            row=0,
            column=0,
            padx=12,
            pady=(9, 7),
            sticky="ew",
        )
        tk.Label(
            heading,
            text="工作原理",
            background="#ffffff",
            foreground="#243a55",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")
        tk.Label(
            heading,
            text="  ·  当前设置",
            background="#ffffff",
            foreground="#8795a8",
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")

        cards = tk.Frame(panel, background="#ffffff")
        cards.grid(
            row=1,
            column=0,
            padx=9,
            sticky="ew",
        )
        for column in range(3):
            cards.grid_columnconfigure(
                column,
                weight=1,
                uniform="guide",
            )
        self._debug_guide_variables = {}
        guide_items = (
            ("1", "本地检测", "local_detection"),
            ("2", "安全判定", "safe_decision"),
            ("3", "自动锁屏", "lock_action"),
        )
        for column, (number, title, key) in enumerate(guide_items):
            card = tk.Frame(
                cards,
                background="#f9fbfe",
                highlightthickness=1,
                highlightbackground="#e1e8f1",
            )
            card.grid(
                row=0,
                column=column,
                padx=3,
                sticky="nsew",
            )
            tk.Label(
                card,
                text=number,
                background="#2f76e8",
                foreground="#ffffff",
                width=2,
                font=("Segoe UI", 9, "bold"),
            ).grid(
                row=0,
                column=0,
                rowspan=2,
                padx=(9, 9),
                pady=(9, 0),
                sticky="n",
            )
            tk.Label(
                card,
                text=title,
                background="#f9fbfe",
                foreground="#273b54",
                anchor="w",
                font=("Microsoft YaHei UI", 9, "bold"),
            ).grid(
                row=0,
                column=1,
                padx=(0, 8),
                pady=(8, 2),
                sticky="w",
            )
            variable = tk.StringVar(value="正在读取当前设置")
            self._debug_guide_variables[key] = variable
            tk.Label(
                card,
                textvariable=variable,
                background="#f9fbfe",
                foreground="#6d7e92",
                anchor="nw",
                justify="left",
                wraplength=150,
                font=("Microsoft YaHei UI", 8),
            ).grid(
                row=1,
                column=1,
                padx=(0, 8),
                pady=(0, 9),
                sticky="nw",
            )
            card.grid_columnconfigure(1, weight=1)

        tk.Label(
            panel,
            text=(
                "ⓘ  所有处理均在本地完成，不上传、不保存。"
                "可在“参数设置”中调整检测参数与锁屏阈值。"
            ),
            background="#f7faff",
            foreground="#6d819a",
            anchor="w",
            justify="left",
            wraplength=610,
            font=("Microsoft YaHei UI", 8),
        ).grid(
            row=2,
            column=0,
            padx=12,
            pady=(8, 9),
            sticky="ew",
        )
        panel.grid_columnconfigure(0, weight=1)
        return panel

    def _create_debug_metric_card(
        self,
        parent: tk.Misc,
        title: str,
        icon_text: str,
        metrics: tuple[tuple[str, str], ...],
    ) -> tk.Frame:
        card = tk.Frame(
            parent,
            background="#ffffff",
            highlightthickness=1,
            highlightbackground="#d8e2ef",
        )
        tk.Label(
            card,
            text=icon_text,
            background="#ffffff",
            foreground="#2f76e8",
            font=("Segoe UI Symbol", 15, "bold"),
        ).grid(
            row=0,
            column=0,
            padx=(14, 8),
            pady=(11, 7),
            sticky="w",
        )
        tk.Label(
            card,
            text=title,
            background="#ffffff",
            foreground="#243a55",
            anchor="w",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).grid(
            row=0,
            column=1,
            padx=(0, 14),
            pady=(11, 7),
            sticky="ew",
        )
        for row_index, (label_text, key) in enumerate(
            metrics,
            start=1,
        ):
            variable = tk.StringVar(value="--")
            self._debug_metric_variables[key] = variable
            tk.Label(
                card,
                text=label_text,
                background="#ffffff",
                foreground="#66778c",
                anchor="w",
                font=("Microsoft YaHei UI", 8),
            ).grid(
                row=row_index,
                column=0,
                columnspan=2,
                padx=(14, 6),
                pady=4,
                sticky="w",
            )
            value_label = tk.Label(
                card,
                textvariable=variable,
                background="#ffffff",
                foreground="#344a64",
                anchor="e",
                justify="right",
                wraplength=165 if key == "camera" else 120,
                font=("Consolas", 9, "bold"),
            )
            value_label.grid(
                row=row_index,
                column=2,
                padx=(4, 14),
                pady=4,
                sticky="e",
            )
            self._debug_metric_labels[key] = value_label
        card.grid_columnconfigure(1, weight=1)
        return card

    @staticmethod
    def _create_debug_button(
        parent: tk.Misc,
        text: str,
        command: Callable[[], object],
        danger: bool = False,
        primary: bool = False,
    ) -> tk.Button:
        if danger:
            background = "#fff7f7"
            foreground = "#d44444"
            active_background = "#ffe9e9"
            border_color = "#efaaaa"
        elif primary:
            background = "#2f76e8"
            foreground = "#ffffff"
            active_background = "#1f63cb"
            border_color = "#2f76e8"
        else:
            background = "#ffffff"
            foreground = "#3c5069"
            active_background = "#edf3fb"
            border_color = "#cfdbea"
        return tk.Button(
            parent,
            text=text,
            command=command,
            background=background,
            foreground=foreground,
            activebackground=active_background,
            activeforeground=(
                "#ffffff" if primary else foreground
            ),
            relief="solid",
            borderwidth=1,
            highlightbackground=border_color,
            highlightcolor=border_color,
            padx=13,
            pady=7,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9),
        )

    def _refresh_debug_window(self) -> None:
        window = self._debug_window
        if window is None or not window.winfo_exists():
            return

        running = self._service.is_running()
        snapshot = self._service.debug_snapshot()
        now = time.monotonic()
        if snapshot.frame_bgr is None:
            self._debug_last_frame_time = None
            self._debug_preview_fps = 0.0
            self._debug_last_frame_sequence = snapshot.sequence
        elif (
            snapshot.frame_bgr is not None
            and snapshot.sequence != self._debug_last_frame_sequence
        ):
            if self._debug_last_frame_time is not None:
                interval = now - self._debug_last_frame_time
                if interval > 0:
                    instant_fps = 1.0 / interval
                    if self._debug_preview_fps <= 0:
                        self._debug_preview_fps = instant_fps
                    else:
                        self._debug_preview_fps = (
                            self._debug_preview_fps * 0.65
                            + instant_fps * 0.35
                        )
            self._debug_last_frame_time = now
            self._debug_last_frame_sequence = snapshot.sequence

        if self._debug_status_badge is not None:
            if running and snapshot.frame_bgr is not None:
                badge_text = "●  LIVE"
                badge_color = "#2b9f51"
                badge_background = "#edf9f1"
            elif running:
                badge_text = "●  待机"
                badge_color = "#a86b00"
                badge_background = "#fff8e6"
            else:
                badge_text = "●  已暂停"
                badge_color = "#c74343"
                badge_background = "#fff0f0"
            self._debug_status_badge.configure(
                text=badge_text,
                foreground=badge_color,
                background=badge_background,
            )

        if self._debug_status_variable is not None:
            self._debug_status_variable.set(
                f"{snapshot.status}  ·  {self._service.status_detail()}"
            )

        confidences = [
            detection.confidence
            for detection in snapshot.detections
        ]
        identity_similarities = [
            detection.identity_similarity
            for detection in snapshot.detections
            if detection.identity_similarity is not None
        ]
        try:
            current_settings = self._settings_store.load()
            camera_name = current_settings.camera_name
        except SettingsError:
            current_settings = None
            camera_name = "设置读取失败"

        if (
            current_settings is not None
            and self._debug_camera_variable is not None
            and self._debug_camera_variable.get() != camera_name
        ):
            self._debug_camera_variable.set(camera_name)

        if current_settings is None:
            guide_values = {
                "local_detection": "暂时无法读取检测参数",
                "safe_decision": "暂时无法读取锁屏参数",
                "lock_action": (
                    "仅在全部状态明确时锁屏\n异常时自动禁止锁屏"
                ),
            }
        else:
            guide_values = {
                "local_detection": (
                    f"每 {current_settings.detection_interval_seconds:g} 秒"
                    "本地检测一次\n"
                    + (
                        "当前 NPU 优先，失败回退 CPU"
                        if current_settings.inference_device == "NPU"
                        else "当前由用户指定使用 CPU"
                    )
                ),
                "safe_decision": (
                    (
                        "未确认本人 ≥ "
                        if current_settings.presence_mode
                        == "REGISTERED_FACE"
                        else "无人脸 ≥ "
                    )
                    + f"{current_settings.face_absence_timeout_seconds:g} 秒"
                    + " 且键鼠空闲 ≥ "
                    + f"{current_settings.input_idle_timeout_seconds:g} 秒\n"
                    + "启动宽限期 "
                    + f"{current_settings.startup_grace_period_seconds:g} 秒"
                ),
                "lock_action": (
                    "全部条件满足才锁定 Windows\n"
                    "摄像头、模型或状态异常时不锁屏"
                ),
            }
        for key, value in guide_values.items():
            variable = self._debug_guide_variables.get(key)
            if variable is not None:
                variable.set(value)

        frame = snapshot.frame_bgr
        resolution = (
            f"{frame.shape[1]} × {frame.shape[0]}"
            if frame is not None
            else "--"
        )
        frame_age = (
            max(0.0, now - snapshot.captured_at)
            if snapshot.captured_at is not None
            else None
        )
        startup_grace = snapshot.startup_elapsed_seconds
        metric_values = {
            "face_count": str(len(snapshot.detections)),
            "confidence": (
                (
                    f"{max(confidences) * 100:.1f}% / "
                    f"{max(identity_similarities) * 100:.1f}%"
                    if identity_similarities
                    else f"{max(confidences) * 100:.1f}% / --"
                )
                if confidences
                else "--"
            ),
            "inference": self._format_milliseconds(
                snapshot.inference_ms
            ),
            "preview_fps": (
                f"{self._debug_preview_fps:.1f} FPS"
                if self._debug_preview_fps > 0
                else "--"
            ),
            "face_absent": self._format_seconds(
                snapshot.face_absent_seconds
            ),
            "input_idle": self._format_seconds(
                snapshot.input_idle_seconds
            ),
            "startup_grace": (
                f"{startup_grace:.1f} / "
                f"{config.STARTUP_GRACE_PERIOD_SECONDS:.0f} s"
                if startup_grace is not None
                else "--"
            ),
            "lock_condition": (
                "READY"
                if snapshot.should_lock is True
                else "SAFE"
                if snapshot.should_lock is False
                else "UNKNOWN"
            ),
            "device": snapshot.device or "--",
            "resolution": resolution,
            "frame_age": (
                f"{frame_age * 1000:.0f} ms"
                if frame_age is not None
                else "--"
            ),
            "camera": camera_name,
        }
        for key, value in metric_values.items():
            variable = self._debug_metric_variables.get(key)
            if variable is not None:
                variable.set(value)

        lock_label = self._debug_metric_labels.get("lock_condition")
        if lock_label is not None:
            if snapshot.should_lock is True:
                lock_color = "#d94747"
            elif snapshot.should_lock is False:
                lock_color = "#2b9f51"
            else:
                lock_color = "#a86b00"
            lock_label.configure(foreground=lock_color)
        device_label = self._debug_metric_labels.get("device")
        if device_label is not None:
            device_label.configure(foreground="#2f76e8")

        if self._debug_device_toggle_button is not None:
            actual_device = snapshot.device.strip().upper()
            if actual_device == "NPU":
                device_button_text = "切换至 CPU"
            elif actual_device == "CPU":
                device_button_text = "尝试切换至 NPU"
            elif (
                current_settings is not None
                and current_settings.inference_device == "NPU"
            ):
                device_button_text = "切换至 CPU"
            else:
                device_button_text = "切换至 NPU"
            self._debug_device_toggle_button.configure(
                text=device_button_text
            )

        if self._debug_toggle_button is not None:
            self._debug_toggle_button.configure(
                text="暂停监控" if running else "开始监控"
            )
        self._render_debug_preview(snapshot)
        window.after(500, self._refresh_debug_window)

    @staticmethod
    def _format_seconds(value: Optional[float]) -> str:
        return "--" if value is None else f"{value:.1f} s"

    @staticmethod
    def _format_milliseconds(value: Optional[float]) -> str:
        return "--" if value is None else f"{value:.1f} ms"

    def _render_debug_preview(
        self,
        snapshot: DebugFrameSnapshot,
    ) -> None:
        canvas = self._debug_canvas
        if canvas is None or not canvas.winfo_exists():
            return

        frame = snapshot.frame_bgr
        if frame is None:
            self._debug_photo_image = None
            canvas.delete("all")
            canvas.create_text(
                320,
                180,
                text=f"摄像头待机\n\n{snapshot.status}",
                fill="#74859a",
                width=560,
                justify="center",
                font=("Microsoft YaHei UI", 11, "bold"),
            )
            return

        try:
            annotated = np.ascontiguousarray(frame.copy())
            frame_height, frame_width = annotated.shape[:2]

            for face_index, detection in enumerate(
                snapshot.detections,
                start=1,
            ):
                registered_mode = (
                    snapshot.presence_mode == "REGISTERED_FACE"
                )
                if registered_mode and detection.is_registered_person is True:
                    box_color = (75, 205, 90)
                    corner_color = (75, 205, 90)
                    label_color = (75, 185, 75)
                    identity_text = "SELF"
                elif registered_mode:
                    box_color = (65, 105, 235)
                    corner_color = (65, 105, 235)
                    label_color = (65, 105, 220)
                    identity_text = "OTHER"
                else:
                    box_color = (75, 205, 90)
                    corner_color = (75, 205, 90)
                    label_color = (75, 185, 75)
                    identity_text = "FACE"
                cv2.rectangle(
                    annotated,
                    (detection.xmin, detection.ymin),
                    (detection.xmax, detection.ymax),
                    box_color,
                    2,
                )
                corner_length = max(
                    8,
                    min(
                        22,
                        (detection.xmax - detection.xmin) // 4,
                        (detection.ymax - detection.ymin) // 4,
                    ),
                )
                for start, end in (
                    (
                        (detection.xmin, detection.ymin),
                        (detection.xmin + corner_length, detection.ymin),
                    ),
                    (
                        (detection.xmin, detection.ymin),
                        (detection.xmin, detection.ymin + corner_length),
                    ),
                    (
                        (detection.xmax, detection.ymin),
                        (detection.xmax - corner_length, detection.ymin),
                    ),
                    (
                        (detection.xmax, detection.ymin),
                        (detection.xmax, detection.ymin + corner_length),
                    ),
                    (
                        (detection.xmin, detection.ymax),
                        (detection.xmin + corner_length, detection.ymax),
                    ),
                    (
                        (detection.xmin, detection.ymax),
                        (detection.xmin, detection.ymax - corner_length),
                    ),
                    (
                        (detection.xmax, detection.ymax),
                        (detection.xmax - corner_length, detection.ymax),
                    ),
                    (
                        (detection.xmax, detection.ymax),
                        (detection.xmax, detection.ymax - corner_length),
                    ),
                ):
                    cv2.line(
                        annotated,
                        start,
                        end,
                        corner_color,
                        3,
                    )

                confidence_text = (
                    f"{identity_text} {face_index:02d}  "
                    + (
                        f"{detection.identity_similarity * 100:.1f}%"
                        if detection.identity_similarity is not None
                        else f"{detection.confidence * 100:.1f}%"
                    )
                )
                (text_width, text_height), baseline = cv2.getTextSize(
                    confidence_text,
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.48,
                    1,
                )
                text_bottom = max(
                    text_height + baseline + 4,
                    detection.ymin,
                )
                text_top = max(
                    0,
                    text_bottom - text_height - baseline - 4,
                )
                text_right = min(
                    annotated.shape[1] - 1,
                    detection.xmin + text_width + 8,
                )
                cv2.rectangle(
                    annotated,
                    (detection.xmin, text_top),
                    (text_right, text_bottom),
                    label_color,
                    thickness=-1,
                )
                cv2.putText(
                    annotated,
                    confidence_text,
                    (detection.xmin + 4, text_bottom - baseline - 2),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.48,
                    (255, 255, 255),
                    1,
                    cv2.LINE_AA,
                )

            overlay = annotated.copy()
            cv2.rectangle(
                overlay,
                (12, 12),
                (112, 40),
                (25, 31, 38),
                thickness=-1,
            )
            privacy_width = min(frame_width - 24, 290)
            cv2.rectangle(
                overlay,
                (12, frame_height - 44),
                (12 + privacy_width, frame_height - 12),
                (25, 31, 38),
                thickness=-1,
            )
            cv2.addWeighted(overlay, 0.64, annotated, 0.36, 0, annotated)

            resolution_text = f"{frame_width} x {frame_height}"
            cv2.putText(
                annotated,
                resolution_text,
                (23, 31),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (240, 245, 250),
                1,
                cv2.LINE_AA,
            )
            cv2.putText(
                annotated,
                "LOCAL PROCESSING ONLY  /  NO RECORDING",
                (24, frame_height - 23),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                (245, 248, 252),
                1,
                cv2.LINE_AA,
            )

            rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
            preview = Image.fromarray(rgb)
            preview.thumbnail(
                (640, 360),
                Image.Resampling.LANCZOS,
            )
            letterboxed = Image.new("RGB", (640, 360), (17, 17, 17))
            offset = (
                (640 - preview.width) // 2,
                (360 - preview.height) // 2,
            )
            letterboxed.paste(preview, offset)
            photo = ImageTk.PhotoImage(
                image=letterboxed,
                master=self._root,
            )
        except (cv2.error, OSError, ValueError) as exc:
            LOGGER.warning(
                "Unable to render debug preview: %s",
                exc,
            )
            self._debug_photo_image = None
            canvas.delete("all")
            canvas.create_text(
                320,
                180,
                text="调试画面显示失败",
                fill="white",
                font=("Microsoft YaHei UI", 12),
            )
            return

        self._debug_photo_image = photo
        canvas.delete("all")
        canvas.create_image(320, 180, image=photo)

    def _close_debug_window(self) -> None:
        self._debug_photo_image = None
        self._debug_window_icon = None
        self._debug_header_icon = None
        self._debug_metric_variables = {}
        self._debug_metric_labels = {}
        self._debug_guide_variables = {}
        self._debug_status_badge = None
        self._debug_canvas = None
        self._debug_toggle_button = None
        self._debug_device_toggle_button = None
        self._debug_exit_button = None
        self._debug_camera_variable = None
        self._debug_camera_combobox = None
        self._debug_last_frame_sequence = -1
        self._debug_last_frame_time = None
        self._debug_preview_fps = 0.0
        window = self._debug_window
        if window is not None and window.winfo_exists():
            window.destroy()

    def _toggle_monitoring(self) -> None:
        if self._service.is_running():
            self._service.pause_async()
        else:
            self._service.start_async()

    def _exit_from_debug(self) -> None:
        self._request_shutdown()

    def _toggle_inference_device(self) -> None:
        try:
            current = self._settings_store.load()
            actual_device = (
                self._service.debug_snapshot().device.strip().upper()
            )
            if actual_device == "NPU":
                target_device = "CPU"
            elif actual_device == "CPU":
                target_device = "NPU"
            else:
                target_device = (
                    "CPU"
                    if current.inference_device == "NPU"
                    else "NPU"
                )
            updated = replace(
                current,
                inference_device=target_device,
            )
            self._settings_store.save(updated)
        except SettingsError as exc:
            messagebox.showerror(
                "切换推理设备失败",
                str(exc),
                parent=self._debug_window,
            )
            return

        self._service.restart_async()

    def _refresh_debug_camera_choices(self) -> None:
        combobox = self._debug_camera_combobox
        variable = self._debug_camera_variable
        if (
            combobox is None
            or variable is None
            or not combobox.winfo_exists()
        ):
            return
        try:
            current_camera = self._settings_store.load().camera_name
        except SettingsError:
            current_camera = variable.get().strip()
        camera_names = self._camera_names()
        if current_camera and current_camera not in camera_names:
            camera_names.insert(0, current_camera)
        combobox.configure(values=camera_names)
        if current_camera:
            variable.set(current_camera)

    def _on_debug_camera_selected(
        self,
        event: Optional[tk.Event] = None,
    ) -> None:
        del event
        variable = self._debug_camera_variable
        if variable is None:
            return
        selected_camera = variable.get().strip()
        if not selected_camera:
            return
        try:
            current = self._settings_store.load()
            if selected_camera == current.camera_name:
                return
            updated = replace(
                current,
                camera_name=selected_camera,
            )
            self._settings_store.save(updated)
        except SettingsError as exc:
            messagebox.showerror(
                "切换摄像头失败",
                str(exc),
                parent=self._debug_window,
            )
            return

        if self._debug_status_variable is not None:
            self._debug_status_variable.set(
                f"正在切换至摄像头：{selected_camera}"
            )
        self._service.restart_async()

    @staticmethod
    def _open_log_directory() -> None:
        log_directory = config.USER_DATA_DIRECTORY / "logs"
        try:
            log_directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(log_directory))
        except OSError as exc:
            messagebox.showerror(
                "打开日志失败",
                str(exc),
            )

    def _start_face_registration(self) -> None:
        existing_window = self._registration_window
        if existing_window is not None and existing_window.winfo_exists():
            existing_window.deiconify()
            existing_window.lift()
            existing_window.focus_force()
            return

        try:
            settings = self._settings_store.load()
        except SettingsError as exc:
            messagebox.showerror("注册失败", str(exc))
            return
        if self._face_template_store.is_registered() and not messagebox.askyesno(
            "更新本人人脸",
            "更新会替换现有的加密人脸特征模板。是否继续？",
            parent=self._settings_window,
        ):
            return

        settings_window = self._settings_window
        if settings_window is not None and settings_window.winfo_exists():
            settings_window.destroy()

        window = tk.Toplevel(self._root)
        self._registration_window = window
        window.title(f"{config.APPLICATION_TITLE} · 注册本人")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self._cancel_face_registration)

        content = ttk.Frame(window, padding=16)
        content.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            content,
            text="注册本人人脸",
            font=("Microsoft YaHei UI", 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            content,
            text=(
                "请保证画面中只有你一人，并缓慢左右转头。摄像头画面只在内存中处理，"
                "不会保存照片或视频。"
            ),
            wraplength=600,
            foreground="#555555",
            justify="left",
        ).grid(row=1, column=0, pady=(6, 12), sticky="w")

        preview = tk.Label(
            content,
            text="正在等待摄像头……",
            width=80,
            height=20,
            background="#050a10",
            foreground="#9fb7c9",
            font=("Microsoft YaHei UI", 10),
        )
        preview.grid(row=2, column=0, sticky="ew")
        self._registration_preview_label = preview

        self._registration_status_variable = tk.StringVar(
            value="正在安全暂停监控并释放摄像头……"
        )
        ttk.Label(
            content,
            textvariable=self._registration_status_variable,
            wraplength=600,
        ).grid(row=3, column=0, pady=(12, 6), sticky="w")
        self._registration_progress = ttk.Progressbar(
            content,
            orient="horizontal",
            mode="determinate",
            maximum=config.FACE_REGISTRATION_SAMPLE_COUNT,
            length=600,
        )
        self._registration_progress.grid(row=4, column=0, sticky="ew")
        self._registration_cancel_button = ttk.Button(
            content,
            text="取消注册",
            command=self._cancel_face_registration,
        )
        self._registration_cancel_button.grid(
            row=5,
            column=0,
            pady=(12, 0),
            sticky="e",
        )

        self._registration_cancel_event = threading.Event()
        self._registration_queue = queue.Queue(maxsize=4)
        self._registration_resume_after = self._service.is_running()
        threading.Thread(
            target=self._face_registration_worker,
            args=(settings,),
            name="seat-sentinel-face-registration",
            daemon=True,
        ).start()
        window.after(100, self._poll_face_registration)
        window.update_idletasks()
        x = max(0, (window.winfo_screenwidth() - window.winfo_width()) // 2)
        y = max(0, (window.winfo_screenheight() - window.winfo_height()) // 3)
        window.geometry(f"+{x}+{y}")
        window.deiconify()
        window.lift()
        window.focus_force()

    def _face_registration_worker(self, settings: AppSettings) -> None:
        try:
            self._service.pause_blocking()
            cancel_event = self._registration_cancel_event
            if cancel_event is None or cancel_event.is_set():
                raise FaceRegistrationCancelled("已取消人脸注册")
            template = register_face_from_camera(
                settings=settings,
                template_store=self._face_template_store,
                cancel_event=cancel_event,
                callback=lambda update: self._queue_registration_message(
                    "update", update
                ),
            )
            self._queue_registration_message("success", template)
        except FaceRegistrationCancelled as exc:
            self._queue_registration_message("cancelled", str(exc))
        except (FaceRegistrationError, FaceTemplateError) as exc:
            self._queue_registration_message("error", str(exc))
        except Exception as exc:
            LOGGER.exception("Unexpected face registration failure")
            self._queue_registration_message("error", str(exc))

    def _queue_registration_message(
        self,
        kind: str,
        payload: object,
    ) -> None:
        message_queue = self._registration_queue
        if message_queue is None:
            return
        while True:
            try:
                message_queue.put_nowait((kind, payload))
                return
            except queue.Full:
                try:
                    message_queue.get_nowait()
                except queue.Empty:
                    return

    def _poll_face_registration(self) -> None:
        window = self._registration_window
        message_queue = self._registration_queue
        if (
            window is None
            or not window.winfo_exists()
            or message_queue is None
        ):
            return

        terminal: Optional[tuple[str, object]] = None
        while True:
            try:
                kind, payload = message_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "update" and isinstance(payload, FaceRegistrationUpdate):
                self._render_registration_update(payload)
            elif kind in {"success", "cancelled", "error"}:
                terminal = (kind, payload)

        if terminal is None:
            window.after(100, self._poll_face_registration)
            return

        kind, payload = terminal
        if self._registration_cancel_button is not None:
            self._registration_cancel_button.configure(state="disabled")
        if kind == "success":
            if self._registration_status_variable is not None:
                self._registration_status_variable.set(
                    "注册成功。已加密保存人脸特征模板，未保存任何照片。"
                )
            if self._registration_progress is not None:
                self._registration_progress.configure(
                    value=config.FACE_REGISTRATION_SAMPLE_COUNT
                )
            messagebox.showinfo(
                "注册成功",
                "本人人脸注册完成。现在可以在设置中选择“仅本人（需注册）”。",
                parent=window,
            )
        elif kind == "error":
            if self._registration_status_variable is not None:
                self._registration_status_variable.set(f"注册失败：{payload}")
            messagebox.showerror("注册失败", str(payload), parent=window)
        else:
            if self._registration_status_variable is not None:
                self._registration_status_variable.set("已取消人脸注册。")

        restart_monitoring = self._registration_resume_after
        if kind == "success":
            try:
                restart_monitoring = (
                    restart_monitoring
                    or self._settings_store.load().presence_mode
                    == "REGISTERED_FACE"
                )
            except SettingsError:
                pass
        self._finish_face_registration(restart_monitoring)

    def _render_registration_update(
        self,
        update: FaceRegistrationUpdate,
    ) -> None:
        if self._registration_status_variable is not None:
            self._registration_status_variable.set(update.message)
        if self._registration_progress is not None:
            self._registration_progress.configure(
                maximum=update.required_samples,
                value=update.accepted_samples,
            )
        preview = self._registration_preview_label
        frame = update.frame_bgr
        if preview is None or frame is None or not preview.winfo_exists():
            return
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb = cv2.resize(rgb, (600, 338), interpolation=cv2.INTER_AREA)
            photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        except (cv2.error, OSError, ValueError) as exc:
            LOGGER.warning("Unable to render registration preview: %s", exc)
            return
        self._registration_photo_image = photo
        preview.configure(image=photo, text="", width=600, height=338)

    def _cancel_face_registration(self) -> None:
        cancel_event = self._registration_cancel_event
        if cancel_event is not None:
            cancel_event.set()
        if self._registration_status_variable is not None:
            self._registration_status_variable.set("正在取消并释放摄像头……")
        if self._registration_cancel_button is not None:
            self._registration_cancel_button.configure(state="disabled")

    def _finish_face_registration(self, restart_monitoring: bool) -> None:
        window = self._registration_window
        if window is not None and window.winfo_exists():
            window.destroy()
        self._registration_window = None
        self._registration_cancel_event = None
        self._registration_queue = None
        self._registration_status_variable = None
        self._registration_progress = None
        self._registration_preview_label = None
        self._registration_cancel_button = None
        self._registration_photo_image = None
        self._registration_resume_after = False
        if restart_monitoring:
            self._service.start_async()

    def _delete_registered_face(self) -> None:
        if not self._face_template_store.is_registered():
            messagebox.showinfo(
                "本人数据",
                "当前没有已注册的人脸特征模板。",
                parent=self._settings_window,
            )
            return
        if not messagebox.askyesno(
            "删除本人人脸数据",
            "将永久删除本机加密保存的人脸特征模板。删除后会切换为任意人脸模式。是否继续？",
            parent=self._settings_window,
        ):
            return
        try:
            self._face_template_store.delete()
            current = self._settings_store.load()
            mode_changed = current.presence_mode == "REGISTERED_FACE"
            if mode_changed:
                self._settings_store.save(
                    replace(current, presence_mode="ANY_FACE")
                )
        except (FaceTemplateError, SettingsError) as exc:
            messagebox.showerror(
                "删除失败",
                str(exc),
                parent=self._settings_window,
            )
            return
        messagebox.showinfo(
            "本人数据",
            "已删除本人人脸特征模板。未保存照片或视频。",
            parent=self._settings_window,
        )
        window = self._settings_window
        if window is not None and window.winfo_exists():
            window.destroy()
        if mode_changed:
            self._service.restart_async()

    def _show_settings(self) -> None:
        if (
            self._settings_window is not None
            and self._settings_window.winfo_exists()
        ):
            self._settings_window.deiconify()
            self._settings_window.lift()
            self._settings_window.focus_force()
            return

        try:
            settings = self._settings_store.load()
        except SettingsError as exc:
            messagebox.showerror("设置错误", str(exc))
            return

        camera_names = self._camera_names()
        if settings.camera_name not in camera_names:
            camera_names.insert(0, settings.camera_name)

        window = tk.Toplevel(self._root)
        self._settings_window = window
        window.title(f"{config.APPLICATION_TITLE} · 设置")
        window.resizable(False, True)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        scroll_container = ttk.Frame(window)
        scroll_container.grid(row=0, column=0, sticky="nsew")
        scroll_container.columnconfigure(0, weight=1)
        scroll_container.rowconfigure(0, weight=1)

        frame_background = ttk.Style(window).lookup(
            "TFrame",
            "background",
        )
        settings_canvas = tk.Canvas(
            scroll_container,
            background=frame_background or "#f0f0f0",
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        settings_scrollbar = ttk.Scrollbar(
            scroll_container,
            orient="vertical",
            command=settings_canvas.yview,
        )
        settings_canvas.configure(
            yscrollcommand=settings_scrollbar.set,
        )
        settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scrollbar.grid(row=0, column=1, sticky="ns")

        content = ttk.Frame(settings_canvas, padding=16)
        content.columnconfigure(1, weight=1)
        content_window = settings_canvas.create_window(
            (0, 0),
            window=content,
            anchor="nw",
        )

        def update_scroll_region(_event: tk.Event[tk.Misc]) -> None:
            bounds = settings_canvas.bbox("all")
            if bounds is not None:
                settings_canvas.configure(scrollregion=bounds)

        def stretch_scroll_content(event: tk.Event[tk.Misc]) -> None:
            settings_canvas.itemconfigure(
                content_window,
                width=event.width,
            )

        def scroll_settings(event: tk.Event[tk.Misc]) -> str:
            delta = int(getattr(event, "delta", 0))
            if delta:
                direction = -1 if delta > 0 else 1
                units = max(1, abs(delta) // 120)
                settings_canvas.yview_scroll(direction * units, "units")
            return "break"

        content.bind("<Configure>", update_scroll_region)
        settings_canvas.bind("<Configure>", stretch_scroll_content)
        window.bind("<MouseWheel>", scroll_settings)

        variables = {
            "camera_name": tk.StringVar(value=settings.camera_name),
            "camera_monitoring_mode": tk.StringVar(
                value=CAMERA_MONITORING_MODE_LABELS[
                    settings.camera_monitoring_mode
                ]
            ),
            "camera_activation_idle_seconds": tk.StringVar(
                value=str(settings.camera_activation_idle_seconds)
            ),
            "presence_mode": tk.StringVar(
                value=PRESENCE_MODE_LABELS[settings.presence_mode]
            ),
            "privacy_blur_enabled": tk.BooleanVar(
                value=settings.privacy_blur_enabled
            ),
            "privacy_blur_hotkey": tk.StringVar(
                value=settings.privacy_blur_hotkey
            ),
            "sedentary_reminder_enabled": tk.BooleanVar(
                value=settings.sedentary_reminder_enabled
            ),
            "detection_interval_seconds": tk.StringVar(
                value=str(settings.detection_interval_seconds)
            ),
            "face_confidence_threshold": tk.StringVar(
                value=str(settings.face_confidence_threshold)
            ),
            "inference_device": tk.StringVar(
                value=settings.inference_device
            ),
            "face_absence_timeout_seconds": tk.StringVar(
                value=str(settings.face_absence_timeout_seconds)
            ),
            "input_idle_timeout_seconds": tk.StringVar(
                value=str(settings.input_idle_timeout_seconds)
            ),
            "startup_grace_period_seconds": tk.StringVar(
                value=str(settings.startup_grace_period_seconds)
            ),
        }
        self._settings_privacy_blur_variable = variables[
            "privacy_blur_enabled"
        ]

        rows = [
            ("摄像头", "camera_name"),
            ("摄像头工作模式", "camera_monitoring_mode"),
            ("空闲开启摄像头（秒）", "camera_activation_idle_seconds"),
            ("在场判断", "presence_mode"),
            ("检测间隔（秒）", "detection_interval_seconds"),
            ("人脸置信度", "face_confidence_threshold"),
            ("推理设备", "inference_device"),
            ("无人超时（秒）", "face_absence_timeout_seconds"),
            ("键鼠空闲超时（秒）", "input_idle_timeout_seconds"),
            ("启动/恢复宽限期（秒）", "startup_grace_period_seconds"),
        ]

        camera_activation_entry: Optional[ttk.Entry] = None
        for row_index, (label_text, key) in enumerate(rows):
            ttk.Label(content, text=label_text).grid(
                row=row_index,
                column=0,
                padx=(0, 12),
                pady=6,
                sticky="w",
            )
            if key in {
                "camera_name",
                "camera_monitoring_mode",
                "inference_device",
                "presence_mode",
            }:
                if key == "camera_name":
                    values = camera_names
                elif key == "camera_monitoring_mode":
                    values = list(CAMERA_MONITORING_MODE_VALUES)
                elif key == "presence_mode":
                    values = list(PRESENCE_MODE_VALUES)
                else:
                    values = ["NPU", "CPU"]
                widget = ttk.Combobox(
                    content,
                    textvariable=variables[key],
                    values=values,
                    state="readonly",
                    width=34,
                )
            else:
                widget = ttk.Entry(
                    content,
                    textvariable=variables[key],
                    width=36,
                )
            widget.grid(
                row=row_index,
                column=1,
                pady=6,
                sticky="ew",
            )
            if key == "camera_activation_idle_seconds":
                camera_activation_entry = widget

        privacy_controls = ttk.LabelFrame(
            content,
            text="多人脸隐私模糊",
            padding=10,
        )
        privacy_controls.grid(
            row=len(rows),
            column=0,
            columnspan=2,
            pady=(10, 4),
            sticky="ew",
        )
        privacy_blur_checkbutton = ttk.Checkbutton(
            privacy_controls,
            text="启用多人脸隐私模糊：画面中出现至少两个人时自动开启毛玻璃",
            variable=variables["privacy_blur_enabled"],
        )
        privacy_blur_checkbutton.pack(anchor="w")
        ttk.Label(
            privacy_controls,
            text="也可从右下角托盘图标的右键菜单随时开关",
            foreground="#666666",
        ).pack(anchor="w", pady=(4, 0))

        sedentary_controls = ttk.LabelFrame(
            content,
            text="久坐提醒",
            padding=10,
        )
        sedentary_controls.grid(
            row=len(rows) + 1,
            column=0,
            columnspan=2,
            pady=(10, 4),
            sticky="ew",
        )
        ttk.Checkbutton(
            sedentary_controls,
            text=(
                "启用久坐提醒：连续在场每满 30 分钟提示一次，"
                "离开 30 秒后重新计时"
            ),
            variable=variables["sedentary_reminder_enabled"],
        ).pack(anchor="w")
        ttk.Label(
            sedentary_controls,
            text="提醒沿用锁屏预告样式，显示 1 秒且不会抢占当前窗口焦点",
            foreground="#666666",
        ).pack(anchor="w", pady=(4, 0))
        hotkey_row = ttk.Frame(privacy_controls)
        hotkey_row.pack(fill="x", pady=(10, 0))
        ttk.Label(
            hotkey_row,
            text="手动毛玻璃快捷键",
        ).pack(side="left")
        ttk.Entry(
            hotkey_row,
            textvariable=variables["privacy_blur_hotkey"],
            width=18,
        ).pack(side="left", padx=(12, 0))
        ttk.Label(
            privacy_controls,
            text=(
                "默认 Alt+B；空闲监测、监控暂停或摄像头关闭时也可直接切换，"
                "再次按下或快速甩动鼠标即可解除"
            ),
            foreground="#666666",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        def sync_camera_mode_controls(*_args: object) -> None:
            selected_mode = CAMERA_MONITORING_MODE_VALUES.get(
                variables["camera_monitoring_mode"].get(),
                variables["camera_monitoring_mode"].get(),
            )
            idle_triggered = selected_mode == "IDLE_TRIGGERED"
            if camera_activation_entry is not None:
                camera_activation_entry.configure(
                    state="normal" if idle_triggered else "disabled"
                )
            if idle_triggered:
                variables["privacy_blur_enabled"].set(False)
                privacy_blur_checkbutton.state(["disabled"])
            else:
                privacy_blur_checkbutton.state(["!disabled"])

        variables["camera_monitoring_mode"].trace_add(
            "write",
            sync_camera_mode_controls,
        )
        sync_camera_mode_controls()

        identity_controls = ttk.LabelFrame(
            content,
            text="本人人脸注册",
            padding=10,
        )
        identity_controls.grid(
            row=len(rows) + 2,
            column=0,
            columnspan=2,
            pady=(10, 4),
            sticky="ew",
        )
        identity_status = (
            "状态：已注册（仅保存 Windows 加密的人脸特征模板）"
            if self._face_template_store.is_registered()
            else "状态：未注册"
        )
        ttk.Label(identity_controls, text=identity_status).pack(
            side="left"
        )
        ttk.Button(
            identity_controls,
            text=(
                "更新本人"
                if self._face_template_store.is_registered()
                else "注册本人"
            ),
            command=self._start_face_registration,
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            identity_controls,
            text="删除本人数据",
            command=self._delete_registered_face,
            state=(
                "normal"
                if self._face_template_store.is_registered()
                else "disabled"
            ),
        ).pack(side="right")

        ttk.Label(
            content,
            text=(
                "“键鼠空闲后监测”会在达到设定空闲时间后开启摄像头，恢复操作后"
                "立即释放；该模式只与自动多人脸隐私模糊互斥，手动快捷键不受影响。"
                "持续监测模式下，"
                "多人脸隐私模糊无需注册人脸；仅本人模式还可在只剩本人 3 秒后"
                "自动解除，未注册时可甩动鼠标解除。"
                "保存后会安全释放摄像头并重启监控。"
            ),
            foreground="#555555",
            wraplength=560,
            justify="left",
        ).grid(
            row=len(rows) + 3,
            column=0,
            columnspan=2,
            pady=(10, 8),
            sticky="w",
        )

        buttons = ttk.Frame(window, padding=(16, 10, 24, 16))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(
            buttons,
            text="取消",
            command=window.destroy,
        ).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(
            buttons,
            text="保存并应用",
            command=lambda: self._save_settings(
                variables,
                window,
                settings,
            ),
        ).grid(row=0, column=2)

        def bind_scroll_wheel(widget: tk.Misc) -> None:
            widget.bind("<MouseWheel>", scroll_settings)
            for child in widget.winfo_children():
                bind_scroll_wheel(child)

        bind_scroll_wheel(scroll_container)
        bind_scroll_wheel(buttons)

        window.update_idletasks()
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        content_width = content.winfo_reqwidth()
        content_height = content.winfo_reqheight()
        buttons_height = buttons.winfo_reqheight()
        max_window_height = max(200, screen_height - 96)
        viewport_height = min(
            content_height,
            max(120, max_window_height - buttons_height),
        )
        settings_canvas.configure(
            width=content_width,
            height=viewport_height,
        )
        window.update_idletasks()
        window_width = window.winfo_reqwidth()
        window_height = min(window.winfo_reqheight(), max_window_height)
        window.minsize(
            window_width,
            min(window_height, 420),
        )
        window.maxsize(window_width, max_window_height)
        x = max(
            0,
            (screen_width - window_width) // 2,
        )
        y = max(
            0,
            (screen_height - window_height) // 3,
        )
        window.geometry(f"{window_width}x{window_height}+{x}+{y}")
        window.deiconify()
        window.lift()
        window.focus_force()

    @staticmethod
    def _camera_names() -> list[str]:
        try:
            names = [
                str(camera.name)
                for camera in enumerate_cameras(cv2.CAP_DSHOW)
            ]
        except Exception as exc:
            LOGGER.warning("Unable to enumerate cameras: %s", exc)
            return []
        return list(dict.fromkeys(names))

    def _save_settings(
        self,
        variables: dict[str, tk.Variable],
        window: tk.Toplevel,
        old_settings: AppSettings,
    ) -> None:
        try:
            settings = AppSettings.from_mapping(
                {
                    "camera_name": variables["camera_name"].get(),
                    "camera_monitoring_mode": (
                        CAMERA_MONITORING_MODE_VALUES.get(
                            variables["camera_monitoring_mode"].get(),
                            variables["camera_monitoring_mode"].get(),
                        )
                    ),
                    "camera_activation_idle_seconds": variables[
                        "camera_activation_idle_seconds"
                    ].get(),
                    "presence_mode": PRESENCE_MODE_VALUES.get(
                        variables["presence_mode"].get(),
                        variables["presence_mode"].get(),
                    ),
                    "privacy_blur_enabled": variables[
                        "privacy_blur_enabled"
                    ].get(),
                    "privacy_blur_hotkey": variables[
                        "privacy_blur_hotkey"
                    ].get(),
                    "sedentary_reminder_enabled": variables[
                        "sedentary_reminder_enabled"
                    ].get(),
                    "detection_interval_seconds": variables[
                        "detection_interval_seconds"
                    ].get(),
                    "face_confidence_threshold": variables[
                        "face_confidence_threshold"
                    ].get(),
                    "inference_device": variables[
                        "inference_device"
                    ].get(),
                    "face_absence_timeout_seconds": variables[
                        "face_absence_timeout_seconds"
                    ].get(),
                    "input_idle_timeout_seconds": variables[
                        "input_idle_timeout_seconds"
                    ].get(),
                    "startup_grace_period_seconds": variables[
                        "startup_grace_period_seconds"
                    ].get(),
                    "frame_width": old_settings.frame_width,
                    "frame_height": old_settings.frame_height,
                }
            )
            if (
                settings.presence_mode == "REGISTERED_FACE"
                and not self._face_template_store.is_registered()
            ):
                raise SettingsError(
                    "选择“仅本人”前必须先完成人脸注册"
                )
            if settings.presence_mode == "REGISTERED_FACE":
                try:
                    self._face_template_store.load()
                except FaceTemplateError as exc:
                    raise SettingsError(
                        f"本人人脸模板无法使用：{exc}"
                    ) from exc
            previous_hotkey = self._privacy_hotkey
            self._replace_privacy_hotkey(settings.privacy_blur_hotkey)
            hotkey_rebound = self._privacy_hotkey is not previous_hotkey
            self._settings_store.save(settings)
            settings.apply_to_runtime()
        except (GlobalHotkeyError, SettingsError) as exc:
            if locals().get("hotkey_rebound", False):
                try:
                    self._replace_privacy_hotkey(
                        old_settings.privacy_blur_hotkey
                    )
                except GlobalHotkeyError:
                    LOGGER.exception(
                        "Unable to restore the previous privacy hotkey"
                    )
            messagebox.showerror(
                "设置错误",
                str(exc),
                parent=window,
            )
            return

        if not settings.privacy_blur_enabled:
            self._service.clear_privacy_blur()
            if not self._manual_privacy_blur_active:
                self._hide_privacy_blur()
        try:
            self._tray_icon.update_menu()
        except Exception as exc:
            LOGGER.debug(
                "Unable to refresh the tray menu after saving settings",
                exc_info=exc,
            )
        window.destroy()
        self._service.restart_async()

    def _exit_from_tray(
        self,
        icon: pystray.Icon,
        item: pystray.MenuItem,
    ) -> None:
        self._request_shutdown()

    def _request_shutdown(self) -> None:
        if self._shutdown_started.is_set():
            return
        self._shutdown_started.set()
        threading.Thread(
            target=self._shutdown,
            name="seat-sentinel-exit",
            daemon=True,
        ).start()

    def _shutdown(self) -> None:
        if self._registration_cancel_event is not None:
            self._registration_cancel_event.set()
        self._stop_privacy_hotkey()
        self._manual_privacy_blur_active = False
        self._hide_privacy_blur()
        self._service.shutdown()
        self._tray_icon.stop()
        self._root.after(0, self._root.quit)


def _run_self_test() -> int:
    """Validate packaged resources and NPU/CPU inference without a camera."""
    detector: Optional[FaceDetector] = None
    cpu_detector: Optional[FaceDetector] = None
    identity_recognizer: Optional[FaceIdentityRecognizer] = None
    test_application: Optional[TrayApplication] = None
    try:
        settings = SettingsStore().load()
        settings.apply_to_runtime()
        if not config.APPLICATION_ICON_PNG_PATH.is_file():
            raise RuntimeError("Application icon resource is missing")
        with Image.open(config.APPLICATION_ICON_PNG_PATH) as icon_source:
            if icon_source.size != (1024, 1024):
                raise RuntimeError("Application icon master has an invalid size")
            alpha_extrema = icon_source.convert("RGBA").getchannel("A").getextrema()
            if alpha_extrema != (0, 255):
                raise RuntimeError("Application icon transparency is invalid")
        test_application = TrayApplication()
        locked_icon = test_application._create_icon_image(locked=True)
        unlocked_icon = test_application._create_icon_image(locked=False)
        if locked_icon.size != (64, 64) or unlocked_icon.size != (64, 64):
            raise RuntimeError("Tray icon image has an invalid size")
        if locked_icon.tobytes() == unlocked_icon.tobytes():
            raise RuntimeError("Tray state icons must be visually distinct")
        privacy_menu_items = [
            item
            for item in test_application._tray_icon.menu.items
            if str(item.text) == "多人脸隐私模糊"
        ]
        if len(privacy_menu_items) != 1:
            raise RuntimeError(
                "Tray privacy-blur switch was not initialized"
            )
        if bool(privacy_menu_items[0].checked) != bool(
            settings.privacy_blur_enabled
        ):
            raise RuntimeError(
                "Tray privacy-blur switch does not match saved settings"
            )
        if bool(privacy_menu_items[0].enabled) != (
            settings.camera_monitoring_mode == "CONTINUOUS"
        ):
            raise RuntimeError(
                "Tray privacy-blur availability does not match camera mode"
            )
        manual_privacy_menu_items = [
            item
            for item in test_application._tray_icon.menu.items
            if str(item.text).startswith("手动毛玻璃（")
        ]
        if (
            len(manual_privacy_menu_items) != 1
            or settings.privacy_blur_hotkey
            not in str(manual_privacy_menu_items[0].text)
        ):
            raise RuntimeError(
                "Tray manual privacy hotkey was not initialized"
            )
        test_application._service._update_status(
            "lock_warning",
            "5 秒后自动锁屏",
        )
        test_application._refresh_lock_warning(schedule_next=False)
        test_application._root.update_idletasks()
        warning_window = test_application._lock_warning_window
        if (
            warning_window is None
            or not warning_window.winfo_exists()
            or not warning_window.winfo_viewable()
            or test_application._lock_warning_opacity <= 0
        ):
            raise RuntimeError("Lock warning overlay could not be shown")
        warning_style = _get_window_long_ptr(
            _native_top_level_handle(warning_window),
            GWL_EXSTYLE,
        )
        if (
            not warning_style & WS_EX_TOOLWINDOW
            or not warning_style & WS_EX_NOACTIVATE
            or warning_style & WS_EX_APPWINDOW
        ):
            raise RuntimeError(
                "Lock warning overlay can steal focus or enter the taskbar"
            )
        if (
            test_application._lock_warning_detail_variable is None
            or test_application._lock_warning_detail_variable.get()
            != "5 秒后自动锁屏"
        ):
            raise RuntimeError("Lock warning countdown was not rendered")
        test_application._service._update_status(
            "monitoring",
            "锁屏倒计时已取消",
        )
        test_application._refresh_lock_warning(schedule_next=False)
        test_application._root.update_idletasks()
        if warning_window.winfo_viewable():
            raise RuntimeError("Cancelled lock warning remained visible")

        sedentary_detail = (
            "已经连续坐了 30 分钟，请注意起身活动"
        )
        test_application._service._sedentary_reminder_signal.trigger(
            1800.0,
            sedentary_detail,
        )
        test_application._refresh_sedentary_reminder(schedule_next=False)
        test_application._root.update_idletasks()
        sedentary_window = test_application._sedentary_reminder_window
        if (
            sedentary_window is None
            or not sedentary_window.winfo_exists()
            or not sedentary_window.winfo_viewable()
            or test_application._sedentary_reminder_opacity <= 0
        ):
            raise RuntimeError("Sedentary reminder overlay could not be shown")
        sedentary_style = _get_window_long_ptr(
            _native_top_level_handle(sedentary_window),
            GWL_EXSTYLE,
        )
        if (
            not sedentary_style & WS_EX_TOOLWINDOW
            or not sedentary_style & WS_EX_NOACTIVATE
            or sedentary_style & WS_EX_APPWINDOW
        ):
            raise RuntimeError(
                "Sedentary reminder can steal focus or enter the taskbar"
            )
        if (
            test_application._sedentary_reminder_detail_variable is None
            or test_application._sedentary_reminder_detail_variable.get()
            != sedentary_detail
        ):
            raise RuntimeError("Sedentary duration was not rendered")
        test_application._sedentary_reminder_started_at = (
            time.monotonic()
            - config.SEDENTARY_REMINDER_DISPLAY_SECONDS
        )
        test_application._refresh_sedentary_reminder(schedule_next=False)
        test_application._root.update_idletasks()
        if sedentary_window.winfo_viewable():
            raise RuntimeError("Expired sedentary reminder remained visible")

        privacy_diagnostics = (
            test_application._privacy_blur_overlay.self_test()
        )
        if not privacy_diagnostics.get("ok"):
            raise RuntimeError(
                "DWM Desktop Acrylic privacy overlay self-test failed: "
                f"{privacy_diagnostics}"
            )
        if (
            privacy_diagnostics.get("strength_percent")
            != config.PRIVACY_ACRYLIC_STRENGTH_PERCENT
        ):
            raise RuntimeError(
                "DWM privacy strength does not match the configured value"
            )
        if int(privacy_diagnostics.get("monitor_count", 0)) < 1:
            raise RuntimeError(
                "DWM privacy self-test did not find an active monitor"
            )
        camera_names = test_application._camera_names()
        if not camera_names:
            LOGGER.warning(
                "Self-test could not enumerate a camera; "
                "camera selection remains available in the debug window"
            )
        elif settings.camera_name not in camera_names:
            LOGGER.warning(
                "Configured camera '%s' is not present on this computer; "
                "select an available camera from the debug window",
                settings.camera_name,
            )
        synthetic_preview = np.zeros((120, 160, 3), dtype=np.uint8)
        test_application._service._debug_frame_buffer.publish(
            frame_bgr=synthetic_preview,
            detections=(
                FaceDetection(
                    confidence=0.95,
                    xmin=20,
                    ymin=15,
                    xmax=100,
                    ymax=90,
                ),
            ),
            device="SELF-TEST",
            status="调试预览自检",
        )
        test_application._show_debug_window()
        test_application._root.update_idletasks()
        if (
            test_application._debug_window is None
            or not test_application._debug_window.winfo_exists()
            or test_application._debug_photo_image is None
        ):
            raise RuntimeError("Debug window could not be created")
        if (
            test_application._debug_window.cget("background")
            != "#c8d5e6"
            or test_application._debug_header_icon is None
        ):
            raise RuntimeError(
                "Light privacy-dashboard shell was not initialized"
            )
        if (
            test_application._debug_exit_button is None
            or test_application._debug_exit_button.cget("text")
            != "彻底退出程序"
        ):
            raise RuntimeError(
                "Debug window exit-program button was not initialized"
            )
        if (
            test_application._debug_toggle_button is None
            or test_application._debug_toggle_button.cget("background")
            != "#2f76e8"
        ):
            raise RuntimeError(
                "Primary monitoring control was not initialized"
            )
        if (
            test_application._debug_camera_combobox is None
            or test_application._debug_camera_variable is None
            or not test_application._debug_camera_variable.get().strip()
        ):
            raise RuntimeError("Debug camera selector was not initialized")
        debug_camera_values = tuple(
            test_application._debug_camera_combobox.cget("values")
        )
        if settings.camera_name not in debug_camera_values:
            raise RuntimeError(
                "Debug camera selector does not contain the configured camera"
            )
        test_application._debug_preview_fps = 1.9
        test_application._service._debug_frame_buffer.clear(
            status="摄像头待机",
            device="SELF-TEST",
            input_idle_seconds=7.5,
        )
        test_application._refresh_debug_window()
        test_application._root.update_idletasks()
        if (
            test_application._debug_metric_variables["input_idle"].get()
            != "7.5 s"
            or test_application._debug_metric_variables["preview_fps"].get()
            != "--"
        ):
            raise RuntimeError(
                "Standby idle timer or preview FPS was not rendered correctly"
            )
        test_application._show_settings()
        test_application._root.update_idletasks()
        settings_window = test_application._settings_window
        if settings_window is None or not settings_window.winfo_exists():
            raise RuntimeError("Settings window could not be created")

        def descendants(widget: tk.Misc) -> list[tk.Misc]:
            result: list[tk.Misc] = []
            for child in widget.winfo_children():
                result.append(child)
                result.extend(descendants(child))
            return result

        settings_widgets = descendants(settings_window)
        mode_selector_found = any(
            isinstance(widget, ttk.Combobox)
            and "仅本人（需注册）" in tuple(widget.cget("values"))
            for widget in settings_widgets
        )
        camera_mode_selectors = [
            widget
            for widget in settings_widgets
            if isinstance(widget, ttk.Combobox)
            and "键鼠空闲后监测" in tuple(widget.cget("values"))
        ]
        registration_button_found = any(
            isinstance(widget, ttk.Button)
            and widget.cget("text") in {"注册本人", "更新本人"}
            for widget in settings_widgets
        )
        privacy_switches = [
            widget
            for widget in settings_widgets
            if isinstance(widget, ttk.Checkbutton)
            and "多人脸隐私模糊" in str(widget.cget("text"))
        ]
        sedentary_switches = [
            widget
            for widget in settings_widgets
            if isinstance(widget, ttk.Checkbutton)
            and "启用久坐提醒" in str(widget.cget("text"))
        ]
        hotkey_label_found = any(
            isinstance(widget, ttk.Label)
            and widget.cget("text") == "手动毛玻璃快捷键"
            for widget in settings_widgets
        )
        settings_canvases = [
            widget
            for widget in settings_widgets
            if isinstance(widget, tk.Canvas)
        ]
        settings_scrollbars = [
            widget
            for widget in settings_widgets
            if isinstance(widget, ttk.Scrollbar)
            and str(widget.cget("orient")) == "vertical"
        ]
        save_buttons = [
            widget
            for widget in settings_widgets
            if isinstance(widget, ttk.Button)
            and widget.cget("text") == "保存并应用"
        ]
        fixed_save_button = (
            len(save_buttons) == 1
            and isinstance(save_buttons[0].master, ttk.Frame)
            and save_buttons[0].master.master is settings_window
        )
        if (
            not mode_selector_found
            or len(camera_mode_selectors) != 1
            or not registration_button_found
            or len(privacy_switches) != 1
            or len(sedentary_switches) != 1
            or not hotkey_label_found
            or len(settings_canvases) != 1
            or len(settings_scrollbars) != 1
            or not settings_canvases[0].cget("yscrollcommand")
            or not settings_scrollbars[0].cget("command")
            or not fixed_save_button
        ):
            raise RuntimeError(
                "Camera mode, registered-face, privacy, sedentary, or "
                "hotkey controls and settings scrolling were not initialized"
            )
        camera_mode_before_wheel = camera_mode_selectors[0].get()
        camera_mode_selectors[0].event_generate(
            "<MouseWheel>",
            delta=-120,
        )
        test_application._root.update_idletasks()
        if camera_mode_selectors[0].get() != camera_mode_before_wheel:
            raise RuntimeError(
                "Settings scrolling unexpectedly changed a combobox value"
            )
        camera_mode_selectors[0].set("键鼠空闲后监测")
        test_application._root.update_idletasks()
        if (
            "disabled" not in privacy_switches[0].state()
            or test_application._settings_privacy_blur_variable is None
            or test_application._settings_privacy_blur_variable.get()
        ):
            raise RuntimeError(
                "Idle-triggered mode did not disable privacy blur"
            )
        settings_window.destroy()
        if not test_application._debug_window.overrideredirect():
            raise RuntimeError("Custom title bar was not enabled")
        has_taskbar_style, is_unowned = _taskbar_window_state(
            test_application._debug_window
        )
        if not has_taskbar_style or not is_unowned:
            raise RuntimeError(
                "Debug window was not configured as a taskbar application"
            )
        if (
            len(test_application._debug_guide_variables) != 3
            or any(
                not variable.get().strip()
                for variable in (
                    test_application._debug_guide_variables.values()
                )
            )
        ):
            raise RuntimeError("Guide panel content was not initialized")
        test_application._minimize_debug_window()
        test_application._root.update_idletasks()
        if test_application._debug_window.state() != "withdrawn":
            raise RuntimeError("Debug window could not minimize to tray")
        test_application._show_debug_window()
        test_application._root.update_idletasks()
        if not test_application._debug_window.winfo_viewable():
            raise RuntimeError("Debug window could not reopen from tray")
        has_taskbar_style, is_unowned = _taskbar_window_state(
            test_application._debug_window
        )
        if not has_taskbar_style or not is_unowned:
            raise RuntimeError(
                "Debug window lost its taskbar style after reopening"
            )
        test_application._root.destroy()
        test_application = None

        session_locked = SessionMonitor().is_locked()
        detector = FaceDetector(
            config.MODEL_XML_PATH,
            config.MODEL_BIN_PATH,
            config.FACE_CONFIDENCE_THRESHOLD,
            preferred_device="NPU",
        )
        frame = np.zeros(
            (config.FRAME_HEIGHT, config.FRAME_WIDTH, 3),
            dtype=np.uint8,
        )
        face_detected = detector.has_face(frame)
        cpu_detector = FaceDetector(
            config.MODEL_XML_PATH,
            config.MODEL_BIN_PATH,
            config.FACE_CONFIDENCE_THRESHOLD,
            preferred_device="CPU",
        )
        cpu_face_detected = cpu_detector.has_face(frame)
        if cpu_detector.device != "CPU":
            raise RuntimeError("Explicit CPU selection was not honored")
        identity_recognizer = FaceIdentityRecognizer(
            config.LANDMARKS_MODEL_XML_PATH,
            config.LANDMARKS_MODEL_BIN_PATH,
            config.FACE_REIDENTIFICATION_MODEL_XML_PATH,
            config.FACE_REIDENTIFICATION_MODEL_BIN_PATH,
            preferred_device="NPU",
        )
        synthetic_face = FaceDetection(
            confidence=0.95,
            xmin=160,
            ymin=40,
            xmax=480,
            ymax=350,
        )
        identity_embedding = identity_recognizer.extract_embedding(
            frame,
            synthetic_face,
        )
        if identity_embedding.shape != (256,) or not np.isclose(
            np.linalg.norm(identity_embedding),
            1.0,
            atol=1e-4,
        ):
            raise RuntimeError("Face identity descriptor self-test failed")
        LOGGER.info(
            "Self-test passed | primary_device=%s | cpu_device=%s | "
            "identity_device=%s | "
            "session_locked=%s | monitors=%d | acrylic_strength=%d%% | "
            "synthetic_face=%s | cpu_face=%s",
            detector.device,
            cpu_detector.device,
            identity_recognizer.device,
            session_locked,
            int(privacy_diagnostics["monitor_count"]),
            int(privacy_diagnostics["strength_percent"]),
            face_detected,
            cpu_face_detected,
        )
        return 0
    except Exception:
        LOGGER.exception("Self-test failed")
        return 1
    finally:
        if test_application is not None:
            try:
                test_application._root.destroy()
            except tk.TclError:
                pass
        if detector is not None:
            detector.close()
        if cpu_detector is not None:
            cpu_detector.close()
        if identity_recognizer is not None:
            identity_recognizer.close()


def main() -> None:
    monitoring.configure_logging()
    if "--self-test" in sys.argv:
        raise SystemExit(_run_self_test())

    show_debug_on_start = "--debug-ui" in sys.argv
    try:
        instance = SingleInstance()
        if not instance.acquire():
            if show_debug_on_start and request_debug_window():
                return
            show_already_running_message()
            return
    except SingleInstanceError as exc:
        LOGGER.error("Single-instance initialization failed: %s", exc)
        messagebox.showerror(config.APPLICATION_TITLE, str(exc))
        return

    debug_signal: Optional[DebugWindowSignal] = None
    try:
        debug_signal = DebugWindowSignal()
        debug_signal.create()
        application = TrayApplication(
            debug_signal=debug_signal,
            show_debug_on_start=show_debug_on_start,
        )
        application.run()
    finally:
        if debug_signal is not None:
            debug_signal.close()
        instance.release()


if __name__ == "__main__":
    main()
