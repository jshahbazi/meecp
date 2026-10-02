import base64
import importlib.util
import io
import json
import socket
from pathlib import Path

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def load_detector():
    spec = importlib.util.spec_from_file_location("hotdog_detector_image_inputs", ROOT / "detector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


detector = load_detector()


def png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), (220, 40, 20)).save(stream, format="PNG")
    return stream.getvalue()


def result() -> dict:
    return {"verdict": "not_hotdog", "hotdog": False, "confidence": None,
            "threshold": 0.35, "detections": [], "image": {"width": 8, "height": 6}}


@pytest.fixture
def recording_detector(monkeypatch):
    calls = []

    def detect(data, confidence=0.35):
        detector.decode_image(data)
        calls.append((data, confidence))
        return result()

    monkeypatch.setattr(detector._detector, "detect", detect)
    return calls


def test_inline_plain_base64_is_decoded_and_sent_to_detector(recording_detector):
    payload = png_bytes()

    returned = detector.detect_hotdog(image_base64=base64.b64encode(payload).decode(), confidence=0.6)

    assert returned["verdict"] == "not_hotdog"
    assert recording_detector == [(payload, 0.6)]


@pytest.mark.parametrize("mime", ["jpeg", "png", "webp", "bmp", "gif"])
def test_inline_data_url_accepts_supported_image_mimes(recording_detector, mime):
    payload = png_bytes()

    detector.detect_hotdog(image_base64=f"data:image/{mime};base64,{base64.b64encode(payload).decode()}")

    assert recording_detector[0][0] == payload


@pytest.mark.parametrize("value", [None, "", "%%%", "not-base64", "data:image/svg+xml;base64,AAAA"])
def test_inline_rejects_missing_or_malformed_image_data(value):
    if value is None:
        with pytest.raises(detector.DetectorError, match="invalid_image_input"):
            detector.detect_hotdog()
    else:
        with pytest.raises(detector.DetectorError, match="invalid_image_data"):
            detector.detect_hotdog(image_base64=value)


def test_inline_rejects_whitespace_in_plain_base64():
    encoded = base64.b64encode(png_bytes()).decode()
    with pytest.raises(detector.DetectorError, match="invalid_image_data"):
        detector.detect_hotdog(image_base64=encoded[:10] + " " + encoded[10:])


def test_inline_rejects_noncanonical_base64_padding():
    encoded = base64.b64encode(png_bytes()).decode()
    padding_index = encoded.rfind("=")
    altered = encoded[:padding_index - 1] + "B" + encoded[padding_index:]

    with pytest.raises(detector.DetectorError, match="invalid_image_data"):
        detector.detect_hotdog(image_base64=altered)


def test_inline_rejects_corrupt_payload(recording_detector):
    encoded = base64.b64encode(b"corrupt image").decode()
    with pytest.raises(detector.DetectorError, match="invalid_image"):
        detector.detect_hotdog(image_base64=encoded)
    assert recording_detector == []


def test_inline_rejects_both_sources_and_neither_source(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(png_bytes())
    encoded = base64.b64encode(png_bytes()).decode()

    with pytest.raises(detector.DetectorError, match="invalid_image_input"):
        detector.detect_hotdog(str(path), image_base64=encoded)
    with pytest.raises(detector.DetectorError, match="invalid_image_input"):
        detector.detect_hotdog()


def test_inline_enforces_character_limit_before_decoding(monkeypatch):
    limit = detector.MAX_INLINE_CHARS
    value = "A" * (limit + 1)
    monkeypatch.setattr(detector, "decode_image", lambda _: pytest.fail("must reject before decode"))

    with pytest.raises(detector.DetectorError, match="image_size_limit"):
        detector.detect_hotdog(image_base64=value)


def test_inline_enforces_decoded_size_limit(monkeypatch):
    payload = b"x" * (detector.MAX_INLINE_BYTES + 1)
    encoded = base64.b64encode(payload).decode()
    monkeypatch.setattr(detector, "decode_image", lambda _: pytest.fail("must reject oversized input"))

    with pytest.raises(detector.DetectorError, match="image_size_limit"):
        detector.detect_hotdog(image_base64=encoded)


def test_inline_accepts_exact_decoded_size_limit(monkeypatch):
    payload = b"x" * detector.MAX_INLINE_BYTES
    encoded = base64.b64encode(payload).decode()
    monkeypatch.setattr(detector._detector, "detect", lambda data, confidence=0.35: result())

    assert detector.detect_hotdog(image_base64=encoded)["verdict"] == "not_hotdog"


def test_tool_schema_inline_limit_tracks_detector_constants():
    schema = json.loads((ROOT / "mcp-tool.json").read_text())["input_schema"]
    assert schema["properties"]["image_base64"]["maxLength"] == detector.MAX_INLINE_CHARS + 64


def test_inline_does_not_open_files_or_use_network(monkeypatch, recording_detector):
    payload = base64.b64encode(png_bytes()).decode()
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("inline input opened a file"))
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("inline input used network"))

    detector.detect_hotdog(image_base64=payload)
    assert recording_detector


def test_path_input_preserves_absolute_path_behavior(tmp_path, recording_detector):
    path = tmp_path / "image.png"
    payload = png_bytes()
    path.write_bytes(payload)

    detector.detect_hotdog(str(path))

    assert recording_detector == [(payload, 0.35)]


def test_path_input_rejects_relative_paths(tmp_path, recording_detector, monkeypatch):
    path = tmp_path / "image.png"
    path.write_bytes(png_bytes())
    monkeypatch.chdir(tmp_path)

    with pytest.raises(detector.DetectorError, match="invalid_image_path"):
        detector.detect_hotdog(path.name)
    assert recording_detector == []
