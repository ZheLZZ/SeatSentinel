"""Local camera enrollment workflow for one registered SeatSentinel user."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Event
from typing import Callable, Optional

import numpy as np
from numpy.typing import NDArray

import config
from camera import Camera
from detector import FaceDetector
from face_identity import (
    FaceIdentityInferenceError,
    FaceIdentityRecognizer,
    FaceTemplate,
    FaceTemplateStore,
)
from user_settings import AppSettings


class FaceRegistrationError(RuntimeError):
    """Raised when local face registration cannot be completed."""


class FaceRegistrationCancelled(FaceRegistrationError):
    """Raised when the user cancels registration."""


@dataclass(frozen=True)
class FaceRegistrationUpdate:
    """One progress update; the optional frame exists only in memory."""

    message: str
    accepted_samples: int
    required_samples: int
    frame_bgr: Optional[NDArray[np.uint8]] = None


RegistrationCallback = Callable[[FaceRegistrationUpdate], None]


def register_face_from_camera(
    settings: AppSettings,
    template_store: FaceTemplateStore,
    cancel_event: Event,
    callback: Optional[RegistrationCallback] = None,
) -> FaceTemplate:
    """Capture multiple descriptors and save only their protected centroid."""
    camera: Optional[Camera] = None
    detector: Optional[FaceDetector] = None
    recognizer: Optional[FaceIdentityRecognizer] = None
    samples: list[NDArray[np.float32]] = []
    required = config.FACE_REGISTRATION_SAMPLE_COUNT
    deadline = time.monotonic() + config.FACE_REGISTRATION_TIMEOUT_SECONDS
    next_inference_at = time.monotonic()
    next_sample_at = time.monotonic()
    last_message = ""

    def publish(
        message: str,
        frame: Optional[NDArray[np.uint8]] = None,
        force: bool = False,
    ) -> None:
        nonlocal last_message
        if callback is None:
            return
        if not force and message == last_message and frame is None:
            return
        last_message = message
        callback(
            FaceRegistrationUpdate(
                message=message,
                accepted_samples=len(samples),
                required_samples=required,
                frame_bgr=(
                    None
                    if frame is None
                    else np.ascontiguousarray(frame.copy())
                ),
            )
        )

    try:
        publish("正在初始化本地人脸模型……", force=True)
        detector = FaceDetector(
            config.MODEL_XML_PATH,
            config.MODEL_BIN_PATH,
            settings.face_confidence_threshold,
            preferred_device=settings.inference_device,
        )
        recognizer = FaceIdentityRecognizer(
            config.LANDMARKS_MODEL_XML_PATH,
            config.LANDMARKS_MODEL_BIN_PATH,
            config.FACE_REIDENTIFICATION_MODEL_XML_PATH,
            config.FACE_REIDENTIFICATION_MODEL_BIN_PATH,
            preferred_device=settings.inference_device,
        )
        if cancel_event.is_set():
            raise FaceRegistrationCancelled("已取消人脸注册")

        publish("正在打开摄像头……", force=True)
        camera = Camera(
            index=config.CAMERA_INDEX,
            width=settings.frame_width,
            height=settings.frame_height,
            preferred_name=settings.camera_name,
        )
        camera.open()
        publish(
            "请正对摄像头，并缓慢左右转头；画面不会保存。",
            force=True,
        )

        while len(samples) < required:
            if cancel_event.is_set():
                raise FaceRegistrationCancelled("已取消人脸注册")
            now = time.monotonic()
            if now >= deadline:
                raise FaceRegistrationError(
                    "注册超时。请保证光线充足、仅一人入镜并靠近摄像头。"
                )
            camera_ok, frame = camera.read()
            if not camera_ok or frame is None:
                publish("摄像头画面读取失败，正在重试……")
                if cancel_event.wait(0.1):
                    raise FaceRegistrationCancelled("已取消人脸注册")
                continue
            if now < next_inference_at:
                continue
            next_inference_at = now + 0.20

            detections = detector.detect_faces(frame)
            if len(detections) == 0:
                publish("未检测到人脸，请正对并靠近摄像头。", frame)
                continue
            if len(detections) != 1:
                publish("注册时画面中只能有一张人脸。", frame)
                continue
            detection = detections[0]
            face_width = detection.xmax - detection.xmin
            face_height = detection.ymax - detection.ymin
            frame_height, frame_width = frame.shape[:2]
            if (
                face_width < 70
                or face_height < 70
                or face_width * face_height
                < frame_width * frame_height * 0.025
            ):
                publish("人脸距离较远，请靠近摄像头。", frame)
                continue
            try:
                embedding = recognizer.extract_embedding(frame, detection)
            except FaceIdentityInferenceError:
                publish("暂时无法提取稳定特征，请保持面部清晰。", frame)
                continue
            if samples:
                current_centroid = FaceTemplateStore.normalize_embedding(
                    np.mean(samples, axis=0)
                )
                similarity = float(np.dot(embedding, current_centroid))
                if similarity < 0.55:
                    publish("人脸变化过大，请确保始终是同一人注册。", frame)
                    continue
            if now < next_sample_at:
                publish("保持自然表情，并缓慢调整头部角度。", frame)
                continue
            samples.append(embedding)
            next_sample_at = now + 0.35
            publish(
                f"已采集 {len(samples)} / {required} 个本地特征样本",
                frame,
                force=True,
            )

        publish("正在加密保存人脸特征模板……", force=True)
        template = template_store.save_embeddings(samples)
        publish(
            f"注册完成：已使用 {template.sample_count} 个样本。",
            force=True,
        )
        return template
    except FaceRegistrationError:
        raise
    except Exception as exc:
        raise FaceRegistrationError(str(exc)) from exc
    finally:
        if camera is not None:
            camera.release()
        if detector is not None:
            detector.close()
        if recognizer is not None:
            recognizer.close()
