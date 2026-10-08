"""Pinned YOLO11n CPU task. Images remain in memory; failures contain no input.

Adapted from meecp's AGPL-3.0 hotdog example; full text is LICENSE-MODEL.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import threading
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, PngImagePlugin, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent
MODEL_SHA256 = "121b1f682e2df1aff1c16dacb7ce4bedc26df016a500b18743349c16724ad85d"
MODEL_BYTES = 10701557
MAX_BYTES = 1024 * 1024
MAX_INLINE_CHARS = 4 * ((MAX_BYTES + 2) // 3)
MAX_PIXELS = 1024 * 1024
MAX_DIMENSION = 4096
IMAGE_SIZE = 640
HOTDOG_CLASS = 52
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
# Compressed PNG text/ICC data must not consume Pillow's default 64 MiB metadata
# allowance independently of the measured image-pixel envelope.
PngImagePlugin.MAX_TEXT_CHUNK = 64 * 1024
PngImagePlugin.MAX_TEXT_MEMORY = 256 * 1024


class DetectorError(ValueError):
    """Fixed code only, safe for the protocol error result."""


def threshold_value(value):
    if type(value) not in {int, float} or not math.isfinite(value) or not 0.05 <= value <= 0.95:
        raise DetectorError("invalid_threshold")
    return float(value)


def inline_image(value):
    if type(value) is not str or not value:
        raise DetectorError("invalid_image_data")
    if len(value) > MAX_INLINE_CHARS:
        raise DetectorError("image_size_limit")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise DetectorError("invalid_image_data") from None
    if not data or base64.b64encode(data).decode("ascii") != value:
        raise DetectorError("invalid_image_data")
    if len(data) > MAX_BYTES:
        raise DetectorError("image_size_limit")
    return data


def decode_image(data):
    if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES:
        raise DetectorError("image_size_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP", "BMP", "GIF"}:
                    raise DetectorError("unsupported_image")
                if (image.width * image.height > MAX_PIXELS
                        or max(image.size) > MAX_DIMENSION or min(image.size) < 1):
                    raise DetectorError("image_pixel_limit")
                # Only the first animation frame is part of this task contract.
                return ImageOps.exif_transpose(image).convert("RGB")
    except DetectorError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise DetectorError("image_pixel_limit") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise DetectorError("invalid_image") from None


def preprocess(image):
    width, height = image.size
    scale = min(IMAGE_SIZE / width, IMAGE_SIZE / height)
    resized = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BILINEAR)
    left = round((IMAGE_SIZE - resized.width) / 2 - 0.1)
    top = round((IMAGE_SIZE - resized.height) / 2 - 0.1)
    canvas = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), (114, 114, 114))
    canvas.paste(resized, (left, top))
    tensor = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    return np.ascontiguousarray(tensor), scale, left, top


def postprocess(output, threshold, scale, left, top, width, height):
    if (not isinstance(output, np.ndarray) or output.dtype != np.float32
            or output.shape != (1, 84, 8400) or not np.isfinite(output).all()
            or not math.isfinite(scale) or scale <= 0):
        raise DetectorError("unsupported_model_output")
    rows = output[0].T
    scores = rows[:, 4:].max(axis=1)
    if (scores < 0).any() or (scores > 1).any():
        raise DetectorError("unsupported_model_output")
    mask = (rows[:, 4:].argmax(axis=1) == HOTDOG_CLASS) & (scores >= threshold)
    rows, scores = rows[mask], scores[mask]
    if not len(rows):
        return []
    boxes = np.column_stack((rows[:, 0] - rows[:, 2] / 2, rows[:, 1] - rows[:, 3] / 2,
                             rows[:, 0] + rows[:, 2] / 2, rows[:, 1] + rows[:, 3] / 2))
    areas = np.maximum(0, boxes[:, 2:] - boxes[:, :2]).prod(axis=1)
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


def validate_session(session):
    def declarations(items):
        return [{"name": item.name, "shape": item.shape, "type": item.type} for item in items]
    expected = json.loads((ROOT / "model-metadata.json").read_bytes())
    if (session.get_providers() != ["CPUExecutionProvider"]
            or declarations(session.get_inputs()) != expected["inputs"]
            or declarations(session.get_outputs()) != expected["outputs"]
            or session.get_modelmeta().custom_metadata_map != expected["metadata"]):
        raise DetectorError("unsupported_model_layout")


class HotdogDetector:
    def __init__(self):
        self._lock = threading.Lock()
        self._session = None

    def load(self):
        if self._session is not None:
            return
        try:
            import onnxruntime as ort
            path = ROOT / "model.onnx"
            with path.open("rb") as stream:
                data = stream.read(MODEL_BYTES + 1)
            if len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256:
                raise DetectorError("model_integrity_failed")
            options = ort.SessionOptions()
            options.log_severity_level = 4
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.add_session_config_entry("session.load_model_format", "ONNX")
            # Loading verified bytes also excludes mutable external-data paths.
            session = ort.InferenceSession(data, options, providers=["CPUExecutionProvider"])
            validate_session(session)
            self._session = session
        except DetectorError:
            raise
        except Exception:
            raise DetectorError("model_unavailable") from None

    def detect(self, data, confidence=0.35):
        threshold = threshold_value(confidence)
        # Decode, inference and postprocessing share the same bounded ownership.
        with self._lock:
            image = decode_image(data)
            try:
                self.load()
                tensor, scale, left, top = preprocess(image)
                output = self._session.run(["output0"], {"images": tensor})[0]
                detections = postprocess(output, threshold, scale, left, top, *image.size)
                return {"verdict": "hotdog" if detections else "not_hotdog", "hotdog": bool(detections),
                        "confidence": detections[0]["confidence"] if detections else None,
                        "threshold": threshold, "detections": detections,
                        "image": {"width": image.width, "height": image.height},
                        "model": "YOLO11n / COCO / ONNX CPU"}
            except DetectorError:
                raise
            except Exception:
                raise DetectorError("detection_failed") from None
            finally:
                image.close()


_detector = HotdogDetector()


def detect_hotdog(image_base64, confidence=0.35):
    """One canonical base64 image, CPU-only; no paths, URLs or image history."""
    return _detector.detect(inline_image(image_base64), confidence)
