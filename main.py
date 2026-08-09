"""Console entry point for SeatSentinel."""

from __future__ import annotations

import logging
import logging.handlers
import math
import sys
import time
from enum import Enum
from threading import Event
from typing import Callable, Optional

import numpy as np
from numpy.typing import NDArray

import config
from activity_monitor import ActivityMonitor, ActivityMonitorError
from camera import Camera, CameraError
from debug_frame import DebugFrameBuffer
from detector import (
    DetectorError,
    DetectorInferenceError,
    FaceDetection,
    FaceDetector,
)
from face_identity import (
    FaceIdentityError,
    FaceIdentityInferenceError,
    FaceIdentityRecognizer,
    FaceTemplate,
    FaceTemplateError,
    FaceTemplateStore,
)
from privacy_blur import PrivacyBlurSignal, SecondPersonPrivacyGuard
from sedentary_reminder import (
    SedentaryReminderSignal,
    SedentaryTracker,
    format_sedentary_duration,
)
from session_monitor import SessionMonitor, SessionMonitorError
from windows_lock import WindowsLockError, lock_workstation


LOGGER = logging.getLogger("seat_sentinel")


class MonitorOutcome(Enum):
    """Reasons for pausing active camera monitoring."""

    LOCK_REQUESTED = "lock_requested"
    SESSION_LOCKED = "session_locked"
    SESSION_STATE_UNKNOWN = "session_state_unknown"
    INPUT_ACTIVE = "input_active"
    INPUT_MONITOR_UNAVAILABLE = "input_monitor_unavailable"
    PRESENCE_CONFIRMED_STANDBY = "presence_confirmed_standby"
    STOP_REQUESTED = "stop_requested"


StatusCallback = Callable[[str, str], None]


def _report_status(
    callback: Optional[StatusCallback],
    state: str,
    detail: str,
) -> None:
    if callback is not None:
        try:
            callback(state, detail)
        except Exception:
            LOGGER.exception("Status callback failed")


def _clear_debug_frame(
    debug_frame_buffer: Optional[DebugFrameBuffer],
    status: str,
    device: str = "",
    input_idle_seconds: Optional[float] = None,
) -> None:
    if debug_frame_buffer is None:
        return
    try:
        debug_frame_buffer.clear(
            status=status,
            device=device,
            input_idle_seconds=input_idle_seconds,
        )
    except Exception:
        LOGGER.exception("Unable to clear the in-memory debug frame")


def _clear_privacy_blur(
    privacy_blur_signal: Optional[PrivacyBlurSignal],
) -> None:
    if privacy_blur_signal is not None:
        privacy_blur_signal.clear()


def _publish_debug_frame(
    debug_frame_buffer: Optional[DebugFrameBuffer],
    frame: NDArray[np.uint8],
    detections: list[FaceDetection],
    device: str,
    status: str,
    face_absent_seconds: Optional[float],
    input_idle_seconds: Optional[float],
    startup_elapsed_seconds: Optional[float],
    should_lock: bool,
    inference_ms: Optional[float],
    presence_detected: bool,
    presence_mode: str,
) -> None:
    if debug_frame_buffer is None:
        return
    try:
        debug_frame_buffer.publish(
            frame_bgr=frame,
            detections=detections,
            device=device,
            status=status,
            face_absent_seconds=face_absent_seconds,
            input_idle_seconds=input_idle_seconds,
            startup_elapsed_seconds=startup_elapsed_seconds,
            should_lock=should_lock,
            inference_ms=inference_ms,
            presence_detected=presence_detected,
            presence_mode=presence_mode,
        )
    except Exception:
        LOGGER.exception("Unable to publish the in-memory debug frame")


def _wait_or_stop(
    stop_event: Optional[Event],
    seconds: float,
) -> bool:
    """Wait for a duration and return whether stop was requested."""
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(max(0.0, seconds))


def configure_logging() -> None:
    """Configure console and rotating local-file logging."""
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handlers: list[logging.Handler] = []

    if sys.stderr is not None:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

    try:
        log_directory = config.USER_DATA_DIRECTORY / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_directory / "seat-sentinel.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError:
        if sys.stderr is not None:
            print(
                "Warning: unable to initialize local log file",
                file=sys.stderr,
            )

    if not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers,
        force=True,
    )


def _face_status_text(face_detected: Optional[bool]) -> str:
    if face_detected is True:
        return "是"
    if face_detected is False:
        return "否"
    return "未知"


def _seconds_text(seconds: Optional[float]) -> str:
    if seconds is None:
        return "未知"
    return f"{seconds:.1f}"


def _observe_sedentary_time(
    tracker: Optional[SedentaryTracker],
    signal: Optional[SedentaryReminderSignal],
    present: Optional[bool],
    timestamp: float,
) -> None:
    if tracker is None:
        return
    seated_seconds = tracker.observe(present, timestamp)
    if seated_seconds is None:
        return
    duration_text = format_sedentary_duration(seated_seconds)
    LOGGER.info(
        "Continuous seated time reached %.1f seconds; showing reminder",
        seated_seconds,
    )
    if signal is not None:
        signal.trigger(
            seated_seconds,
            f"已经连续坐了 {duration_text}，请注意起身活动",
            timestamp,
        )


def camera_should_be_active(
    camera_monitoring_mode: str,
    input_idle_seconds: Optional[float],
    activation_idle_seconds: float,
) -> bool:
    """Return whether the selected camera strategy allows capture now."""
    if activation_idle_seconds <= 0:
        raise ValueError("Camera activation idle time must be positive")
    if camera_monitoring_mode == "CONTINUOUS":
        return True
    if camera_monitoring_mode != "IDLE_TRIGGERED":
        raise ValueError(
            f"Unsupported camera monitoring mode: {camera_monitoring_mode}"
        )
    return bool(
        input_idle_seconds is not None
        and input_idle_seconds >= activation_idle_seconds
    )


def evaluate_presence_auto_standby(
    presence_detected: Optional[bool],
    cycle_time: float,
    confirmed_since: Optional[float],
    required_seconds: float,
) -> tuple[Optional[float], bool]:
    """Track uninterrupted presence before an idle-mode camera standby."""
    if required_seconds < 0:
        raise ValueError("Presence confirmation time cannot be negative")
    if presence_detected is not True:
        return None, False

    started_at = cycle_time if confirmed_since is None else confirmed_since
    return started_at, cycle_time - started_at >= required_seconds


