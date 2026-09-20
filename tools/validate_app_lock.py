"""Exercise real lock windows/hooks on an isolated, never-selected desktop.

Run with the project's Python. The interactive user's desktop is not switched.
No camera, real credentials, system lock or user settings changes are needed.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def capture_window(window, path):
    """Render just the test HWND to a bitmap, not the user's screen."""
    from PIL import Image
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetDC.argtypes = [wintypes.HWND]
    user.GetDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    for name, args, restype in (
        ("CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
        ("CreateCompatibleBitmap", [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
        ("SelectObject", [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
        ("DeleteObject", [wintypes.HANDLE], wintypes.BOOL),
        ("DeleteDC", [wintypes.HDC], wintypes.BOOL),
        ("GetBitmapBits", [wintypes.HBITMAP, wintypes.LONG, ctypes.c_void_p], wintypes.LONG),
    ):
        func = getattr(gdi, name)
        func.argtypes, func.restype = args, restype
    hwnd = user.GetAncestor(window.winfo_id(), 2)
    rect = wintypes.RECT()
    assert user.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    dc = user.GetDC(hwnd)
    memory = gdi.CreateCompatibleDC(dc)
    bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
    previous = gdi.SelectObject(memory, bitmap)
    try:
        assert user.PrintWindow(hwnd, memory, 2), "PrintWindow failed"
        buffer = ctypes.create_string_buffer(width * height * 4)
        assert gdi.GetBitmapBits(bitmap, len(buffer), buffer) == len(buffer)
        Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1).save(path)
    finally:
        gdi.SelectObject(memory, previous)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user.ReleaseDC(hwnd, dc)


def child():
    import tkinter as tk
    from app_lock import hash_password
    from app_lock_windows import AppLockWindow, AwakeRequest, user32
    from private_test_unlock import PrivateTestUnlock
    from app_lock import UnlockGate
    from dwm_privacy import enumerate_monitor_work_areas, _physical_pixel_context
    destination = ROOT / "dist" / "app-lock-validation"
    destination.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.withdraw()
    errors, unlocked = [], []
    lock = AppLockWindow(root, lambda: unlocked.append(True), errors.append)
    wake = AwakeRequest()
    def pump_until(predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("UI operation timed out")
    try:
        wake.update(True)
        lock.show(hash_password("Test-only!482"))
        root.update()
        assert lock.active and lock.guard._thread.is_alive()
        assert len(lock.windows) == len(enumerate_monitor_work_areas())
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        for window, monitor in zip(lock.windows, enumerate_monitor_work_areas()):
            rect = wintypes.RECT()
            with _physical_pixel_context():
                assert user32.GetWindowRect(user32.GetAncestor(window.winfo_id(), 2), ctypes.byref(rect))
            assert (rect.left, rect.top, rect.right, rect.bottom) == monitor.monitor
        capture_window(lock.windows[0], destination / "lock-window.png")
        lock.password.set("wrong-password")
        lock._submit()
        pump_until(lambda: not lock._verifying)
        assert lock.active and not unlocked and not errors
        # Simulate a display topology refresh with the lock already active.
        lock._signature = ()
        lock._rebuild()
        assert lock.guard.handles == lock._handles()
        lock.password.set("Test-only!482")
        lock._submit()
        pump_until(lambda: bool(unlocked))
        assert not lock.active and lock.guard is None and not errors
        lock.show(hash_password("Test-only!482"))
        test_chord = "Ctrl+Alt+Shift+F6"
        lock._test_unlock = PrivateTestUnlock(UnlockGate(hash_password(test_chord)),
                                              time.time() + 60)
        lock._test_chords.put(test_chord)
        pump_until(lambda: len(unlocked) == 2)
        assert not lock.active and lock.guard is None and not errors
        lock.show("", preview=True)
        lock._preview_deadline = time.monotonic()
        pump_until(lambda: not lock.active)
        assert len(unlocked) == 2  # a preview never completes a real lock
    finally:
        lock.close()
        wake.update(False)
        root.destroy()
    from app import TrayApplication
    application = TrayApplication()
    try:
        application._show_settings()
        application._root.update()
        capture_window(application._settings_window, destination / "settings-window.png")
    finally:
        application._root.destroy()
    print("PASS: real hooks, monitor coverage, wrong/correct password, display rebuild, preview, wake cleanup and settings UI")
    if "--full-self-test" in sys.argv:
        from app import _run_self_test
        assert _run_self_test() == 0
        print("PASS: full application self-test")


def parent():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user.CreateDesktopW.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    name = "SeatSentinelTest_" + uuid.uuid4().hex
    desktop = user.CreateDesktopW(name, None, None, 0, 0x01FF, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    info = subprocess.STARTUPINFO()
    info.lpDesktop = "winsta0\\" + name
    info.dwFlags = subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    try:
        if "--packaged" in sys.argv:
            executable = ROOT / "dist" / "app-lock-preview" / "SeatSentinel" / "SeatSentinel.exe"
            command = [str(executable), "--self-test"]
        else:
            command = [sys.executable, __file__, "--child"] + sys.argv[1:]
        result = subprocess.run(command, cwd=ROOT, startupinfo=info,
                                capture_output=True, timeout=90)
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        if "--packaged" in sys.argv and result.returncode == 0:
            print("PASS: packaged EXE self-test on isolated desktop")
        return result.returncode
    finally:
        user.CloseDesktop(desktop)


if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
    else:
        raise SystemExit(parent())
