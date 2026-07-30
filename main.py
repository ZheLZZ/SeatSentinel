"""Console entry point for SeatSentinel."""

from __future__ import annotations

import logging
import logging.handlers
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
from session_monitor import SessionMonitor, SessionMonitorError
from windows_lock import WindowsLockError, lock_workstation


LOGGER = logging.getLogger("seat_sentinel")


class MonitorOutcome(Enum):
    """Reasons for pausing active camera monitoring."""

    LOCK_REQUESTED = "lock_requested"
    SESSION_LOCKED = "session_locked"
    SESSION_STATE_UNKNOWN = "session_state_unknown"
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
) -> None:
    if debug_frame_buffer is None:
        return
    try:
        debug_frame_buffer.clear(status=status, device=device)
    except Exception:
        LOGGER.exception("Unable to clear the in-memory debug frame")


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


def monitor_until_session_pause(
    camera: Camera,
    detector: FaceDetector,
    activity_monitor: ActivityMonitor,
    session_monitor: SessionMonitor,
    stop_event: Optional[Event] = None,
    status_callback: Optional[StatusCallback] = None,
    debug_frame_buffer: Optional[DebugFrameBuffer] = None,
) -> MonitorOutcome:
    """Monitor until Windows locks or the session state becomes uncertain."""
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

    LOGGER.info(
        "Monitoring active; grace period is %.0f seconds",
        config.STARTUP_GRACE_PERIOD_SECONDS,
    )
    _report_status(
        status_callback,
        "monitoring",
        f"正在监控 · {detector.device}",
    )

    while True:
        if stop_event is not None and stop_event.is_set():
            _clear_debug_frame(
                debug_frame_buffer,
                "监控已停止 · 调试画面已清空",
                detector.device,
            )
            return MonitorOutcome.STOP_REQUESTED

        now = time.monotonic()
        wait_seconds = next_detection_at - now
        if wait_seconds > 0:
            if _wait_or_stop(stop_event, wait_seconds):
                return MonitorOutcome.STOP_REQUESTED

        cycle_time = time.monotonic()
        next_detection_at = (
            cycle_time + config.DETECTION_INTERVAL_SECONDS
        )

        try:
            if session_monitor.is_locked():
                _clear_debug_frame(
                    debug_frame_buffer,
                    "Windows 已锁定 · 调试画面已清空",
                    detector.device,
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
            _clear_debug_frame(
                debug_frame_buffer,
                "Windows 会话状态不明 · 调试画面已清空",
                detector.device,
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

        camera_ok, frame = camera.read()
        inference_ok = False
        face_detected: Optional[bool] = None
        detections: list[FaceDetection] = []
        inference_ms: Optional[float] = None

        if not camera_ok or frame is None:
            _clear_debug_frame(
                debug_frame_buffer,
                "摄像头读取失败 · 调试画面已清空",
                detector.device,
            )
            # Unknown visual state is treated as presence. This starts a fresh
            # uninterrupted absence window after the camera recovers.
            last_seen_time = cycle_time
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
                        f"摄像头已恢复 · {detector.device}",
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
                inference_ms = (
                    time.perf_counter() - inference_started_at
                ) * 1000.0
                face_detected = bool(detections)
                inference_ok = True
                if not inference_was_healthy:
                    LOGGER.info("Model inference recovered")
                inference_was_healthy = True
            except DetectorInferenceError as exc:
                _clear_debug_frame(
                    debug_frame_buffer,
                    "模型推理失败 · 调试画面已清空",
                    detector.device,
                )
                # As with camera failure, do not count uncertain time as
                # confirmed absence.
                last_seen_time = cycle_time
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

        if face_detected is True:
            last_seen_time = cycle_time

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
        should_lock = (
            camera_ok
            and inference_ok
            and face_detected is False
            and face_absent_seconds
            >= config.FACE_ABSENCE_TIMEOUT_SECONDS
            and input_idle_seconds is not None
            and input_idle_seconds >= config.INPUT_IDLE_TIMEOUT_SECONDS
            and startup_elapsed_seconds
            >= config.STARTUP_GRACE_PERIOD_SECONDS
            and cycle_time >= lock_retry_not_before
        )

        if (
            camera_ok
            and frame is not None
            and inference_ok
            and face_detected is not None
        ):
            _publish_debug_frame(
                debug_frame_buffer,
                frame,
                detections,
                detector.device,
                (
                    f"检测正常 · 发现 {len(detections)} 张人脸"
                    if face_detected
                    else "检测正常 · 未发现人脸"
                ),
                face_absent_seconds,
                input_idle_seconds,
                startup_elapsed_seconds,
                should_lock,
                inference_ms,
            )

        if (
            cycle_time - last_status_log_at
            >= config.STATUS_LOG_INTERVAL_SECONDS
            or should_lock
        ):
            LOGGER.info(
                "状态 | 检测到人脸=%s | 距上次人脸=%s秒 | "
                "距上次键鼠活动=%s秒 | 推理设备=%s | 达到锁屏条件=%s",
                _face_status_text(face_detected),
                _seconds_text(face_absent_seconds),
                _seconds_text(input_idle_seconds),
                detector.device,
                "是" if should_lock else "否",
            )
            _report_status(
                status_callback,
                "monitoring",
                "人脸=%s · 无人=%s秒 · 键鼠空闲=%s秒 · %s · "
                "锁屏条件=%s"
                % (
                    _face_status_text(face_detected),
                    _seconds_text(face_absent_seconds),
                    _seconds_text(input_idle_seconds),
                    detector.device,
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
            continue

        if (
            final_input_idle_seconds
            < config.INPUT_IDLE_TIMEOUT_SECONDS
        ):
            LOGGER.info(
                "Recent keyboard/mouse input detected during final check; "
                "lock cancelled"
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
            detector.device,
        )
        try:
            lock_workstation()
            LOGGER.info("LockWorkStation completed successfully")
            _report_status(
                status_callback,
                "locked",
                "Windows 已锁定 · 等待解锁",
            )
            return MonitorOutcome.LOCK_REQUESTED
        except WindowsLockError as exc:
            LOGGER.error("%s", exc)
            last_seen_time = cycle_time
            lock_retry_not_before = (
                cycle_time + config.LOCK_RETRY_COOLDOWN_SECONDS
            )


def wait_for_session_ready(
    session_monitor: SessionMonitor,
    require_lock_transition: bool,
    stop_event: Optional[Event] = None,
    status_callback: Optional[StatusCallback] = None,
    debug_frame_buffer: Optional[DebugFrameBuffer] = None,
    debug_device: str = "",
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


def open_camera_when_session_ready(
    camera: Camera,
    session_monitor: SessionMonitor,
    stop_event: Optional[Event] = None,
    status_callback: Optional[StatusCallback] = None,
    debug_frame_buffer: Optional[DebugFrameBuffer] = None,
    debug_device: str = "",
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
                _clear_debug_frame(
                    debug_frame_buffer,
                    "Windows 已锁定 · 调试画面已清空",
                    debug_device,
                )
                return False
        except SessionMonitorError as exc:
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

        try:
            camera.open()
            _report_status(
                status_callback,
                "starting",
                f"已打开 {config.PREFERRED_CAMERA_NAME}",
            )
            return True
        except CameraError as exc:
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
) -> int:
    """Run persistent lock, unlock, and resume cycles."""
    camera: Optional[Camera] = None
    detector: Optional[FaceDetector] = None

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
        _clear_debug_frame(
            debug_frame_buffer,
            "OpenVINO 初始化完成 · 等待摄像头",
            detector.device,
        )

        camera = Camera(
            index=config.CAMERA_INDEX,
            width=config.FRAME_WIDTH,
            height=config.FRAME_HEIGHT,
            preferred_name=config.PREFERRED_CAMERA_NAME,
        )
        activity_monitor = ActivityMonitor()
        session_monitor = SessionMonitor()

        while True:
            saw_locked = wait_for_session_ready(
                session_monitor,
                require_lock_transition=False,
                stop_event=stop_event,
                status_callback=status_callback,
                debug_frame_buffer=debug_frame_buffer,
                debug_device=detector.device,
            )
            if saw_locked is None:
                return 0
            if saw_locked:
                if _wait_or_stop(
                    stop_event,
                    config.POST_UNLOCK_RESUME_DELAY_SECONDS,
                ):
                    return 0

            camera_opened = open_camera_when_session_ready(
                camera,
                session_monitor,
                stop_event=stop_event,
                status_callback=status_callback,
                debug_frame_buffer=debug_frame_buffer,
                debug_device=detector.device,
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
                    stop_event=stop_event,
                    status_callback=status_callback,
                    debug_frame_buffer=debug_frame_buffer,
                )
            finally:
                camera.release()
                _clear_debug_frame(
                    debug_frame_buffer,
                    "摄像头已释放 · 调试画面已清空",
                    detector.device,
                )
                LOGGER.info("Camera released")

            if outcome is MonitorOutcome.STOP_REQUESTED:
                return 0
            if outcome is MonitorOutcome.LOCK_REQUESTED:
                saw_locked = wait_for_session_ready(
                    session_monitor,
                    require_lock_transition=True,
                    stop_event=stop_event,
                    status_callback=status_callback,
                    debug_frame_buffer=debug_frame_buffer,
                    debug_device=detector.device,
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
                    debug_device=detector.device,
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
        if camera is not None:
            camera.release()
        _clear_debug_frame(
            debug_frame_buffer,
            "监控已停止 · 调试画面已清空",
            "" if detector is None else detector.device,
        )
        if detector is not None:
            detector.close()
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