def evaluate_presence(
    detections: list[FaceDetection],
    presence_mode: str,
    previous_identity_match_streak: int,
    required_identity_confirmations: int,
) -> tuple[bool, int]:
    """Return current presence and the updated registered-face streak."""
    if presence_mode == "ANY_FACE":
        return bool(detections), 0
    if presence_mode != "REGISTERED_FACE":
        raise ValueError(f"Unsupported presence mode: {presence_mode}")
    if required_identity_confirmations < 1:
        raise ValueError("Identity confirmation count must be positive")
    raw_identity_match = any(
        detection.is_registered_person is True
        for detection in detections
    )
    identity_match_streak = (
        previous_identity_match_streak + 1
        if raw_identity_match
        else 0
    )
    return (
        identity_match_streak >= required_identity_confirmations,
        identity_match_streak,
    )


def evaluate_lock_warning(
    lock_conditions_met: bool,
    cycle_time: float,
    warning_started_at: Optional[float],
    warning_duration_seconds: float,
) -> tuple[Optional[float], Optional[int], bool]:
    """Advance a cancellable pre-lock warning countdown."""
    if warning_duration_seconds <= 0:
        raise ValueError("Lock warning duration must be positive")
    if not lock_conditions_met:
        return None, None, False
    started_at = (
        cycle_time
        if warning_started_at is None
        else warning_started_at
    )
    remaining_seconds = max(
        0.0,
        warning_duration_seconds - (cycle_time - started_at),
    )
    if remaining_seconds <= 0:
        return started_at, 0, True
    return started_at, max(1, math.ceil(remaining_seconds)), False


