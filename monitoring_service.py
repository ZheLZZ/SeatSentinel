"""Monitoring lifecycle and exclusive ownership of the camera."""
from __future__ import annotations

import logging
import threading
from typing import Optional

import main as monitoring
from app_lock import AppLockSignal
from debug_frame import DebugFrameBuffer, DebugFrameSnapshot
from privacy_blur import PrivacyBlurSignal, PrivacyBlurSnapshot
from sedentary_reminder import (
    SedentaryDurationSignal, SedentaryDurationSnapshot,
    SedentaryReminderSignal, SedentaryReminderSnapshot,
)
from user_settings import SettingsError, SettingsStore

LOGGER = logging.getLogger("seat_sentinel.monitoring_service")


class MonitoringTransitionError(RuntimeError):
    """A camera owner cannot safely proceed after a failed transition."""


class MonitoringService:
    """Start, pause, resume, and restart the monitoring worker safely."""

    def __init__(self, settings_store: SettingsStore) -> None:
        self._settings_store = settings_store
        self._state_lock = threading.RLock()
        self._transition_lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._status_state = "stopped"
        self._status_detail = "监控尚未启动"
        self._shutting_down = False
        self._camera_owner: object | None = None
        self._request_generation = 0
        self._desired_running = False
        self._debug_frame_buffer = DebugFrameBuffer()
        self._privacy_blur_signal = PrivacyBlurSignal()
        self._sedentary_reminder_signal = SedentaryReminderSignal()
        self._sedentary_duration_signal = SedentaryDurationSignal()
        self.app_lock_signal = AppLockSignal()

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

    def should_resume_monitoring(self) -> bool:
        """Return user intent, including a start/restart still in progress."""
        with self._state_lock:
            return self._desired_running and not self._shutting_down

    def debug_snapshot(self) -> DebugFrameSnapshot:
        """Return the latest frame copied safely for the Tkinter thread."""
        return self._debug_frame_buffer.snapshot()

    def privacy_blur_snapshot(self) -> PrivacyBlurSnapshot:
        return self._privacy_blur_signal.snapshot()

    def sedentary_reminder_snapshot(self) -> SedentaryReminderSnapshot:
        return self._sedentary_reminder_signal.snapshot()

    def sedentary_duration_snapshot(self) -> SedentaryDurationSnapshot:
        return self._sedentary_duration_signal.snapshot()

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

    def _new_request(self, should_run: bool, force_restart: bool = False,
                     reload_settings: bool = False, *,
                     preserve_running_intent: bool = False) -> int | None:
        with self._state_lock:
            if self._shutting_down:
                return None
            if (should_run or reload_settings) and (
                self._camera_owner is not None or self.app_lock_signal.busy
            ):
                return None
            self._request_generation += 1
            if not preserve_running_intent:
                self._desired_running = should_run
            if (not should_run or force_restart) and self._stop_event is not None:
                self._stop_event.set()
            return self._request_generation

    def _request_is_current(self, generation: int | None) -> bool:
        return not self._shutting_down and (
            generation is None or generation == self._request_generation
        )

    def _clear_failed_request_intent(self, generation: int | None) -> None:
        with self._state_lock:
            if self._request_is_current(generation):
                self._desired_running = False

    def start_async(self) -> None:
        generation = self._new_request(True)
        if generation is None:
            return
        threading.Thread(
            target=self._transition,
            args=(True, False, False, generation),
            name="seat-sentinel-start",
            daemon=True,
        ).start()

    def pause_async(self) -> None:
        generation = self._new_request(False)
        if generation is None:
            return
        self._privacy_blur_signal.clear()
        self._sedentary_reminder_signal.clear()
        self._sedentary_duration_signal.clear()
        self._update_status("pausing", "正在暂停并释放摄像头")
        self._debug_frame_buffer.clear(
            "监控正在暂停 · 调试画面已清空"
        )
        threading.Thread(
            target=self._transition,
            args=(False, False, False, generation),
            name="seat-sentinel-pause",
            daemon=True,
        ).start()

    def pause_blocking(self, *, preserve_running_intent: bool = False) -> None:
        """Stop monitoring synchronously from a non-UI worker thread."""
        generation = self._new_request(
            False, preserve_running_intent=preserve_running_intent,
        )
        if generation is None:
            raise MonitoringTransitionError("程序正在退出")
        self._privacy_blur_signal.clear()
        self._sedentary_reminder_signal.clear()
        self._sedentary_duration_signal.clear()
        self._update_status("pausing", "正在暂停并释放摄像头")
        self._debug_frame_buffer.clear(
            "监控正在暂停 · 调试画面已清空"
        )
        if not self._transition(False, False, request_generation=generation):
            raise MonitoringTransitionError(self.status_detail())

    def acquire_camera(self, owner: object) -> None:
        """Reserve the camera only after the monitoring worker has stopped."""
        with self._transition_lock:
            with self._state_lock:
                if self._shutting_down:
                    raise MonitoringTransitionError("程序正在退出")
                if self._camera_owner is not None:
                    raise MonitoringTransitionError("摄像头正在被其他操作使用")
                if self.app_lock_signal.busy:
                    raise MonitoringTransitionError("应用正在锁屏，请解锁后重试")
                # Reserve before waiting so concurrent start requests cannot
                # queue up behind this transition and reopen the camera.
                self._camera_owner = owner
            try:
                self.pause_blocking(preserve_running_intent=True)
                if self.app_lock_signal.busy:
                    raise MonitoringTransitionError("应用正在锁屏，请解锁后重试")
            except Exception:
                self.release_camera(owner)
                raise

    def release_camera(self, owner: object) -> None:
        """A registration worker releases its lease after camera cleanup."""
        with self._transition_lock, self._state_lock:
            if self._camera_owner is owner:
                self._camera_owner = None
                self._request_generation += 1

    def refresh_settings_async(self) -> None:
        """Apply saved settings while preserving a paused monitoring state."""
        generation = self._new_request(False, reload_settings=True)
        if generation is None:
            return
        threading.Thread(
            target=self._transition,
            args=(False, False, True, generation),
            name="seat-sentinel-settings",
            daemon=True,
        ).start()

    def restart_async(self) -> None:
        generation = self._new_request(True, True)
        if generation is None:
            return
        self._privacy_blur_signal.clear()
        self._sedentary_reminder_signal.clear()
        self._sedentary_duration_signal.clear()
        self._update_status("starting", "正在应用设置并重启监控")
        self._debug_frame_buffer.clear(
            "正在重新启动监控 · 调试画面已清空"
        )
        threading.Thread(
            target=self._transition,
            args=(True, True, False, generation),
            name="seat-sentinel-restart",
            daemon=True,
        ).start()

    def _transition(self, should_run: bool, force_restart: bool,
                    reload_settings: bool = False,
                    request_generation: int | None = None) -> bool:
        with self._transition_lock:
            with self._state_lock:
                if not self._request_is_current(request_generation):
                    return False
                if self._camera_owner is not None and (should_run or reload_settings):
                    return False
                if should_run and self.app_lock_signal.busy:
                    return False
                current_thread = self._thread
                current_stop_event = self._stop_event

            if current_thread is not None and current_thread.is_alive():
                if should_run and not force_restart and self.is_running():
                    return True
                if current_stop_event is not None:
                    current_stop_event.set()
                current_thread.join(timeout=30.0)
                if current_thread.is_alive():
                    self._clear_failed_request_intent(request_generation)
                    self._update_status(
                        "error",
                        "监控线程未能及时停止，请退出后重试",
                    )
                    return False

            with self._state_lock:
                self._thread = None
                self._stop_event = None
                if not self._request_is_current(request_generation):
                    return False

            if should_run or reload_settings:
                try:
                    settings = self._settings_store.load()
                    settings.apply_to_runtime()
                except SettingsError as exc:
                    self._clear_failed_request_intent(request_generation)
                    self._update_status("error", f"设置错误：{exc}")
                    return False

            if not should_run:
                self._privacy_blur_signal.clear()
                self._sedentary_reminder_signal.clear()
                self._sedentary_duration_signal.clear()
                self._update_status("paused", "监控已暂停 · 摄像头已释放")
                self._debug_frame_buffer.clear(
                    "监控已暂停 · 调试画面已清空"
                )
                return True

            stop_event = threading.Event()
            with self._state_lock:
                if not self._request_is_current(request_generation):
                    return False
                worker = threading.Thread(
                    target=self._worker,
                    args=(stop_event, self._request_generation),
                    name="seat-sentinel-monitor",
                    daemon=True,
                )
                self._stop_event = stop_event
                self._thread = worker
                self._status_state = "starting"
                self._status_detail = "正在启动监控"
                worker.start()
            return True

    def _worker(self, stop_event: threading.Event, request_generation: int) -> None:
        try:
            exit_code = monitoring.run(
                stop_event=stop_event,
                status_callback=self._update_status,
                debug_frame_buffer=self._debug_frame_buffer,
                privacy_blur_signal=self._privacy_blur_signal,
                sedentary_reminder_signal=self._sedentary_reminder_signal,
                sedentary_duration_signal=self._sedentary_duration_signal,
                app_lock_signal=self.app_lock_signal,
            )
        except Exception:
            LOGGER.exception("Unexpected monitoring failure")
            self._update_status("error", "监控异常退出，请重新启动监控")
            exit_code = 1
        with self._state_lock:
            if self._thread is threading.current_thread():
                if not stop_event.is_set() and exit_code != 0:
                    self._clear_failed_request_intent(request_generation)
                    self._status_state = "error"
                    if self._status_detail == "监控已停止":
                        self._status_detail = "监控异常退出"

    def shutdown(self) -> None:
        with self._state_lock:
            self._shutting_down = True
            self._desired_running = False
            self._request_generation += 1
            if self._stop_event is not None:
                self._stop_event.set()
        with self._transition_lock:
            with self._state_lock:
                stop_event = self._stop_event
                worker = self._thread
            if stop_event is not None:
                stop_event.set()
            if worker is not None and worker.is_alive():
                worker.join(timeout=30.0)
            self._privacy_blur_signal.clear()
            self._sedentary_reminder_signal.clear()
            self._sedentary_duration_signal.clear()
            self._debug_frame_buffer.clear(
                "程序正在退出 · 调试画面已清空"
            )
