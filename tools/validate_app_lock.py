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
    import tempfile
    from dataclasses import replace
    from unittest.mock import patch
    from user_settings import AppSettings, SettingsStore
    from app_lock import hash_password
    from app_lock_windows import AppLockWindow, AwakeRequest, user32
    from private_test_unlock import PrivateTestUnlock
    from app_lock import UnlockGate
    from dwm_privacy import enumerate_monitor_work_areas, _physical_pixel_context, MonitorWorkArea
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
        # Native HWNDs on this isolated desktop, with injected display topology.
        # This exercises negative origins, portrait surfaces, primary switching,
        # DPI-change detection and unplug/error recovery without moving user windows.
        main_display = MonitorWorkArea((0, 0, 1920, 1080), (0, 0, 1920, 1040), True)
        left_display = MonitorWorkArea((-1080, -240, 0, 1680), (-1080, -240, 0, 1640), False)
        upper_display = MonitorWorkArea((1920, -720, 3200, 0), (1920, -720, 3200, -40), False)
        topologies = [(main_display, left_display), (main_display, left_display, upper_display),
                      (replace(main_display, primary=False), replace(left_display, primary=True), upper_display),
                      (main_display,)]
        lock.password.set("unfinished input")
        for topology in topologies:
            previous_windows = tuple(lock.windows)
            with patch("app_lock_windows.enumerate_monitor_work_areas", return_value=topology):
                lock._rebuild()
                root.update()
                expected = sorted(topology, key=lambda m: (not m.primary, m.monitor))
                assert len(lock.windows) == len(expected)
                assert lock.password.get() == "unfinished input"
                assert lock.guard.handles == lock._handles()
                for window, monitor in zip(lock.windows, expected):
                    rect = wintypes.RECT()
                    with _physical_pixel_context():
                        user32.GetWindowRect(user32.GetAncestor(window.winfo_id(), 2), ctypes.byref(rect))
                    assert (rect.left, rect.top, rect.right, rect.bottom) == monitor.monitor
                assert all(not window.winfo_exists() for window in previous_windows)
                if len(topology) == 3 and topology[1].primary:
                    capture_window(lock.windows[1], destination / "secondary-monitor.png")
                if len(topology) == 1:
                    before = tuple(lock.windows)
                    with patch("app_lock_windows.user32.GetDpiForWindow", return_value=144):
                        lock._rebuild()
                    assert tuple(lock.windows) != before
        # PrintWindow may return black for a simulated display entirely outside
        # the real desktop. Check portrait painting within its actual bounds.
        portrait = MonitorWorkArea((0, 0, 540, 960), (0, 0, 540, 920), True)
        with patch("app_lock_windows.enumerate_monitor_work_areas", return_value=[portrait]):
            lock._rebuild()
            root.update()
            capture_window(lock.windows[0], destination / "portrait-primary.png")
            from PIL import Image, ImageStat
            with Image.open(destination / "portrait-primary.png") as rendered:
                assert max(ImageStat.Stat(rendered).stddev) > 15, "Portrait surface did not paint"
            assert lock.entry.winfo_viewable() and lock.button.winfo_viewable()
        before = tuple(lock.windows)
        before_handles = lock.guard.handles
        with patch("app_lock_windows.enumerate_monitor_work_areas", side_effect=RuntimeError("transient display error")):
            lock._rebuild()
        assert tuple(lock.windows) == before and lock.guard.handles == before_handles
        assert all(window.winfo_exists() for window in before)
        with patch("app_lock_windows.enumerate_monitor_work_areas", return_value=[]):
            lock._rebuild()
        assert tuple(lock.windows) == before and lock.password.get() == "unfinished input"
        with patch.object(lock, "_draw_surface", side_effect=RuntimeError("transient surface failure")):
            lock._signature = ()
            lock._rebuild()
        assert tuple(lock.windows) == before and lock.guard.handles == before_handles
        assert all(window.winfo_exists() for window in before)
        lock._rebuild()
        lock.password.set("")
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
        with tempfile.TemporaryDirectory() as directory:
            application._settings_store = SettingsStore(path=Path(directory) / "settings.json")
            application._service._settings_store = application._settings_store
            application._settings_store.save(replace(AppSettings.defaults(),
                                                     app_lock_password_hash=hash_password("单")))
            manual_item = next(item for item in application._tray_icon.menu.items
                               if item.text == "应用锁屏")
            manual_item(application._tray_icon)
            application._poll_app_lock()
            def pump_app_until(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    application._root.update()
                    if predicate():
                        return
                    time.sleep(0.01)
                raise AssertionError("Manual lock UI timed out")
            pump_app_until(lambda: application._service.app_lock_signal.state == "locked")
            assert application._awake_request.active
            application._app_lock_window.password.set("单")
            application._app_lock_window._submit()
            pump_app_until(lambda: not application._application_locked())
            assert application._service.status_snapshot()[0] == "paused"
            pump_app_until(lambda: not application._awake_request.active)
            application._settings_store.save(replace(AppSettings.defaults(),
                                                     app_lock_password_hash=hash_password("")))
            manual_item(application._tray_icon)
            pump_app_until(lambda: application._service.app_lock_signal.state == "locked")
            application._app_lock_window._submit()
            pump_app_until(lambda: not application._application_locked())
            pump_app_until(lambda: not application._awake_request.active)
            application._show_settings()
            application._root.update()
            capture_window(application._settings_window, destination / "settings-window.png")
    finally:
        application._shutdown_started.set()
        application._app_lock_window.close()
        application._awake_request.update(False)
        application._root.destroy()
    print("PASS: real hooks, monitor coverage, 2/3-display simulations, negative origins, portrait/primary/DPI changes, unplug/error recovery, password, manual lock, preview and cleanup")
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
            argument = sys.argv.index("--packaged") + 1
            executable = (Path(sys.argv[argument]).resolve() if argument < len(sys.argv)
                          else ROOT / "dist" / "app-lock-preview" / "SeatSentinel" / "SeatSentinel.exe")
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