def monitor_until_session_pause(
    camera: Camera,
    detector: FaceDetector,
    activity_monitor: ActivityMonitor,
    session_monitor: SessionMonitor,
    identity_recognizer: Optional[FaceIdentityRecognizer] = None,
    face_template: Optional[FaceTemplate] = None,
    stop_event: Optional[Event] = None,
    status_callback: Optional[StatusCallback] = None,
    debug_frame_buffer: Optional[DebugFrameBuffer] = None,
    privacy_blur_signal: Optional[PrivacyBlurSignal] = None,
    sedentary_tracker: Optional[SedentaryTracker] = None,
    sedentary_reminder_signal: Optional[SedentaryReminderSignal] = None,
) -> MonitorOutcome:
    """Monitor until Windows locks or the session state becomes uncertain."""
    registered_face_mode = config.PRESENCE_MODE == "REGISTERED_FACE"
    if registered_face_mode and (
        identity_recognizer is None or face_template is None
    ):
        raise FaceIdentityError("仅本人模式尚未完成本人人脸注册")
    device_text = detector.device
    if identity_recognizer is not None:
        identity_device = identity_recognizer.device
        if identity_device and identity_device != detector.device:
            device_text = f"检测 {detector.device} / 识别 {identity_device}"
    monitoring_started_at = time.monotonic()
    last_seen_time = monitoring_started_at
    next_detection_at = monitoring_started_at
    next_camera_reconnect_at = monitoring_started_at
    lock_retry_not_before = monitoring_started_at
    last_status_log_at = float("-inf")
    last_camera_error_log_at = float("-inf")
    last_inference_error_log_at = float("-inf")
    last_activity_error_log_at = float("-inf")
    camera_was_healthy = True
    inference_was_healthy = True
    activity_was_healthy = True
    identity_match_streak = 0
    presence_confirmed_since: Optional[float] = None
    lock_warning_started_at: Optional[float] = None
    privacy_guard = (
        SecondPersonPrivacyGuard(
            confirmation_frames=(
                config.SECOND_PERSON_CONFIRMATION_FRAMES
            ),
            rearm_clear_seconds=(
                config.SECOND_PERSON_REARM_CLEAR_SECONDS
            ),
            auto_dismiss_owner_alone_seconds=(
                config.SECOND_PERSON_AUTO_DISMISS_SECONDS
            ),
        )
        if (
            config.CAMERA_MONITORING_MODE == "CONTINUOUS"
            and config.PRIVACY_BLUR_ENABLED
            and privacy_blur_signal is not None
        )
        else None
    )

    LOGGER.info(
        "Monitoring active; grace period is %.0f seconds",
        config.STARTUP_GRACE_PERIOD_SECONDS,
    )
    _report_status(
        status_callback,
        "monitoring",
        (
            f"正在监控 · 仅本人 · {device_text}"
            if registered_face_mode
            else f"正在监控 · 任意人脸 · {device_text}"
        ),
    )

    while True:
        if stop_event is not None and stop_event.is_set():
            _clear_privacy_blur(privacy_blur_signal)
            _clear_debug_frame(
                debug_frame_buffer,
                "监控已停止 · 调试画面已清空",
                device_text,
            )
            return MonitorOutcome.STOP_REQUESTED

        now = time.monotonic()
        wait_seconds = next_detection_at - now
        if wait_seconds > 0:
            if _wait_or_stop(stop_event, wait_seconds):
                _clear_privacy_blur(privacy_blur_signal)
                return MonitorOutcome.STOP_REQUESTED

        cycle_time = time.monotonic()
        next_detection_at = (
            cycle_time + config.DETECTION_INTERVAL_SECONDS
        )

        try:
            if session_monitor.is_locked():
                if sedentary_tracker is not None:
                    sedentary_tracker.reset()
                _clear_privacy_blur(privacy_blur_signal)
                _clear_debug_frame(
                    debug_frame_buffer,
                    "Windows 已锁定 · 调试画面已清空",
                    device_text,
                )
                LOGGER.info(
                    "Windows session is locked; pausing camera monitoring"
                )
                _report_status(
                    status_callback,
                    "locked",
                    "Windows 已锁定 · 摄像头已释放",
                )
                return MonitorOutcome.SESSION_LOCKED
        except SessionMonitorError as exc:
            _observe_sedentary_time(
                sedentary_tracker,
                sedentary_reminder_signal,
                None,
                cycle_time,
            )
            _clear_privacy_blur(privacy_blur_signal)
            _clear_debug_frame(
                debug_frame_buffer,
                "Windows 会话状态不明 · 调试画面已清空",
                device_text,
            )
            LOGGER.warning(
                "Windows session state is unknown; "
                "pausing camera monitoring: %s",
                exc,
            )
            _report_status(
                status_callback,
                "waiting",
                "会话状态不明 · 摄像头即将释放",
            )
            return MonitorOutcome.SESSION_STATE_UNKNOWN

        if config.CAMERA_MONITORING_MODE == "IDLE_TRIGGERED":
            try:
                activation_idle_seconds = (
                    activity_monitor.seconds_since_last_input()
                )
            except ActivityMonitorError as exc:
                _clear_privacy_blur(privacy_blur_signal)
                _clear_debug_frame(
                    debug_frame_buffer,
                    "无法确认键鼠状态 · 摄像头即将释放",
                    device_text,
                )
                LOGGER.warning(
                    "Unable to read keyboard/mouse activity in idle-triggered "
                    "mode; releasing the camera: %s",
                    exc,
                )
                _report_status(
                    status_callback,
                    "waiting",
                    "无法确认键鼠状态 · 摄像头即将释放",
                )
                return MonitorOutcome.INPUT_MONITOR_UNAVAILABLE

            if not camera_should_be_active(
                config.CAMERA_MONITORING_MODE,
                activation_idle_seconds,
                config.CAMERA_ACTIVATION_IDLE_SECONDS,
            ):
                _observe_sedentary_time(
                    sedentary_tracker,
                    sedentary_reminder_signal,
                    True,
                    cycle_time,
                )
                _clear_privacy_blur(privacy_blur_signal)
                _clear_debug_frame(
                    debug_frame_buffer,
                    "检测到键鼠操作 · 摄像头已进入待机",
                    device_text,
                )
                LOGGER.info(
                    "Keyboard/mouse activity resumed; releasing the camera"
                )
                _report_status(
                    status_callback,
                    "standby",
                    "检测到键鼠操作 · 摄像头已进入待机",
                )
                return MonitorOutcome.INPUT_ACTIVE

        camera_ok, frame = camera.read()
        inference_ok = False
        any_face_detected: Optional[bool] = None
        presence_detected: Optional[bool] = None
        detections: list[FaceDetection] = []
        inference_ms: Optional[float] = None

        if not camera_ok or frame is None:
            _clear_debug_frame(
                debug_frame_buffer,
                "摄像头读取失败 · 调试画面已清空",
                device_text,
            )
            # Unknown visual state is treated as presence. This starts a fresh
            # uninterrupted absence window after the camera recovers.
            last_seen_time = cycle_time
            identity_match_streak = 0
            if (
                cycle_time - last_camera_error_log_at
                >= config.ERROR_LOG_INTERVAL_SECONDS
            ):
                LOGGER.warning(
                    "Camera frame read failed; locking is disabled"
                )
                _report_status(
                    status_callback,
                    "camera_error",
                    "摄像头读取失败 · 正在重连",
                )
                last_camera_error_log_at = cycle_time
            camera_was_healthy = False
            if privacy_guard is not None:
                privacy_guard.mark_visual_state_unknown()

            if cycle_time >= next_camera_reconnect_at:
                camera.release()
                try:
                    camera.open()
                    LOGGER.info(
                        "Camera reconnected; starting a fresh absence window"
                    )
                    _report_status(
                        status_callback,
                        "monitoring",
                        f"摄像头已恢复 · {device_text}",
                    )
                except CameraError as exc:
                    LOGGER.warning(
                        "Camera reconnect failed; will retry: %s",
                        exc,
                    )
                next_camera_reconnect_at = (
                    cycle_time
                    + config.CAMERA_RECONNECT_INTERVAL_SECONDS
                )
        else:
            if not camera_was_healthy:
                LOGGER.info("Camera frame reading recovered")
            camera_was_healthy = True

            try:
                inference_started_at = time.perf_counter()
                detections = detector.detect_faces(frame)
                any_face_detected = bool(detections)
                if registered_face_mode:
                    assert identity_recognizer is not None
                    assert face_template is not None
                    detections = identity_recognizer.recognize_faces(
                        frame,
                        detections,
                        face_template,
                        config.FACE_MATCH_SIMILARITY_THRESHOLD,
                    )
                    presence_detected, identity_match_streak = (
                        evaluate_presence(
                            detections,
                            config.PRESENCE_MODE,
                            identity_match_streak,
                            config.IDENTITY_MATCH_CONFIRMATION_FRAMES,
                        )
                    )
                else:
                    presence_detected, identity_match_streak = (
                        evaluate_presence(
                            detections,
                            config.PRESENCE_MODE,
                            identity_match_streak,
                            config.IDENTITY_MATCH_CONFIRMATION_FRAMES,
                        )
                    )
                inference_ms = (
                    time.perf_counter() - inference_started_at
                ) * 1000.0
                inference_ok = True
                if (
                    config.PRIVACY_BLUR_ENABLED
                    and privacy_guard is not None
                ):
                    privacy_decision = privacy_guard.evaluate(
                        owner_confirmed=(
                            registered_face_mode
                            and presence_detected is True
                        ),
                        face_count=len(detections),
                        timestamp=cycle_time,
                    )
                    assert privacy_blur_signal is not None
                    if privacy_decision.activate:
                        detail = (
                            (
                                "检测到至少两个人 · 快速甩动鼠标，"
                                "或仅剩本人 3 秒后自动恢复"
                            )
                            if registered_face_mode
                            else (
                                "检测到至少两个人 · "
                                "快速甩动鼠标恢复"
                            )
                        )
                        if privacy_blur_signal.activate(detail):
                            LOGGER.info(
                                "Multiple faces confirmed; "
                                "privacy blur activated"
                            )
                            _report_status(
                                status_callback,
                                "privacy_blur",
                                detail,
                            )
                    elif (
                        privacy_decision.auto_dismiss
                        and privacy_blur_signal.dismiss()
                    ):
                        detail = (
                            "画面仅剩本人已满 3 秒 · "
                            "隐私模糊已自动解除"
                        )
                        LOGGER.info(
                            "Privacy blur automatically dismissed after "
                            "the registered user was alone for 3 seconds"
                        )
                        _report_status(
                            status_callback,
                            "monitoring",
                            detail,
                        )
                if not inference_was_healthy:
                    LOGGER.info("Model inference recovered")
                inference_was_healthy = True
            except (DetectorInferenceError, FaceIdentityInferenceError) as exc:
                _clear_debug_frame(
                    debug_frame_buffer,
                    "模型推理失败 · 调试画面已清空",
                    device_text,
                )
                # As with camera failure, do not count uncertain time as
                # confirmed absence.
                last_seen_time = cycle_time
                identity_match_streak = 0
                if (
                    cycle_time - last_inference_error_log_at
                    >= config.ERROR_LOG_INTERVAL_SECONDS
                ):
                    LOGGER.warning(
                        "Model inference failed; locking is disabled: %s",
                        exc,
                    )
                    _report_status(
                        status_callback,
                        "inference_error",
                        "模型推理异常 · 已禁止锁屏",
                    )
                    last_inference_error_log_at = cycle_time
                inference_was_healthy = False
                if privacy_guard is not None:
                    privacy_guard.mark_visual_state_unknown()

        _observe_sedentary_time(
            sedentary_tracker,
            sedentary_reminder_signal,
            (
                presence_detected
                if camera_ok and inference_ok
                else None
            ),
            cycle_time,
        )

        if presence_detected is True:
            last_seen_time = cycle_time

        if config.CAMERA_MONITORING_MODE == "IDLE_TRIGGERED":
            (
                presence_confirmed_since,
                should_enter_presence_standby,
            ) = evaluate_presence_auto_standby(
                presence_detected,
                cycle_time,
                presence_confirmed_since,
                config.CAMERA_PRESENCE_AUTO_STANDBY_SECONDS,
            )
            if should_enter_presence_standby:
                detail = (
                    "已连续确认在场 %.0f 秒 · 摄像头进入待机 · "
                    "%.0f 秒后复查"
                    % (
                        config.CAMERA_PRESENCE_AUTO_STANDBY_SECONDS,
                        config.CAMERA_PRESENCE_RECHECK_INTERVAL_SECONDS,
                    )
                )
                LOGGER.info(
                    "Presence confirmed for %.1f seconds; releasing the "
                    "camera for a %.1f-second recheck interval",
                    config.CAMERA_PRESENCE_AUTO_STANDBY_SECONDS,
                    config.CAMERA_PRESENCE_RECHECK_INTERVAL_SECONDS,
                )
                _clear_debug_frame(
                    debug_frame_buffer,
                    detail,
                    device_text,
                    input_idle_seconds=activation_idle_seconds,
                )
                _report_status(status_callback, "standby", detail)
                return MonitorOutcome.PRESENCE_CONFIRMED_STANDBY

        face_absent_seconds = max(0.0, cycle_time - last_seen_time)

        try:
            input_idle_seconds: Optional[float] = (
                activity_monitor.seconds_since_last_input()
            )
            if not activity_was_healthy:
                LOGGER.info("Keyboard/mouse activity monitoring recovered")
            activity_was_healthy = True
        except ActivityMonitorError as exc:
            input_idle_seconds = None
            if (
                cycle_time - last_activity_error_log_at
                >= config.ERROR_LOG_INTERVAL_SECONDS
            ):
                LOGGER.warning(
                    "Unable to read keyboard/mouse activity; "
                    "locking is disabled: %s",
                    exc,
                )
                last_activity_error_log_at = cycle_time
            activity_was_healthy = False

        startup_elapsed_seconds = cycle_time - monitoring_started_at
        lock_conditions_met = (
            camera_ok
            and inference_ok
            and presence_detected is False
            and face_absent_seconds
            >= config.FACE_ABSENCE_TIMEOUT_SECONDS
            and input_idle_seconds is not None
            and input_idle_seconds >= config.INPUT_IDLE_TIMEOUT_SECONDS
            and startup_elapsed_seconds
            >= config.STARTUP_GRACE_PERIOD_SECONDS
            and cycle_time >= lock_retry_not_before
        )
        previous_lock_warning_started_at = lock_warning_started_at
        (
            lock_warning_started_at,
            lock_warning_seconds,
            should_lock,
        ) = evaluate_lock_warning(
            lock_conditions_met,
            cycle_time,
            lock_warning_started_at,
            config.LOCK_WARNING_SECONDS,
        )
        warning_started = (
            previous_lock_warning_started_at is None
            and lock_warning_started_at is not None
        )
        warning_cancelled = (
            previous_lock_warning_started_at is not None
            and lock_warning_started_at is None
        )
        if warning_started:
            LOGGER.info(
                "Lock conditions met; starting %.1f-second warning",
                config.LOCK_WARNING_SECONDS,
            )
        elif warning_cancelled:
            LOGGER.info(
                "Presence or input resumed; lock warning cancelled"
            )

        if (
            camera_ok
            and frame is not None
            and inference_ok
            and presence_detected is not None
        ):
            _publish_debug_frame(
                debug_frame_buffer,
                frame,
                detections,
                device_text,
                (
                    (
                        f"仅本人模式 · 已确认本人 · 共 {len(detections)} 张人脸"
                        if presence_detected
                        else (
                            "仅本人模式 · 正在连续确认本人"
                            if any(
                                detection.is_registered_person is True
                                for detection in detections
                            )
                            else (
                                f"仅本人模式 · {len(detections)} 张人脸均非本人"
                                if detections
                                else "仅本人模式 · 未发现人脸"
                            )
                        )
                    )
                    if registered_face_mode
                    else (
                        f"任意人脸模式 · 发现 {len(detections)} 张人脸"
                        if any_face_detected
                        else "任意人脸模式 · 未发现人脸"
                    )
                ),
                face_absent_seconds,
                input_idle_seconds,
                startup_elapsed_seconds,
                should_lock,
                inference_ms,
                presence_detected,
                config.PRESENCE_MODE,
            )

        if (
            cycle_time - last_status_log_at
            >= config.STATUS_LOG_INTERVAL_SECONDS
            or lock_warning_seconds is not None
            or warning_cancelled
            or should_lock
        ):
            LOGGER.info(
                "状态 | 在场判断=%s | 距上次确认在场=%s秒 | "
                "距上次键鼠活动=%s秒 | 推理设备=%s | 达到锁屏条件=%s",
                _face_status_text(presence_detected),
                _seconds_text(face_absent_seconds),
                _seconds_text(input_idle_seconds),
                device_text,
                "是" if should_lock else "否",
            )
            if lock_warning_seconds is not None and not should_lock:
                _report_status(
                    status_callback,
                    "lock_warning",
                    f"{lock_warning_seconds} 秒后自动锁屏",
                )
            elif (
                privacy_blur_signal is not None
                and privacy_blur_signal.snapshot().active
            ):
                _report_status(
                    status_callback,
                    "privacy_blur",
                    privacy_blur_signal.snapshot().detail,
                )
            elif warning_cancelled:
                _report_status(
                    status_callback,
                    "monitoring",
                    "检测到在场或键鼠操作 · 已取消锁屏",
                )
            else:
                _report_status(
                    status_callback,
                    "monitoring",
                    "在场=%s · 离席=%s秒 · 键鼠空闲=%s秒 · %s · "
                    "锁屏条件=%s"
                    % (
                        _face_status_text(presence_detected),
                        _seconds_text(face_absent_seconds),
                        _seconds_text(input_idle_seconds),
                        device_text,
                        "是" if should_lock else "否",
                    ),
                )
            last_status_log_at = cycle_time

        if not should_lock:
            continue

        # Re-read input state immediately before locking. Any API failure or
        # recent input cancels this attempt and lets monitoring continue.
        try:
            final_input_idle_seconds = (
                activity_monitor.seconds_since_last_input()
            )
        except ActivityMonitorError as exc:
            LOGGER.warning(
                "Final keyboard/mouse check failed; lock cancelled: %s",
                exc,
            )
            lock_warning_started_at = None
            _report_status(
                status_callback,
                "monitoring",
                "无法确认键鼠状态 · 已取消锁屏",
            )
            continue

        if (
            final_input_idle_seconds
            < config.INPUT_IDLE_TIMEOUT_SECONDS
        ):
            LOGGER.info(
                "Recent keyboard/mouse input detected during final check; "
                "lock cancelled"
            )
            lock_warning_started_at = None
            _report_status(
                status_callback,
                "monitoring",
                "检测到键鼠操作 · 已取消锁屏",
            )
            continue

        LOGGER.warning(
            "No face for %.1f seconds and no input for %.1f seconds; "
            "locking Windows",
            face_absent_seconds,
            final_input_idle_seconds,
        )
        _report_status(
            status_callback,
            "locking",
            "达到条件 · 正在锁定 Windows",
        )
        _clear_debug_frame(
            debug_frame_buffer,
            "正在锁定 Windows · 调试画面已清空",
            device_text,
        )
        try:
            lock_workstation()
            LOGGER.info("LockWorkStation completed successfully")
            _report_status(
                status_callback,
                "locked",
                "Windows 已锁定 · 等待解锁",
            )
            _clear_privacy_blur(privacy_blur_signal)
            return MonitorOutcome.LOCK_REQUESTED
        except WindowsLockError as exc:
            LOGGER.error("%s", exc)
            last_seen_time = cycle_time
            lock_warning_started_at = None
            lock_retry_not_before = (
                cycle_time + config.LOCK_RETRY_COOLDOWN_SECONDS
            )
            _report_status(
                status_callback,
                "monitoring",
                "锁屏调用失败 · 将稍后重试",
            )


