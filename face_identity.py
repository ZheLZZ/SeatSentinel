"""Local registered-face matching and Windows-protected template storage."""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import math
import os
from dataclasses import dataclass, replace
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import cv2
import numpy as np
from numpy.typing import NDArray

from detector import FaceDetection

os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("SCARF_NO_ANALYTICS", "1")

from openvino import Core


LOGGER = logging.getLogger(__name__)
TEMPLATE_HEADER = b"SEATSENTINEL-FACE-TEMPLATE-V1\n"
TEMPLATE_VERSION = 1
REIDENTIFICATION_MODEL_NAME = "face-reidentification-retail-0095"
REFERENCE_LANDMARKS = np.asarray(
    [
        (0.31556875, 0.4615741071428571),
        (0.6826229166666667, 0.4615741071428571),
        (0.5002625, 0.6405053571428571),
        (0.349471875, 0.8246919642857142),
        (0.6534364583333333, 0.8246919642857142),
    ],
    dtype=np.float32,
)


class FaceIdentityError(RuntimeError):
    """Raised when registered-face support cannot be initialized."""


class FaceIdentityInferenceError(RuntimeError):
    """Raised when a face descriptor cannot be produced reliably."""


class FaceTemplateError(RuntimeError):
    """Raised when a protected face template is missing or invalid."""


