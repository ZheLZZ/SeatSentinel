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

from app_lock import UnlockGate
from private_test_unlock import load_private_test_unlock
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
user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
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
            or 0x70 <= vk <= 0x87 or alt or (ctrl and vk != 0x41))


class InputGuard:
    """Short hook callbacks on a dedicated message-pump thread, no key logging."""

    def __init__(self, on_test_chord: Callable[[str], None] | None = None) -> None:
        self.handles: tuple[int, ...] = ()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._on_test_chord = on_test_chord

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

    def _run(self) -> None:
        hooks = []
        modifiers: set[int] = set()
        held_function_keys: set[int] = set()
        groups = ({0x11, 0xA2, 0xA3}, {0x12, 0xA4, 0xA5}, {0x10, 0xA0, 0xA1})

        @HOOKPROC
        def keyboard(code, message, data):
            if code >= 0:
                key = ctypes.cast(data, ctypes.POINTER(KeyboardData)).contents
                vk = int(key.vkCode)
                down = message in {0x0100, 0x0104}
                for group in groups:
                    if vk in group:
                        if down:
                            modifiers.add(vk)
                        else:
                            modifiers.difference_update(group)
                if 0x70 <= vk <= 0x87:
                    if (down and vk not in held_function_keys
                            and all(modifiers & group for group in groups)
                            and self._on_test_chord is not None):
                        self._on_test_chord(f"Ctrl+Alt+Shift+F{vk - 0x6F}")
                    if down:
                        held_function_keys.add(vk)
                    else:
                        held_function_keys.discard(vk)
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
            if code >= 0 and message != 0x0200:  # allow pointer movement
                point = ctypes.cast(data, ctypes.POINTER(wintypes.POINT)).contents
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
        self._preview = False
        self._preview_deadline = 0.0
        self._generation = 0
        self._test_chords: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._test_unlock = None

    @property
    def active(self) -> bool:
        return bool(self.windows)

    def show(self, record: str, *, preview: bool = False) -> None:
        if self.active:
            return
        self._preview = preview
        self._generation += 1
        self._preview_deadline = time.monotonic() + 8
        self.gate = None if preview else UnlockGate(record)
        self._test_unlock = None if preview else load_private_test_unlock()
        self._verifying = False
        self._result = None
        try:
            self._rebuild()
            if not preview:
                self.guard = InputGuard(
                    self._test_chords.put if self._test_unlock is not None else None
                )
                self.guard.handles = self._handles()
                self.guard.start()
            self._tick()
        except Exception:
            self.close()
            raise

    def _handles(self) -> tuple[int, ...]:
        return tuple(user32.GetAncestor(w.winfo_id(), 2) for w in self.windows)

    def _rebuild(self) -> None:
        monitors = enumerate_monitor_work_areas()
        signature = tuple(m.monitor for m in monitors)
        if signature == self._signature and self.windows:
            return
        old_windows = self.windows
        self.windows = []
        try:
            for index, monitor in enumerate(monitors):
                window = tk.Toplevel(self.root, background="#101827")
                self.windows.append(window)
                window.withdraw()
                window.overrideredirect(True)
                window.attributes("-topmost", True)
                window.protocol("WM_DELETE_WINDOW", lambda: None)
                window.bind("<Alt-F4>", lambda event: "break")
                panel = tk.Frame(window, background="#101827")
                panel.place(relx=0.5, rely=0.5, anchor="center")
                tk.Label(panel, text="SeatSentinel", font=("Segoe UI", 14),
                         fg="#7da9ed", bg="#101827").pack(pady=(0, 24))
                tk.Label(panel, text="应用锁屏" if not self._preview else "锁屏界面预览",
                         font=("Microsoft YaHei UI", 30, "bold"),
                         fg="white", bg="#101827").pack(pady=(0, 12))
                tk.Label(panel, text="后台任务继续运行 · 输入独立密码恢复操作",
                         font=("Microsoft YaHei UI", 12), fg="#afbdcf",
                         bg="#101827").pack(pady=(0, 28))
                if index == 0:
                    self.password = tk.StringVar(master=window)
                    self.entry = tk.Entry(panel, textvariable=self.password, show="●",
                                          width=28, font=("Segoe UI", 16),
                                          justify="center", relief="flat")
                    self.entry.pack(ipady=10, pady=(0, 16))
                    self.entry.bind("<Return>", lambda event: self._submit())
                    self.message = tk.StringVar(master=window, value="请输入应用锁屏密码")
                    tk.Label(panel, textvariable=self.message, font=("Microsoft YaHei UI", 11),
                             fg="#f0c483", bg="#101827").pack(pady=(0, 16))
                    self.button = tk.Button(panel, text="解锁", command=self._submit,
                                            font=("Microsoft YaHei UI", 12), width=24,
                                            bg="#3676df", fg="white", relief="flat")
                    self.button.pack(ipady=6)
                    if self._preview:
                        self.entry.configure(state="disabled")
                        self.message.set("预览将在 8 秒后自动关闭，不拦截系统操作")
                        self.button.configure(text="关闭预览", command=self.close)
                        window.bind("<Escape>", lambda event: self.close())
                else:
                    tk.Label(panel, text="请在主屏输入密码", fg="white", bg="#101827",
                             font=("Microsoft YaHei UI", 13)).pack()
                    window.bind("<Button-1>", lambda event: self.entry.focus_force())
                tk.Label(panel, text="应用级遮挡保护 · 无法替代 Windows 安全锁屏",
                         fg="#788ba4", bg="#101827",
                         font=("Microsoft YaHei UI", 10)).pack(pady=(30, 0))
                window.update_idletasks()
                left, top, right, bottom = monitor.monitor
                window.deiconify()
                with _physical_pixel_context():
                    if not user32.SetWindowPos(user32.GetAncestor(window.winfo_id(), 2),
                                              -1, left, top, right-left, bottom-top, 0x0040):
                        raise ctypes.WinError(ctypes.get_last_error())
            self._signature = signature
            if self.guard is not None:
                self.guard.handles = self._handles()
            self.entry.focus_force()
        finally:
            for window in old_windows:
                window.destroy()

    def _submit(self) -> None:
        if self._preview or self._verifying or self.gate is None:
            return
        if self.gate.retry_seconds:
            self.message.set(f"尝试过于频繁，请 {self.gate.retry_seconds} 秒后重试")
            return
        password = self.password.get()
        self.password.set("")
        self._verifying = True
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
            for _ in range(10):
                try:
                    candidate = self._test_chords.get_nowait()
                except queue.Empty:
                    break
                if self._test_unlock is not None and not self._verifying:
                    self._verify_test_chord(candidate)
            if self._preview and time.monotonic() >= self._preview_deadline:
                self.close()
                return
            if self._result is not None:
                success, error = self._result
                self._result = None
                self._verifying = False
                if success:
                    self.close()
                    self.on_unlock()
                    return
                self.button.configure(state="normal")
                self.message.set(error or "密码不正确，请重试")
            if self.gate is not None and self.gate.retry_seconds:
                self.message.set(f"尝试过于频繁，请 {self.gate.retry_seconds} 秒后重试")
            self._rebuild()
            if self.guard is not None:
                if self.guard._error is not None or not self.guard._thread.is_alive():
                    raise RuntimeError("输入保护已停止")
                for window in self.windows:
                    window.lift()
                if user32.GetForegroundWindow() not in self._handles():
                    self.entry.focus_force()
            self._timer = self.root.after(200, self._tick)
        except Exception as exc:
            LOGGER.exception("Application lock failed")
            self.close()
            self.on_error(str(exc))

    def _verify_test_chord(self, candidate: str) -> None:
        """Never do a password derivation on the low-level hook thread."""
        self._verifying = True
        generation = self._generation
        test_unlock = self._test_unlock
        def verify() -> None:
            try:
                success = test_unlock.attempt(candidate)
            except Exception:
                success = False
            if self._generation == generation:
                self._result = (success, "验证未通过")
        threading.Thread(target=verify, name="seat-sentinel-test-unlock", daemon=True).start()

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
        self._signature = ()
        self.gate = None
        self._test_unlock = None
        self._test_chords = queue.SimpleQueue()
        self._result = None