def wait_for_session_ready(
    session_monitor: SessionMonitor,
    require_lock_transition: bool,
    stop_event: Optional[Event] = None,
    status_callback: Optional[StatusCallback] = None,
    debug_frame_buffer: Optional[DebugFrameBuffer] = None,
    debug_device: str = "",
    sedentary_tracker: Optional[SedentaryTracker] = None,
    sedentary_reminder_signal: Optional[SedentaryReminderSignal] = None,
) -> Optional[bool]:
    """Wait for a reliably unlocked session.

    Returns whether a locked state was observed before unlock.
    """
    wait_started_at = time.monotonic()
    last_error_log_at = float("-inf")
    saw_locked = False
    unlocked_confirmations = 0
    waiting_message_logged = False

    while True:
        if stop_event is not None and stop_event.is_set():
            _clear_debug_frame(
                debug_frame_buffer,
                "监控已停止 · 调试画面已清空",
                debug_device,
            )
            return None

        now = time.monotonic()
        try:
            locked = session_monitor.is_locked()
        except SessionMonitorError as exc:
            _observe_sedentary_time(
                sedentary_tracker,
                sedentary_reminder_signal,
                None,
                now,
            )
            _clear_debug_frame(
                debug_frame_buffer,
                "无法读取 Windows 会话状态 · 调试画面已清空",
                debug_device,
            )
            unlocked_confirmations = 0
            if (
                now - last_error_log_at
                >= config.SESSION_STATE_LOG_INTERVAL_SECONDS
            ):
                LOGGER.warning(
                    "Unable to read Windows session state; "
                    "camera remains released: %s",
                    exc,
                )
                _report_status(
                    status_callback,
                    "waiting",
                    "无法读取 Windows 会话状态 · 摄像头保持关闭",
                )
                last_error_log_at = now
            if _wait_or_stop(
                stop_event,
                config.SESSION_STATE_POLL_INTERVAL_SECONDS,
            ):
                return None
            continue

        if locked:
            if sedentary_tracker is not None:
                sedentary_tracker.reset()
            _clear_debug_frame(
                debug_frame_buffer,
                "Windows 已锁定 · 调试画面已清空",
                debug_device,
            )
            saw_locked = True
            unlocked_confirmations = 0
            if not waiting_message_logged:
                LOGGER.info(
                    "Windows is locked; camera released, waiting for unlock"
                )
                _report_status(
                    status_callback,
                    "locked",
                    "Windows 已锁定 · 摄像头已释放",
                )
                waiting_message_logged = True
        elif saw_locked or not require_lock_transition:
            unlocked_confirmations += 1
            if (
                unlocked_confirmations
                >= config.UNLOCK_CONFIRMATION_POLLS
            ):
                if saw_locked:
                    LOGGER.info(
                        "Windows unlock confirmed; preparing to resume"
                    )
                    _report_status(
                        status_callback,
                        "starting",
                        "已解锁 · 准备恢复监控",
                    )
                return saw_locked
        else:
            unlocked_confirmations = 0
            if (
                now - wait_started_at
                >= config.LOCK_TRANSITION_TIMEOUT_SECONDS
            ):
                LOGGER.error(
                    "Windows did not report a locked state within %.0f "
                    "seconds; monitoring will restart safely",
                    config.LOCK_TRANSITION_TIMEOUT_SECONDS,
                )
                return False

        if _wait_or_stop(
            stop_event,
            config.SESSION_STATE_POLL_INTERVAL_SECONDS,
        ):
            return None


