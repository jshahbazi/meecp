"""Local YOLO11 inference. Images are processed in memory and never saved."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import threading
import time
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 12 * 1024 * 1024
# A prepared adapter declares the host request allowance for base64 plus framing.
MAX_INLINE_BYTES = MAX_BYTES
MAX_INLINE_CHARS = 4 * ((MAX_INLINE_BYTES + 2) // 3)
MAX_PIXELS = 24_000_000
IMAGE_SIZE = 640
HOTDOG_CLASS = 52
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class DetectorError(ValueError):
    """A content-free error safe to display to a caller."""


def threshold_value(value: object) -> float:
    if isinstance(value, bool):
        raise DetectorError("invalid_threshold")
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        raise DetectorError("invalid_threshold") from None
    if not math.isfinite(threshold) or not 0.05 <= threshold <= 0.95:
        raise DetectorError("invalid_threshold")
    return threshold


def decode_image(data: bytes) -> Image.Image:
    if not data or len(data) > MAX_BYTES:
        raise DetectorError("image_size_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP", "BMP", "GIF"}:
                    raise DetectorError("unsupported_image")
                if image.width * image.height > MAX_PIXELS:
                    raise DetectorError("image_pixel_limit")
                return ImageOps.exif_transpose(image).convert("RGB")
    except DetectorError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise DetectorError("image_pixel_limit") from None
    except (UnidentifiedImageError, OSError, ValueError):
        raise DetectorError("invalid_image") from None


def preprocess(image: Image.Image) -> tuple[np.ndarray, float, int, int]:
    width, height = image.size
    scale = min(IMAGE_SIZE / width, IMAGE_SIZE / height)
    resized = image.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                           Image.Resampling.BILINEAR)
    left = round((IMAGE_SIZE - resized.width) / 2 - 0.1)
    top = round((IMAGE_SIZE - resized.height) / 2 - 0.1)
    canvas = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), (114, 114, 114))
    canvas.paste(resized, (left, top))
    tensor = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    return np.ascontiguousarray(tensor), scale, left, top


def postprocess(output: np.ndarray, threshold: float, scale: float, left: int, top: int,
                width: int, height: int) -> list[dict]:
    if output.shape != (1, 84, 8400) or not np.isfinite(output).all():
        raise DetectorError("unsupported_model_output")
    rows = output[0].T
    classes = rows[:, 4:].argmax(axis=1)
    scores = rows[:, 4:].max(axis=1)
    mask = (classes == HOTDOG_CLASS) & (scores >= threshold)
    rows, scores = rows[mask], scores[mask]
    if len(rows) == 0:
        return []
    boxes = np.column_stack((rows[:, 0] - rows[:, 2] / 2, rows[:, 1] - rows[:, 3] / 2,
                             rows[:, 0] + rows[:, 2] / 2, rows[:, 1] + rows[:, 3] / 2))
    order = np.argsort(-scores, kind="stable")
    keep = []
    while len(order) and len(keep) < 100:
        current = int(order[0])
        keep.append(current)
        remaining = order[1:]
        if not len(remaining):
            break
        intersection = np.maximum(0, np.minimum(boxes[current, 2:], boxes[remaining, 2:]) -
                                   np.maximum(boxes[current, :2], boxes[remaining, :2])).prod(axis=1)
        areas = np.maximum(0, boxes[:, 2:] - boxes[:, :2]).prod(axis=1)
        iou = intersection / np.maximum(areas[current] + areas[remaining] - intersection, 1e-9)
        order = remaining[iou <= 0.7]
    detections = []
    for index in keep:
        box = (boxes[index] - [left, top, left, top]) / scale
        box[[0, 2]] = np.clip(box[[0, 2]], 0, width)
        box[[1, 3]] = np.clip(box[[1, 3]], 0, height)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        detections.append({"confidence": round(float(scores[index]), 4),
                           "box": [round(float(coordinate), 1) for coordinate in box]})
    return detections


class HotdogDetector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = None

    def load(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime as ort

            info = json.loads((ROOT / "model-provenance.json").read_text())
            path = ROOT / "models" / "yolo11n.onnx"
            if hashlib.sha256(path.read_bytes()).hexdigest() != info["onnx"]["sha256"]:
                raise DetectorError("model_integrity_failed")
            options = ort.SessionOptions()
            options.log_severity_level = 4
            options.intra_op_num_threads = 4
            options.inter_op_num_threads = 1
            session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
            if session.get_inputs()[0].shape != [1, 3, IMAGE_SIZE, IMAGE_SIZE]:
                raise DetectorError("unsupported_model_input")
            self._session = session
        except DetectorError:
            raise
        except Exception:
            raise DetectorError("model_unavailable") from None

    def detect(self, data: bytes, confidence: float = 0.35) -> dict:
        threshold = threshold_value(confidence)
        image = decode_image(data)
        with self._lock:
            self.load()
            started = time.perf_counter()
            tensor, scale, left, top = preprocess(image)
            try:
                output = self._session.run(None, {self._session.get_inputs()[0].name: tensor})[0]
                detections = postprocess(output, threshold, scale, left, top, *image.size)
            except DetectorError:
                raise
            except Exception:
                raise DetectorError("detection_failed") from None
            elapsed = (time.perf_counter() - started) * 1000
        return {"verdict": "hotdog" if detections else "not_hotdog", "hotdog": bool(detections),
                "confidence": detections[0]["confidence"] if detections else None,
                "threshold": threshold, "detections": detections,
                "image": {"width": image.width, "height": image.height},
                "model": "YOLO11n / COCO / ONNX CPU", "inference_ms": round(elapsed, 1)}


_detector = HotdogDetector()


def inline_image(value: str) -> bytes:
    """Decode bounded, canonical base64 or an image data URL without file writes."""
    if not isinstance(value, str) or not value:
        raise DetectorError("invalid_image_data")
    if len(value) > MAX_INLINE_CHARS + 64:
        raise DetectorError("image_size_limit")
    encoded = value
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator or header not in {
            f"data:image/{kind};base64" for kind in ("jpeg", "png", "webp", "bmp", "gif")
        }:
            raise DetectorError("invalid_image_data")
    if len(encoded) > MAX_INLINE_CHARS:
        raise DetectorError("image_size_limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise DetectorError("invalid_image_data") from None
    if not data or base64.b64encode(data).decode("ascii") != encoded:
        raise DetectorError("invalid_image_data")
    if len(data) > MAX_INLINE_BYTES:
        raise DetectorError("image_size_limit")
    return data


def detect_hotdog(image_path: str | None = None, confidence: float = 0.35, *,
                  image_base64: str | None = None) -> dict:
    """Detect from exactly one client-supplied local path or base64 image/data URL.

    No network access, file writes, image history, or calibrated negative probability.
    Both inline images and file inputs are limited to 12 MiB.
    A chat client must expose attachment bytes or a path to pass them to this tool.
    """
    if (image_path is None) == (image_base64 is None):
        raise DetectorError("invalid_image_input")
    if image_base64 is not None:
        return _detector.detect(inline_image(image_base64), confidence)
    try:
        path = Path(image_path)
        if not path.is_absolute() or not path.is_file():
            raise DetectorError("invalid_image_path")
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except DetectorError:
        raise
    except (OSError, TypeError, ValueError):
        raise DetectorError("invalid_image_path") from None
    return _detector.detect(data, confidence)
