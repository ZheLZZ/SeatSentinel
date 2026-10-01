"""Windows application lock: opaque monitor covers, input filter, wake request.

This is an ordinary user process, not a Windows security boundary. Secure
attention, administrators, process termination and policy remain authoritative.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import queue
import threading
import time
import tkinter as tk
from typing import Callable
from PIL import ImageTk

from app_lock import UnlockGate
from lock_screen_theme import LandscapeTheme, LockScreenLayout, clock_text
from oled_saver import IDLE_SECONDS, ClockMotion, clock_color
from dwm_privacy import enumerate_monitor_work_areas, _physical_pixel_context


LOGGER = logging.getLogger("seat_sentinel.app_lock")
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = wintypes.HANDLE
user32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT,
                              ctypes.POINTER(ctypes.c_ubyte), wintypes.LPWSTR,
                              ctypes.c_int, wintypes.UINT, wintypes.HANDLE]
user32.ToUnicodeEx.restype = ctypes.c_int
user32.GetPhysicalCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetPhysicalCursorPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
kernel32.SetThreadExecutionState.restype = wintypes.DWORD


class KeyboardData(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t)]


def block_key(vk: int, *, alt: bool, ctrl: bool, own_focus: bool) -> bool:
    """Allow password editing only; no Windows/Alt/function/system shortcuts."""
    return (not own_focus or vk in {0x5B, 0x5C, 0x5D, 0x1B, 0x2C}
            or 0x70 <= vk <= 0x87 or alt
            or (ctrl and vk not in {0x41, 0x43, 0x56, 0x58, 0x20, 0x08,
                                   0x10, 0x11, 0xA0, 0xA1, 0xA2, 0xA3}))


class InputGuard:
    """Short hook callbacks on a dedicated message-pump thread, no key logging."""

    def __init__(self, *, direct_input: bool = False) -> None:
        self.handles: tuple[int, ...] = ()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self.last_activity = time.monotonic()
        self.saver_active = False
        self.direct_input = direct_input
        self.events: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=4096)
        self.accept_input = True
        self._layout = user32.GetKeyboardLayout(0)
        self._toggles = {vk: user32.GetKeyState(vk) & 1 for vk in (0x14, 0x90)}
        self._toggle_down: set[int] = set()

    def _emit(self, action: str, value: str = "") -> None:
        if self.accept_input:
            try:
                self.events.put_nowait((action, value))
            except queue.Full:
                self._error = RuntimeError("输入过快，请重新锁屏后重试")

    def _wake_saver(self) -> None:
        if self.direct_input and self.saver_active:
            # Queue the surface change ahead of any later password keys. Do not
            # wait for the UI timer, which would swallow fast follow-up input.
            self.saver_active = False
            self._emit("wake")

    def _direct_key(self, key: KeyboardData, down: bool) -> int:
        """Consume text before focus/shortcut filtering; never send it to apps.

        Shift/Caps/NumLock alone pass through so Windows owns their real state.
        Ctrl/Alt are deliberately ignored, so a lost remote key-up cannot latch
        a modifier and suppress all later password input.
        """
        vk = int(key.vkCode)
        if vk in self._toggles:
            if down and vk not in self._toggle_down:
                self._toggles[vk] ^= 1
                self._toggle_down.add(vk)
            elif not down:
                self._toggle_down.discard(vk)
        if vk in {0x10, 0xA0, 0xA1, 0x14, 0x90}:
            return 0
        if not down:
            return 1
        if vk in {0x0D, 0x08, 0x1B}:
            self._emit({0x0D: "submit", 0x08: "backspace", 0x1B: "clear"}[vk])
        elif vk == 0xE7:  # VK_PACKET: remote Unicode input, including UTF-16 pairs
            self._emit("text", chr(key.scanCode & 0xFFFF))
        elif vk not in {0x11, 0xA2, 0xA3, 0x12, 0xA4, 0xA5,
                        0x5B, 0x5C, 0x5D, 0x09, 0x2C} and not 0x70 <= vk <= 0x87:
            state = (ctypes.c_ubyte * 256)()
            state[0x10] = 0x80 if user32.GetAsyncKeyState(0x10) & 0x8000 else 0
            state[0x14] = self._toggles[0x14]
            state[0x90] = self._toggles[0x90]
            buffer = ctypes.create_unicode_buffer(16)
            count = user32.ToUnicodeEx(vk, key.scanCode, state, buffer, 16, 0, self._layout)
            if count > 0:
                self._emit("text", buffer[:count])
        return 1

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="seat-sentinel-lock-input")
        self._thread.start()
        if not self._ready.wait(3):
            self.close()
            raise RuntimeError("输入保护启动超时")
        if self._error is not None:
            self.close()
            raise RuntimeError(f"输入保护启动失败：{self._error}")

    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.last_activity)

    def _run(self) -> None:
        hooks = []
        modifiers: set[int] = set()
        wake_keys: set[int] = set()
        wake_buttons: set[int] = set()
        last_source = None
        cursor = wintypes.POINT()
        last_pointer = (cursor.x, cursor.y) if user32.GetPhysicalCursorPos(ctypes.byref(cursor)) else None
        button_ups = {0x0202: 0x0201, 0x0205: 0x0204, 0x0208: 0x0207, 0x020C: 0x020B}
        groups = ({0x11, 0xA2, 0xA3}, {0x12, 0xA4, 0xA5}, {0x10, 0xA0, 0xA1})

        @HOOKPROC
        def keyboard(code, message, data):
            nonlocal last_source
            if code >= 0:
                key = ctypes.cast(data, ctypes.POINTER(KeyboardData)).contents
                vk = int(key.vkCode)
                down = message in {0x0100, 0x0104}
                source = bool(key.flags & 0x10)
                if last_source is not None and source != last_source:
                    wake_keys.clear()
                    modifiers.clear()
                last_source = source
                self.last_activity = time.monotonic()
                for group in groups:
                    if vk in group:
                        if down:
                            modifiers.add(vk)
                        else:
                            modifiers.difference_update(group)
                # Consume the whole waking key gesture, including auto-repeat.
                if self.saver_active or vk in wake_keys:
                    self._wake_saver()
                    if down:
                        wake_keys.add(vk)
                    else:
                        wake_keys.discard(vk)
                    return 1
                if self.direct_input:
                    try:
                        if self._direct_key(key, down):
                            return 1
                    except Exception:
                        self._error = RuntimeError("密码输入处理失败")
                        return 1
                    return user32.CallNextHookEx(None, code, message, data)
                if block_key(
                    vk,
                    alt=bool(key.flags & 0x20 or modifiers & groups[1]),
                    ctrl=bool(user32.GetAsyncKeyState(0x11) & 0x8000 or modifiers & groups[0]),
                    own_focus=user32.GetForegroundWindow() in self.handles,
                ):
                    return 1
            return user32.CallNextHookEx(None, code, message, data)

        @HOOKPROC
        def mouse(code, message, data):
            nonlocal last_pointer
            if code >= 0:
                point = ctypes.cast(data, ctypes.POINTER(wintypes.POINT)).contents
                position = (point.x, point.y)
                activity = message != 0x0200 or position != last_pointer
                if activity:
                    self.last_activity = time.monotonic()
                last_pointer = position
                down_message = button_ups.get(message, message)
                if not self.saver_active and message in button_ups.values():
                    wake_buttons.discard(message)  # a new click repairs a lost release
                if self.saver_active or down_message in wake_buttons:
                    if activity:
                        self._wake_saver()
                    if message in button_ups.values():
                        wake_buttons.add(message)
                    elif message in button_ups:
                        wake_buttons.discard(down_message)
                    if message != 0x0200:
                        return 1
            if code >= 0 and message != 0x0200:  # allow pointer movement
                target = user32.GetAncestor(user32.WindowFromPoint(point), 2)
                if target not in self.handles:
                    return 1
            return user32.CallNextHookEx(None, code, message, data)

        try:
            for kind, callback in ((13, keyboard), (14, mouse)):
                handle = user32.SetWindowsHookExW(
                    kind, callback, kernel32.GetModuleHandleW(None), 0
                )
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                hooks.append(handle)
            self._ready.set()
            message = wintypes.MSG()
            while not self._stop.is_set():
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                self._stop.wait(0.005)
        except Exception as exc:
            self._error = exc
            LOGGER.exception("Application-lock input guard failed")
        finally:
            for handle in reversed(hooks):
                user32.UnhookWindowsHookEx(handle)
            self._ready.set()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        while not self.events.empty():
            self.events.get_nowait()


class AwakeRequest:
    """UI-thread-owned, process-lifetime request; never edits power policy."""

    def __init__(self) -> None:
        self.active = False
        self._previous = 0x80000000

    def update(self, enabled: bool) -> None:
        if enabled == self.active:
            return
        flags = 0x80000003 if enabled else self._previous
        previous = kernel32.SetThreadExecutionState(flags)
        if not previous:
            raise RuntimeError("Windows 未接受保持唤醒请求")
        if enabled:
            self._previous = previous
        self.active = enabled


class AppLockWindow:
    """All Tk operations stay on the UI thread; credential check is asynchronous."""

    def __init__(self, root: tk.Tk, on_unlock: Callable[[], None],
                 on_error: Callable[[str], None]) -> None:
        self.root = root
        self.on_unlock = on_unlock
        self.on_error = on_error
        self.windows: list[tk.Toplevel] = []
        self.guard: InputGuard | None = None
        self.gate: UnlockGate | None = None
        self._signature: tuple = ()
        self._timer: str | None = None
        self._result: tuple[bool, str] | None = None
        self._verifying = False
        self._retry_message_active = False
        self._preview = False
        self._preview_deadline = 0.0
        self._generation = 0
        self.password = tk.StringVar(master=root)
        self.message = tk.StringVar(master=root)
        self.password.trace_add("write", lambda *_: self._refresh_hint())
        self._canvases: list[tk.Canvas] = []
        self._clock_text: tuple[str, str] | None = None
        self._theme: LandscapeTheme | None = None
        self._last_topology_error = float("-inf")
        self._dpi_signature: tuple[int, ...] = ()
        self._oled_enabled = True
        self._saver_active = False
        self._saver_started = 0.0

    @property
    def active(self) -> bool:
        return bool(self.windows)

    def show(self, record: str, *, preview: bool = False, oled_protection: bool = True) -> None:
        if self.active:
            return
        self._preview = preview
        self._oled_enabled = oled_protection
        self._saver_active = False
        self._generation += 1
        self._preview_deadline = time.monotonic() + 8
        self.gate = None if preview else UnlockGate(record)
        self._verifying = False
        self._result = None
        self.password.set("")
        self.message.set("")
        self._retry_message_active = False
        self._theme = LandscapeTheme()
        try:
            self._rebuild()
            if not preview:
                self.guard = InputGuard(direct_input=True)
                self.guard.handles = self._handles()
                self.guard.start()
            self._tick()
        except Exception:
            self.close()
            raise

    def _handles(self) -> tuple[int, ...]:
        return tuple(user32.GetAncestor(w.winfo_id(), 2) for w in self.windows)

    def _draw_surface(self, window: tk.Toplevel, width: int, height: int,
                      primary: bool) -> tk.Canvas:
        layout = LockScreenLayout.for_size(width, height)
        canvas = tk.Canvas(window, width=width, height=height,
                           highlightthickness=0, borderwidth=0, background="#263e4b")
        canvas.pack(fill="both", expand=True)
        photo = ImageTk.PhotoImage(self._theme.render(layout, primary), master=self.root)
        canvas.wallpaper_photo = photo  # retain the Tk image for this display
        canvas.create_image(0, 0, image=photo, anchor="nw")
        scale = layout.scale
        font = lambda family, pixels: (family, -max(10, round(pixels * scale)))
        canvas.create_text(round(25 * scale), round(30 * scale), text="SeatSentinel",
                           anchor="w", fill="#f2f5f6", font=font("Segoe UI", 21))
        canvas.create_text(width / 2, height * 0.19, text="", fill="white",
                           font=font("Segoe UI Light", 144), tags="clock")
        canvas.create_text(width / 2, height * 0.30, text="", fill="#f5f6f7",
                           font=font("Microsoft YaHei UI", 29), tags="date")
        if primary:
            canvas.create_text(*layout.point(0.5, 0.21),
                               text="暂时离开" if not self._preview else "锁屏界面预览",
                               fill="white", font=font("Microsoft YaHei UI", 34))
            self.entry = tk.Label(canvas, text="直接输入密码 · 回车解锁",
                                  font=font("Microsoft YaHei UI", 19), relief="flat",
                                  background="#718792", foreground="white",
                                  borderwidth=0, takefocus=False)
            entry_width = round((layout.panel[2] - layout.panel[0]) * 0.74)
            canvas.create_window(*layout.point(0.5, 0.485), window=self.entry,
                                 width=entry_width, height=round(38 * scale))
            paste = canvas.create_text(*layout.point(0.5, 0.625),
                                       text="退格修改 · Esc 清空 · 点击粘贴密码",
                                       fill="#e2e9ec", font=font("Microsoft YaHei UI", 12))
            canvas.tag_bind(paste, "<Button-1>", lambda event: self._paste_password())
            self.button = tk.Button(canvas, text="解锁", command=self._submit,
                                    font=font("Microsoft YaHei UI", 21), bg="#4c91a0", fg="white",
                                    activebackground="#559dac", activeforeground="white",
                                    borderwidth=0, highlightthickness=0, relief="flat", cursor="hand2",
                                    state="disabled" if self._verifying else "normal")
            canvas.create_window(*layout.point(0.5, 0.775), window=self.button,
                                 width=entry_width, height=round(45 * scale))
            canvas.create_text(*layout.point(0.5, 0.945), text=self.message.get(),
                               fill="#fff2d2", font=font("Microsoft YaHei UI", 13), tags="message")
            self._primary_canvas = canvas
            if self._preview:
                self.entry.configure(state="disabled")
                self.message.set("预览将在 8 秒后自动关闭")
                self.button.configure(text="关闭预览", command=self.close)
                window.bind("<Escape>", lambda event: self.close())
        else:
            canvas.create_text(width / 2, height * 0.73, text="直接输入密码，按回车解锁",
                               fill="white", font=font("Microsoft YaHei UI", 24))
            window.bind("<Button-1>", lambda event: self.entry.focus_force())
        canvas.oled_overlay = None
        canvas.oled_font = font("Segoe UI Light", 104)
        canvas.oled_date_font = font("Microsoft YaHei UI", 19)
        canvas.oled_scale = scale
        canvas.surface_size = (width, height)
        if self._saver_active:
            self._show_saver_surface(canvas)
        return canvas

    @staticmethod
    def _show_saver_surface(canvas) -> None:
        if canvas.oled_overlay is None:
            overlay = tk.Canvas(canvas.master, background="black", highlightthickness=0,
                                borderwidth=0, cursor="none", takefocus=False)
            overlay.create_text(0, 0, text=clock_text()[0], fill=clock_color(0),
                                font=canvas.oled_font, tags=("oled_clock", "oled_group"))
            scale = canvas.oled_scale
            overlay.create_line(-22 * scale, 66 * scale, 22 * scale, 66 * scale,
                                fill="#91bbc3", width=max(1, round(scale)), tags=("oled_accent", "oled_group"))
            overlay.create_text(0, 88 * scale, text=clock_text()[1], fill="#bdcdd4",
                                font=canvas.oled_date_font, tags=("oled_date", "oled_group"))
            canvas.oled_overlay = overlay
            overlay.motion = None
            overlay.content_text = None
        overlay = canvas.oled_overlay
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.tk.call("raise", str(overlay))

    def _update_saver(self) -> None:
        if self._preview or self.guard is None:
            return
        now = time.monotonic()
        wanted = (self._oled_enabled and not self._verifying
                  and self.guard.idle_seconds() >= IDLE_SECONDS)
        self._set_saver(wanted)
        if self._saver_active:
            elapsed = now - self._saver_started
            for index, canvas in enumerate(self._canvases):
                self._position_saver_clock(canvas, elapsed, index)

    def _set_saver(self, active: bool) -> None:
        if active == self._saver_active:
            return
        self._saver_active = active
        if active:
            self.guard.saver_active = True
            self._saver_started = time.monotonic()
        for canvas in self._canvases:
            if active:
                self._show_saver_surface(canvas)
            else:
                canvas.oled_overlay.destroy()
                canvas.oled_overlay = None
        if not active:
            self.guard.saver_active = False
            self.entry.focus_force()

    @staticmethod
    def _position_saver_clock(canvas, elapsed, index) -> None:
        overlay = canvas.oled_overlay
        value = clock_text()
        scale = canvas.oled_scale
        # Measure only at a fixed local origin, never from rounded moving bounds.
        if value != overlay.content_text:
            overlay.itemconfigure("oled_clock", text=value[0])
            overlay.itemconfigure("oled_date", text=value[1])
            overlay.coords("oled_clock", 0, 0)
            overlay.coords("oled_accent", -22 * scale, 66 * scale, 22 * scale, 66 * scale)
            overlay.coords("oled_date", 0, 88 * scale)
            overlay.local_bounds = overlay.bbox("oled_group")
            overlay.content_text = value
        bounds = overlay.local_bounds
        if overlay.motion is None:
            overlay.motion = ClockMotion(index)
        x, y = overlay.motion.advance(*canvas.surface_size, bounds[2]-bounds[0],
                                       bounds[3]-bounds[1], elapsed)
        ox, oy = x - (bounds[0] + bounds[2]) / 2, y - (bounds[1] + bounds[3]) / 2
        overlay.coords("oled_clock", ox, oy)
        overlay.coords("oled_accent", ox - 22 * scale, oy + 66 * scale,
                       ox + 22 * scale, oy + 66 * scale)
        overlay.coords("oled_date", ox, oy + 88 * scale)
        overlay.itemconfigure("oled_clock", fill=clock_color(elapsed))

    def _refresh_hint(self) -> None:
        if not self.windows or not hasattr(self, "_primary_canvas"):
            return
        length = len(self.password.get())
        self.entry.configure(text=("●" * min(length, 24) + ("…" if length > 24 else ""))
                             if length else "直接输入密码 · 回车解锁")

    def _refresh_clock(self) -> None:
        value = clock_text()
        if value != self._clock_text:
            for canvas in self._canvases:
                canvas.itemconfigure("clock", text=value[0])
                canvas.itemconfigure("date", text=value[1])
            self._clock_text = value
        if self._canvases:
            self._primary_canvas.itemconfigure("message", text=self.message.get())
            self._refresh_hint()

    def _rebuild(self) -> None:
        new_windows: list[tk.Toplevel] = []
        old_entry = getattr(self, "entry", None)
        old_button = getattr(self, "button", None)
        old_canvas = getattr(self, "_primary_canvas", None)
        try:
            monitors = sorted(enumerate_monitor_work_areas(), key=lambda m: (not m.primary, m.monitor))
            if not monitors:
                raise RuntimeError("Windows temporarily reported no active display")
            signature = tuple((m.monitor, m.work, m.primary) for m in monitors)
            dpi_signature = tuple(user32.GetDpiForWindow(handle) for handle in self._handles())
            if signature == self._signature and self.windows and dpi_signature == self._dpi_signature:
                return
            canvases = []
            # Keep all existing covers until every replacement is ready. Creating
            # each HWND in the physical-pixel context avoids mixed-DPI gaps.
            with _physical_pixel_context():
                for index, monitor in enumerate(monitors):
                    left, top, right, bottom = monitor.monitor
                    window = tk.Toplevel(self.root, background="#263e4b")
                    new_windows.append(window)
                    window.withdraw()
                    window.overrideredirect(True)
                    window.attributes("-topmost", True)
                    window.protocol("WM_DELETE_WINDOW", lambda: None)
                    window.bind("<Alt-F4>", lambda event: "break")
                    window.update_idletasks()
                    handle = user32.GetAncestor(window.winfo_id(), 2)
                    if not user32.SetWindowPos(handle, -1, left, top, right-left, bottom-top, 0x0010):
                        raise ctypes.WinError(ctypes.get_last_error())
                    canvas = self._draw_surface(window, right-left, bottom-top, index == 0)
                    canvases.append(canvas)
                    window.update_idletasks()
                    window.deiconify()
                    if not user32.SetWindowPos(handle, -1, left, top, right-left, bottom-top, 0x0050):
                        raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            # A transient unplug/reconfiguration must never dismiss an existing lock.
            for window in new_windows:
                window.destroy()
            self.entry, self.button = old_entry, old_button
            self._primary_canvas = old_canvas
            if not self.windows:
                raise
            if time.monotonic() - self._last_topology_error > 5:
                LOGGER.exception("Display layout update failed; keeping existing lock covers")
                self._last_topology_error = time.monotonic()
            return
        old_windows = self.windows
        self.windows = new_windows
        self._canvases = canvases
        self._signature = signature
        self._dpi_signature = tuple(user32.GetDpiForWindow(handle) for handle in self._handles())
        if self.guard is not None:
            self.guard.handles = self._handles()
        for window in old_windows:
            window.destroy()
        self._clock_text = None
        self._refresh_clock()
        self.entry.focus_force()

    def _paste_password(self) -> None:
        if self._preview or self._saver_active or self._verifying:
            return
        try:
            value = self.root.clipboard_get()
        except tk.TclError:
            self.message.set("剪贴板中没有可粘贴的文字")
            return
        self.password.set(value)
        self.message.set("已粘贴，按回车解锁")

    def _drain_input(self) -> None:
        if self.guard is None:
            return
        while True:
            try:
                action, value = self.guard.events.get_nowait()
            except queue.Empty:
                break
            if action == "wake":
                self._set_saver(False)
                continue
            if self._verifying or self._saver_active:
                continue
            if action == "submit":
                self._submit()
            elif action == "clear":
                self._pending_surrogate = ""
                self.password.set("")
                self.message.set("")
            elif action == "backspace":
                self._pending_surrogate = ""
                self.password.set(self.password.get()[:-1])
            elif action == "text":
                # Recombine remote UTF-16 surrogate packets before giving Tk text.
                pending = getattr(self, "_pending_surrogate", "")
                combined = pending + value
                self._pending_surrogate = ""
                if combined and 0xD800 <= ord(combined[-1]) <= 0xDBFF:
                    self._pending_surrogate = combined[-1]
                    combined = combined[:-1]
                decoded = combined.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")
                self.password.set(self.password.get() + decoded)

    def _submit(self) -> None:
        if self._preview or self._saver_active or self._verifying or self.gate is None:
            return
        if self.gate.retry_seconds:
            self._retry_message_active = True
            self.message.set(f"尝试过于频繁，请 {self.gate.retry_seconds} 秒后重试")
            return
        password = self.password.get()
        self.password.set("")
        self._verifying = True
        if self.guard is not None:
            self.guard.accept_input = False
        self._retry_message_active = False
        self.button.configure(state="disabled")
        self.message.set("正在验证…")
        gate = self.gate
        generation = self._generation

        def verify() -> None:
            try:
                result = (gate.attempt(password), "")
            except Exception:
                LOGGER.exception("Password verification failed")
                result = (False, "密码验证失败，请重试")
            if self._generation == generation:
                self._result = result

        threading.Thread(target=verify, name="seat-sentinel-unlock", daemon=True).start()

    def _tick(self) -> None:
        self._timer = None
        if not self.active:
            return
        try:
            if self._preview and time.monotonic() >= self._preview_deadline:
                self.close()
                return
            if self._result is not None:
                success, error = self._result
                self._result = None
                self._verifying = False
                if self.guard is not None:
                    self.guard.accept_input = True
                if success:
                    self.close()
                    self.on_unlock()
                    return
                self.button.configure(state="normal")
                self.message.set(error or "密码不正确，请重试")
            if self.gate is not None and self.gate.retry_seconds:
                self._retry_message_active = True
                self.message.set(f"尝试过于频繁，请 {self.gate.retry_seconds} 秒后重试")
            elif self._retry_message_active and not self._verifying:
                self._retry_message_active = False
                self.message.set("请重新输入密码")
            self._drain_input()
            self._rebuild()
            self._refresh_clock()
            self._update_saver()
            if self.guard is not None:
                if self.guard._error is not None or not self.guard._thread.is_alive():
                    raise RuntimeError("输入保护已停止")
                for window in self.windows:
                    window.lift()
                if user32.GetForegroundWindow() not in self._handles():
                    self.entry.focus_force()
            self._timer = self.root.after(50, self._tick)
        except Exception as exc:
            LOGGER.exception("Application lock failed")
            self.close()
            self.on_error(str(exc))

    def close(self) -> None:
        self._generation += 1
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer = None
        if self.guard is not None:
            self.guard.close()
            self.guard = None
        for window in self.windows:
            window.destroy()
        self.windows.clear()
        self._canvases.clear()
        self._saver_active = False
        self.password.set("")
        self._pending_surrogate = ""
        self._theme = None
        self._signature = ()
        self._dpi_signature = ()
        self.gate = None
        self._result = None