def wait_for_camera_activation(
    activity_monitor: ActivityMonitor,
    session_monitor: SessionMonitor,
    stop_event: Optional[Event] = None,
    status_callback: Optional[StatusCallback] = None,
    debug_frame_buffer: Optional[DebugFrameBuffer] = None,
    debug_device: str = "",
    sedentary_tracker: Optional[SedentaryTracker] = None,
    sedentary_reminder_signal: Optional[SedentaryReminderSignal] = None,
    presence_recheck_not_before: Optional[float] = None,
) -> Optional[bool]:
    """Wait until the configured strategy permits opening the camera.

    Returns False when Windows locks before activation and None when stopped.
    """
    if config.CAMERA_MONITORING_MODE == "CONTINUOUS":
        return True

    last_session_error_log_at = float("-inf")
    last_activity_error_log_at = float("-inf")
    last_reported_remaining: Optional[int] = None
    effective_recheck_not_before = presence_recheck_not_before

    while True:
        if stop_event is not None and stop_event.is_set():
            _clear_debug_frame(
                debug_frame_buffer,
                "监控已停止 · 调试画面已清空",
                debug_device,
            )
            return None

        now = time.monotonic()
        try:
            if session_monitor.is_locked():
                if sedentary_tracker is not None:
                    sedentary_tracker.reset()
                _clear_debug_frame(
                    debug_frame_buffer,
                    "Windows 已锁定 · 摄像头保持关闭",
                    debug_device,
                )
                _report_status(
                    status_callback,
                    "locked",
                    "Windows 已锁定 · 摄像头保持关闭",
                )
                return False
        except SessionMonitorError as exc:
            _observe_sedentary_time(
                sedentary_tracker,
                sedentary_reminder_signal,
                None,
                now,
            )
            _clear_debug_frame(
                debug_frame_buffer,
                "会话状态不明 · 摄像头保持关闭",
                debug_device,
            )
            if (
                now - last_session_error_log_at
                >= config.SESSION_STATE_LOG_INTERVAL_SECONDS
            ):
                LOGGER.warning(
                    "Session state unavailable while camera is in standby: %s",
                    exc,
                )
                _report_status(
                    status_callback,
                    "waiting",
                    "会话状态不明 · 摄像头保持关闭",
                )
                last_session_error_log_at = now
            if _wait_or_stop(
                stop_event,
                config.SESSION_STATE_POLL_INTERVAL_SECONDS,
            ):
                return None
            continue

        try:
            input_idle_seconds = activity_monitor.seconds_since_last_input()
        except ActivityMonitorError as exc:
            _observe_sedentary_time(
                sedentary_tracker,
                sedentary_reminder_signal,
                None,
                now,
            )
            _clear_debug_frame(
                debug_frame_buffer,
                "无法确认键鼠状态 · 摄像头保持关闭",
                debug_device,
            )
            if (
                now - last_activity_error_log_at
                >= config.ERROR_LOG_INTERVAL_SECONDS
            ):
                LOGGER.warning(
                    "Unable to read keyboard/mouse activity; camera remains "
                    "closed in idle-triggered mode: %s",
                    exc,
                )
                _report_status(
                    status_callback,
                    "waiting",
                    "无法确认键鼠状态 · 摄像头保持关闭",
                )
                last_activity_error_log_at = now
            if _wait_or_stop(
                stop_event,
                config.SESSION_STATE_POLL_INTERVAL_SECONDS,
            ):
                return None
            continue

        if input_idle_seconds < config.CAMERA_ACTIVATION_IDLE_SECONDS:
            # Real keyboard or mouse activity starts a fresh idle cycle and
            # cancels any remaining presence-confirmed standby interval.
            effective_recheck_not_before = None

        idle_activation_ready = camera_should_be_active(
            config.CAMERA_MONITORING_MODE,
            input_idle_seconds,
            config.CAMERA_ACTIVATION_IDLE_SECONDS,
        )
        recheck_remaining_seconds = (
            max(0.0, effective_recheck_not_before - now)
            if effective_recheck_not_before is not None
            else 0.0
        )
        if idle_activation_ready and recheck_remaining_seconds <= 0:
            LOGGER.info(
                "Keyboard/mouse idle for %.1f seconds; activating camera",
                input_idle_seconds,
            )
            _report_status(
                status_callback,
                "starting",
                "键鼠已空闲 %.0f 秒 · 正在开启摄像头"
                % input_idle_seconds,
            )
            return True

        _observe_sedentary_time(
            sedentary_tracker,
            sedentary_reminder_signal,
            True,
            now,
        )
        if idle_activation_ready and recheck_remaining_seconds > 0:
            remaining_seconds = max(1, math.ceil(recheck_remaining_seconds))
            detail = (
                f"已确认在场 · 摄像头待机 · {remaining_seconds} 秒后复查"
            )
        else:
            remaining_seconds = max(
                1,
                math.ceil(
                    config.CAMERA_ACTIVATION_IDLE_SECONDS
                    - input_idle_seconds
                ),
            )
            detail = (
                f"键鼠活跃 · 摄像头待机 · "
                f"空闲满 {config.CAMERA_ACTIVATION_IDLE_SECONDS:g} 秒后开启"
            )
        _clear_debug_frame(
            debug_frame_buffer,
            detail,
            debug_device,
            input_idle_seconds=input_idle_seconds,
        )
        if remaining_seconds != last_reported_remaining:
            _report_status(status_callback, "standby", detail)
            last_reported_remaining = remaining_seconds

        if _wait_or_stop(
            stop_event,
            config.SESSION_STATE_POLL_INTERVAL_SECONDS,
        ):
            return None