@dataclass(frozen=True)
class FaceTemplate:
    """A single normalized registered-person centroid."""

    embedding: NDArray[np.float32]
    sample_count: int
    created_at: str


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _windows_protect(data: bytes) -> bytes:
    """Protect bytes for the current Windows user with DPAPI."""
    if os.name != "nt":
        raise FaceTemplateError("人脸模板加密仅支持 Windows")
    if not data:
        raise FaceTemplateError("不能加密空的人脸模板")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_buffer = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    input_blob = _DataBlob(
        len(data),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    output_blob = _DataBlob()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    success = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "SeatSentinel registered face",
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    )
    if not success:
        raise FaceTemplateError(
            f"无法加密人脸模板，Windows 错误码 {ctypes.get_last_error()}"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _windows_unprotect(data: bytes) -> bytes:
    """Unprotect bytes previously encrypted for the Windows user."""
    if os.name != "nt":
        raise FaceTemplateError("人脸模板解密仅支持 Windows")
    if not data:
        raise FaceTemplateError("人脸模板内容为空")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_buffer = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    input_blob = _DataBlob(
        len(data),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    output_blob = _DataBlob()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    success = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    )
    if not success:
        raise FaceTemplateError(
            "无法解密人脸模板；文件可能已损坏，或来自其他 Windows 用户"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


class FaceTemplateStore:
    """Atomically persist one minimal, DPAPI-protected face descriptor."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def is_registered(self) -> bool:
        return self.path.is_file()

    def save_embeddings(
        self,
        embeddings: Iterable[NDArray[np.floating[Any]]],
    ) -> FaceTemplate:
        normalized = [self.normalize_embedding(value) for value in embeddings]
        if len(normalized) < 5:
            raise FaceTemplateError("至少需要 5 个有效样本才能注册本人")
        centroid = self.normalize_embedding(np.mean(normalized, axis=0))
        created_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "version": TEMPLATE_VERSION,
            "model": REIDENTIFICATION_MODEL_NAME,
            "dimensions": int(centroid.size),
            "sample_count": len(normalized),
            "created_at": created_at,
            "embedding_f32_base64": base64.b64encode(
                centroid.astype("<f4", copy=False).tobytes()
            ).decode("ascii"),
        }
        plaintext = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        protected = _windows_protect(plaintext)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_bytes(TEMPLATE_HEADER + protected)
            os.replace(temporary_path, self.path)
        except OSError as exc:
            raise FaceTemplateError(f"无法保存人脸模板：{exc}") from exc
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return FaceTemplate(centroid, len(normalized), created_at)

    def load(self) -> FaceTemplate:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise FaceTemplateError("尚未注册本人，请先完成人脸注册") from exc
        except OSError as exc:
            raise FaceTemplateError(f"无法读取人脸模板：{exc}") from exc
        if not raw.startswith(TEMPLATE_HEADER):
            raise FaceTemplateError("人脸模板格式不受支持或文件已损坏")
        plaintext = _windows_unprotect(raw[len(TEMPLATE_HEADER) :])
        try:
            payload = json.loads(plaintext.decode("utf-8"))
            if payload["version"] != TEMPLATE_VERSION:
                raise ValueError("unsupported version")
            if payload["model"] != REIDENTIFICATION_MODEL_NAME:
                raise ValueError("model mismatch")
            dimensions = int(payload["dimensions"])
            sample_count = int(payload["sample_count"])
            created_at = str(payload["created_at"])
            encoded = str(payload["embedding_f32_base64"])
            embedding = np.frombuffer(
                base64.b64decode(encoded, validate=True),
                dtype="<f4",
            ).copy()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FaceTemplateError("人脸模板内容无效或已损坏") from exc
        if dimensions != 256 or embedding.size != dimensions:
            raise FaceTemplateError("人脸模板特征维度不正确")
        if sample_count < 5:
            raise FaceTemplateError("人脸模板样本数量不正确")
        return FaceTemplate(
            self.normalize_embedding(embedding),
            sample_count,
            created_at,
        )

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise FaceTemplateError(f"无法删除人脸模板：{exc}") from exc

    @staticmethod
    def normalize_embedding(
        embedding: NDArray[np.floating[Any]],
    ) -> NDArray[np.float32]:
        value = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if value.size != 256 or not np.all(np.isfinite(value)):
            raise FaceTemplateError("人脸特征必须是有效的 256 维向量")
        norm = float(np.linalg.norm(value))
        if not math.isfinite(norm) or norm <= 1e-8:
            raise FaceTemplateError("人脸特征不能是空向量")
        return np.ascontiguousarray(value / norm, dtype=np.float32)


class FaceIdentityRecognizer:
    """Align detected faces and compare their 256-D local descriptors."""

    def __init__(
        self,
        landmarks_xml_path: Path,
        landmarks_bin_path: Path,
        reidentification_xml_path: Path,
        reidentification_bin_path: Path,
        preferred_device: str = "NPU",
    ) -> None:
        self._preferred_device = preferred_device.strip().upper()
        if self._preferred_device not in {"NPU", "CPU"}:
            raise FaceIdentityError("推理设备只能选择 NPU 或 CPU")
        self._core: Optional[Core] = None
        self._landmarks_compiled: Any = None
        self._reidentification_compiled: Any = None
        self._landmarks_request: Any = None
        self._reidentification_request: Any = None
        self._landmarks_size = (0, 0)
        self._reidentification_size = (0, 0)
        self.device = ""
        self._initialize(
            landmarks_xml_path,
            landmarks_bin_path,
            reidentification_xml_path,
            reidentification_bin_path,
        )

    def _initialize(
        self,
        landmarks_xml_path: Path,
        landmarks_bin_path: Path,
        reidentification_xml_path: Path,
        reidentification_bin_path: Path,
    ) -> None:
        for path in (
            landmarks_xml_path,
            landmarks_bin_path,
            reidentification_xml_path,
            reidentification_bin_path,
        ):
            if not path.is_file():
                raise FaceIdentityError(f"人脸识别模型文件不存在：{path}")
        try:
            self._core = Core()
            landmarks_model = self._core.read_model(
                model=str(landmarks_xml_path),
                weights=str(landmarks_bin_path),
            )
            reidentification_model = self._core.read_model(
                model=str(reidentification_xml_path),
                weights=str(reidentification_bin_path),
            )
            self._landmarks_size = self._validate_model(
                landmarks_model,
                expected_output_size=10,
                name="人脸关键点模型",
            )
            self._reidentification_size = self._validate_model(
                reidentification_model,
                expected_output_size=256,
                name="人脸特征模型",
            )
            self._landmarks_compiled, landmarks_device = self._compile_model(
                landmarks_model,
                "人脸关键点模型",
            )
            self._reidentification_compiled, reid_device = self._compile_model(
                reidentification_model,
                "人脸特征模型",
            )
            self._landmarks_request = (
                self._landmarks_compiled.create_infer_request()
            )
            self._reidentification_request = (
                self._reidentification_compiled.create_infer_request()
            )
            devices = {landmarks_device, reid_device}
            self.device = (
                landmarks_device
                if len(devices) == 1
                else "+".join(sorted(devices, reverse=True))
            )
        except FaceIdentityError:
            raise
        except Exception as exc:
            raise FaceIdentityError(
                f"无法初始化本人识别模型：{exc}"
            ) from exc

    @staticmethod
    def _validate_model(
        model: Any,
        expected_output_size: int,
        name: str,
    ) -> tuple[int, int]:
        if len(model.inputs) != 1 or len(model.outputs) != 1:
            raise FaceIdentityError(f"{name}必须各有一个输入和输出")
        input_shape = tuple(int(value) for value in model.input(0).shape)
        if (
            len(input_shape) != 4
            or input_shape[0] != 1
            or input_shape[1] != 3
            or input_shape[2] <= 0
            or input_shape[3] <= 0
        ):
            raise FaceIdentityError(
                f"{name}输入应为 [1, 3, H, W]，实际为 {input_shape}"
            )
        output_size = int(np.prod(tuple(model.output(0).shape)))
        if output_size != expected_output_size:
            raise FaceIdentityError(
                f"{name}输出应含 {expected_output_size} 个值，实际为 {output_size}"
            )
        return input_shape[3], input_shape[2]

    def _compile_model(self, model: Any, name: str) -> tuple[Any, str]:
        if self._core is None:
            raise FaceIdentityError("OpenVINO 尚未初始化")
        if self._preferred_device == "CPU":
            try:
                return self._core.compile_model(model, "CPU"), "CPU"
            except Exception as exc:
                raise FaceIdentityError(f"{name}无法在 CPU 上编译：{exc}") from exc

        try:
            npu_device = next(
                (
                    device
                    for device in self._core.available_devices
                    if device.upper().split(".", maxsplit=1)[0] == "NPU"
                ),
                None,
            )
        except Exception as exc:
            raise FaceIdentityError(f"无法查询 OpenVINO 设备：{exc}") from exc
        if npu_device is not None:
            try:
                return self._core.compile_model(model, npu_device), "NPU"
            except Exception as exc:
                LOGGER.warning("%s 无法使用 NPU，回退 CPU：%s", name, exc)
        try:
            return self._core.compile_model(model, "CPU"), "CPU"
        except Exception as exc:
            raise FaceIdentityError(f"{name}无法在 CPU 上编译：{exc}") from exc

    @staticmethod
    def _prepare_input(
        image: NDArray[np.uint8],
        size: tuple[int, int],
    ) -> NDArray[np.float32]:
        if image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
            raise FaceIdentityInferenceError("人脸图像必须是有效的三通道 BGR 画面")
        try:
            resized = cv2.resize(image, size, interpolation=cv2.INTER_LINEAR)
        except cv2.error as exc:
            raise FaceIdentityInferenceError(f"无法缩放人脸图像：{exc}") from exc
        return np.ascontiguousarray(
            np.expand_dims(np.transpose(resized, (2, 0, 1)), axis=0),
            dtype=np.float32,
        )

    @staticmethod
    def _expanded_box(
        detection: FaceDetection,
        frame_width: int,
        frame_height: int,
        expansion: float = 0.10,
    ) -> tuple[int, int, int, int]:
        width = detection.xmax - detection.xmin
        height = detection.ymax - detection.ymin
        pad_x = int(round(width * expansion))
        pad_y = int(round(height * expansion))
        return (
            max(0, detection.xmin - pad_x),
            max(0, detection.ymin - pad_y),
            min(frame_width, detection.xmax + pad_x + 1),
            min(frame_height, detection.ymax + pad_y + 1),
        )

    def extract_embedding(
        self,
        frame: NDArray[np.uint8],
        detection: FaceDetection,
    ) -> NDArray[np.float32]:
        if self._landmarks_request is None or self._reidentification_request is None:
            raise FaceIdentityInferenceError("本人识别模型尚未初始化")
        frame_height, frame_width = frame.shape[:2]
        xmin, ymin, xmax, ymax = self._expanded_box(
            detection,
            frame_width,
            frame_height,
        )
        crop = frame[ymin:ymax, xmin:xmax]
        if crop.shape[0] < 24 or crop.shape[1] < 24:
            raise FaceIdentityInferenceError("检测到的人脸过小，无法可靠识别")
        try:
            landmark_output = np.asarray(
                self._landmarks_request.infer(
                    inputs={0: self._prepare_input(crop, self._landmarks_size)}
                )[0]
            ).reshape(-1)
        except FaceIdentityInferenceError:
            raise
        except Exception as exc:
            raise FaceIdentityInferenceError(
                f"人脸关键点推理失败：{exc}"
            ) from exc
        if landmark_output.size != 10 or not np.all(np.isfinite(landmark_output)):
            raise FaceIdentityInferenceError("人脸关键点模型输出无效")
        landmarks = landmark_output.reshape(5, 2).astype(np.float32)
        if np.any(landmarks < -0.15) or np.any(landmarks > 1.15):
            raise FaceIdentityInferenceError("人脸关键点超出合理范围")
        source_points = landmarks * np.asarray(
            [crop.shape[1], crop.shape[0]],
            dtype=np.float32,
        ) + np.asarray([xmin, ymin], dtype=np.float32)
        target_points = REFERENCE_LANDMARKS * np.asarray(
            self._reidentification_size,
            dtype=np.float32,
        )
        try:
            transform, _ = cv2.estimateAffinePartial2D(
                source_points,
                target_points,
                method=cv2.LMEDS,
            )
            if transform is None or not np.all(np.isfinite(transform)):
                raise FaceIdentityInferenceError("无法对齐人脸关键点")
            aligned = cv2.warpAffine(
                frame,
                transform,
                self._reidentification_size,
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            descriptor_output = np.asarray(
                self._reidentification_request.infer(
                    inputs={
                        0: self._prepare_input(
                            aligned,
                            self._reidentification_size,
                        )
                    }
                )[0]
            ).reshape(-1)
        except FaceIdentityInferenceError:
            raise
        except Exception as exc:
            raise FaceIdentityInferenceError(
                f"人脸特征推理失败：{exc}"
            ) from exc
        try:
            return FaceTemplateStore.normalize_embedding(descriptor_output)
        except FaceTemplateError as exc:
            raise FaceIdentityInferenceError(str(exc)) from exc

    def recognize_faces(
        self,
        frame: NDArray[np.uint8],
        detections: Sequence[FaceDetection],
        template: FaceTemplate,
        similarity_threshold: float,
    ) -> list[FaceDetection]:
        if not 0.0 <= similarity_threshold <= 1.0:
            raise FaceIdentityInferenceError("本人相似度阈值必须在 0 到 1 之间")
        recognized: list[FaceDetection] = []
        for detection in detections:
            embedding = self.extract_embedding(frame, detection)
            similarity = float(np.dot(embedding, template.embedding))
            if not math.isfinite(similarity):
                raise FaceIdentityInferenceError("本人相似度计算结果无效")
            similarity = max(-1.0, min(1.0, similarity))
            recognized.append(
                replace(
                    detection,
                    identity_similarity=similarity,
                    is_registered_person=(similarity >= similarity_threshold),
                )
            )
        return recognized

    def close(self) -> None:
        self._landmarks_request = None
        self._reidentification_request = None
        self._landmarks_compiled = None
        self._reidentification_compiled = None
        self._core = None