def open_camera_when_session_ready(
    camera: Camera,
    session_monitor: SessionMonitor,
    activity_monitor: Optional[ActivityMonitor] = None,
    stop_event: Optional[Event] = None,
    status_callback: Optional[StatusCallback] = None,
    debug_frame_buffer: Optional[DebugFrameBuffer] = None,
    debug_device: str = "",
    sedentary_tracker: Optional[SedentaryTracker] = None,
    sedentary_reminder_signal: Optional[SedentaryReminderSignal] = None,
) -> Optional[bool]:
    """Open the camera, retrying failures while the session is unlocked."""
    last_error_log_at = float("-inf")

    while True:
        if stop_event is not None and stop_event.is_set():
            _clear_debug_frame(
                debug_frame_buffer,
                "监控已停止 · 调试画面已清空",
                debug_device,
            )
            return None

        now = time.monotonic()
        try:
            if session_monitor.is_locked():
                if sedentary_tracker is not None:
                    sedentary_tracker.reset()
                _clear_debug_frame(
                    debug_frame_buffer,
                    "Windows 已锁定 · 调试画面已清空",
                    debug_device,
                )
                return False
        except SessionMonitorError as exc:
            _observe_sedentary_time(
                sedentary_tracker,
                sedentary_reminder_signal,
                None,
                now,
            )
            _clear_debug_frame(
                debug_frame_buffer,
                "会话状态不明 · 调试画面已清空",
                debug_device,
            )
            if (
                now - last_error_log_at
                >= config.SESSION_STATE_LOG_INTERVAL_SECONDS
            ):
                LOGGER.warning(
                    "Session state unavailable; camera remains closed: %s",
                    exc,
                )
                _report_status(
                    status_callback,
                    "waiting",
                    "会话状态不明 · 摄像头保持关闭",
                )
                last_error_log_at = now
            if _wait_or_stop(
                stop_event,
                config.SESSION_STATE_POLL_INTERVAL_SECONDS,
            ):
                return None
            continue

        if config.CAMERA_MONITORING_MODE == "IDLE_TRIGGERED":
            if activity_monitor is None:
                _clear_debug_frame(
                    debug_frame_buffer,
                    "键鼠监测不可用 · 摄像头保持关闭",
                    debug_device,
                )
                _report_status(
                    status_callback,
                    "waiting",
                    "键鼠监测不可用 · 摄像头保持关闭",
                )
                return False
            try:
                input_idle_seconds = (
                    activity_monitor.seconds_since_last_input()
                )
            except ActivityMonitorError as exc:
                _observe_sedentary_time(
                    sedentary_tracker,
                    sedentary_reminder_signal,
                    None,
                    now,
                )
                _clear_debug_frame(
                    debug_frame_buffer,
                    "无法确认键鼠状态 · 摄像头保持关闭",
                    debug_device,
                )
                if (
                    now - last_error_log_at
                    >= config.ERROR_LOG_INTERVAL_SECONDS
                ):
                    LOGGER.warning(
                        "Unable to recheck keyboard/mouse activity before "
                        "opening the camera: %s",
                        exc,
                    )
                    _report_status(
                        status_callback,
                        "waiting",
                        "无法确认键鼠状态 · 摄像头保持关闭",
                    )
                    last_error_log_at = now
                if _wait_or_stop(
                    stop_event,
                    config.SESSION_STATE_POLL_INTERVAL_SECONDS,
                ):
                    return None
                continue
            if not camera_should_be_active(
                config.CAMERA_MONITORING_MODE,
                input_idle_seconds,
                config.CAMERA_ACTIVATION_IDLE_SECONDS,
            ):
                _observe_sedentary_time(
                    sedentary_tracker,
                    sedentary_reminder_signal,
                    True,
                    now,
                )
                _clear_debug_frame(
                    debug_frame_buffer,
                    "检测到键鼠操作 · 摄像头保持关闭",
                    debug_device,
                )
                _report_status(
                    status_callback,
                    "standby",
                    "检测到键鼠操作 · 摄像头保持关闭",
                )
                return False

        try:
            camera.open()
            _report_status(
                status_callback,
                "starting",
                f"已打开 {config.PREFERRED_CAMERA_NAME}",
            )
            return True
        except CameraError as exc:
            _observe_sedentary_time(
                sedentary_tracker,
                sedentary_reminder_signal,
                None,
                now,
            )
            _clear_debug_frame(
                debug_frame_buffer,
                "无法打开摄像头 · 调试画面已清空",
                debug_device,
            )
            if (
                now - last_error_log_at
                >= config.ERROR_LOG_INTERVAL_SECONDS
            ):
                LOGGER.warning(
                    "Unable to open the camera; will retry: %s",
                    exc,
                )
                _report_status(
                    status_callback,
                    "camera_error",
                    f"无法打开 {config.PREFERRED_CAMERA_NAME} · 正在重试",
                )
                last_error_log_at = now
            if _wait_or_stop(
                stop_event,
                config.CAMERA_RECONNECT_INTERVAL_SECONDS,
            ):
                return None


def run(
    stop_event: Optional[Event] = None,
    status_callback: Optional[StatusCallback] = None,
    debug_frame_buffer: Optional[DebugFrameBuffer] = None,
    privacy_blur_signal: Optional[PrivacyBlurSignal] = None,
    sedentary_reminder_signal: Optional[SedentaryReminderSignal] = None,
) -> int:
    """Run persistent lock, unlock, and resume cycles."""
    camera: Optional[Camera] = None
    detector: Optional[FaceDetector] = None
    identity_recognizer: Optional[FaceIdentityRecognizer] = None
    face_template: Optional[FaceTemplate] = None
    camera_recheck_not_before: Optional[float] = None

    try:
        _clear_debug_frame(
            debug_frame_buffer,
            "正在初始化 OpenVINO · 暂无画面",
        )
        if stop_event is not None and stop_event.is_set():
            return 0
        _report_status(
            status_callback,
            "starting",
            "正在初始化 OpenVINO",
        )
        detector = FaceDetector(
            model_xml_path=config.MODEL_XML_PATH,
            model_bin_path=config.MODEL_BIN_PATH,
            confidence_threshold=config.FACE_CONFIDENCE_THRESHOLD,
            preferred_device=config.PREFERRED_INFERENCE_DEVICE,
        )
        if config.PRESENCE_MODE == "REGISTERED_FACE":
            face_template = FaceTemplateStore(
                config.FACE_TEMPLATE_PATH
            ).load()
            identity_recognizer = FaceIdentityRecognizer(
                landmarks_xml_path=config.LANDMARKS_MODEL_XML_PATH,
                landmarks_bin_path=config.LANDMARKS_MODEL_BIN_PATH,
                reidentification_xml_path=(
                    config.FACE_REIDENTIFICATION_MODEL_XML_PATH
                ),
                reidentification_bin_path=(
                    config.FACE_REIDENTIFICATION_MODEL_BIN_PATH
                ),
                preferred_device=config.PREFERRED_INFERENCE_DEVICE,
            )
        device_text = detector.device
        if identity_recognizer is not None:
            device_text = (
                f"检测 {detector.device} / 识别 {identity_recognizer.device}"
            )
        _clear_debug_frame(
            debug_frame_buffer,
            "OpenVINO 初始化完成 · 等待摄像头",
            device_text,
        )

        camera = Camera(
            index=config.CAMERA_INDEX,
            width=config.FRAME_WIDTH,
            height=config.FRAME_HEIGHT,
            preferred_name=config.PREFERRED_CAMERA_NAME,
        )
        activity_monitor = ActivityMonitor()
        session_monitor = SessionMonitor()
        sedentary_tracker = (
            SedentaryTracker(
                config.SEDENTARY_REMINDER_INTERVAL_SECONDS,
                config.SEDENTARY_LEAVE_CONFIRMATION_SECONDS,
            )
            if config.SEDENTARY_REMINDER_ENABLED
            else None
        )

        while True:
            saw_locked = wait_for_session_ready(
                session_monitor,
                require_lock_transition=False,
                stop_event=stop_event,
                status_callback=status_callback,
                debug_frame_buffer=debug_frame_buffer,
                debug_device=device_text,
                sedentary_tracker=sedentary_tracker,
                sedentary_reminder_signal=sedentary_reminder_signal,
            )
            if saw_locked is None:
                return 0
            if saw_locked:
                if _wait_or_stop(
                    stop_event,
                    config.POST_UNLOCK_RESUME_DELAY_SECONDS,
                ):
                    return 0

            camera_activation_ready = wait_for_camera_activation(
                activity_monitor,
                session_monitor,
                stop_event=stop_event,
                status_callback=status_callback,
                debug_frame_buffer=debug_frame_buffer,
                debug_device=device_text,
                sedentary_tracker=sedentary_tracker,
                sedentary_reminder_signal=sedentary_reminder_signal,
                presence_recheck_not_before=camera_recheck_not_before,
            )
            if camera_activation_ready is None:
                return 0
            if not camera_activation_ready:
                continue
            camera_recheck_not_before = None

            camera_opened = open_camera_when_session_ready(
                camera,
                session_monitor,
                activity_monitor=activity_monitor,
                stop_event=stop_event,
                status_callback=status_callback,
                debug_frame_buffer=debug_frame_buffer,
                debug_device=device_text,
                sedentary_tracker=sedentary_tracker,
                sedentary_reminder_signal=sedentary_reminder_signal,
            )
            if camera_opened is None:
                return 0
            if not camera_opened:
                continue

            try:
                outcome = monitor_until_session_pause(
                    camera,
                    detector,
                    activity_monitor,
                    session_monitor,
                    identity_recognizer=identity_recognizer,
                    face_template=face_template,
                    stop_event=stop_event,
                    status_callback=status_callback,
                    debug_frame_buffer=debug_frame_buffer,
                    privacy_blur_signal=privacy_blur_signal,
                    sedentary_tracker=sedentary_tracker,
                    sedentary_reminder_signal=sedentary_reminder_signal,
                )
            finally:
                camera.release()
                _clear_debug_frame(
                    debug_frame_buffer,
                    "摄像头已释放 · 调试画面已清空",
                    device_text,
                )
                LOGGER.info("Camera released")

            if outcome is MonitorOutcome.STOP_REQUESTED:
                return 0
            if outcome is MonitorOutcome.PRESENCE_CONFIRMED_STANDBY:
                camera_recheck_not_before = (
                    time.monotonic()
                    + config.CAMERA_PRESENCE_RECHECK_INTERVAL_SECONDS
                )
                continue
            if outcome in {
                MonitorOutcome.INPUT_ACTIVE,
                MonitorOutcome.INPUT_MONITOR_UNAVAILABLE,
            }:
                continue
            if outcome is MonitorOutcome.LOCK_REQUESTED:
                saw_locked = wait_for_session_ready(
                    session_monitor,
                    require_lock_transition=True,
                    stop_event=stop_event,
                    status_callback=status_callback,
                    debug_frame_buffer=debug_frame_buffer,
                    debug_device=device_text,
                    sedentary_tracker=sedentary_tracker,
                    sedentary_reminder_signal=sedentary_reminder_signal,
                )
                if saw_locked is None:
                    return 0
                if saw_locked:
                    if _wait_or_stop(
                        stop_event,
                        config.POST_UNLOCK_RESUME_DELAY_SECONDS,
                    ):
                        return 0
            elif outcome is MonitorOutcome.SESSION_LOCKED:
                saw_locked = wait_for_session_ready(
                    session_monitor,
                    require_lock_transition=False,
                    stop_event=stop_event,
                    status_callback=status_callback,
                    debug_frame_buffer=debug_frame_buffer,
                    debug_device=device_text,
                    sedentary_tracker=sedentary_tracker,
                    sedentary_reminder_signal=sedentary_reminder_signal,
                )
                if saw_locked is None:
                    return 0
                if saw_locked:
                    if _wait_or_stop(
                        stop_event,
                        config.POST_UNLOCK_RESUME_DELAY_SECONDS,
                    ):
                        return 0
    except KeyboardInterrupt:
        LOGGER.info("Ctrl+C received; exiting safely")
        return 0
    except (
        CameraError,
        DetectorError,
        FaceIdentityError,
        FaceTemplateError,
        ActivityMonitorError,
        SessionMonitorError,
    ) as exc:
        LOGGER.error("Startup failed: %s", exc)
        _report_status(
            status_callback,
            "error",
            f"启动失败：{exc}",
        )
        return 1
    except Exception:
        LOGGER.exception("Unexpected fatal error")
        _report_status(
            status_callback,
            "error",
            "程序发生未预期错误，请查看控制台",
        )
        return 1
    finally:
        _clear_privacy_blur(privacy_blur_signal)
        if camera is not None:
            camera.release()
        _clear_debug_frame(
            debug_frame_buffer,
            "监控已停止 · 调试画面已清空",
            "" if detector is None else detector.device,
        )
        if detector is not None:
            detector.close()
        if identity_recognizer is not None:
            identity_recognizer.close()
        if stop_event is not None and stop_event.is_set():
            _report_status(
                status_callback,
                "stopped",
                "监控已停止",
            )


def main() -> None:
    configure_logging()
    sys.exit(run())


if __name__ == "__main__":
    main()
