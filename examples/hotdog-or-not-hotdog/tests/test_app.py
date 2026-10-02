import http.client
import importlib.util
import io
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


detector = load_module("hotdog_detector", "detector.py")
sys.modules["detector"] = detector
app = load_module("hotdog_app", "app.py")


def model_output(*rows):
    output = np.zeros((1, 84, 8400), dtype=np.float32)
    for index, (x, y, width, height, class_id, score) in enumerate(rows):
        output[0, :4, index] = [x, y, width, height]
        output[0, 4 + class_id, index] = score
    return output


def test_postprocess_uses_winning_class_and_nms_then_scales_boxes():
    output = model_output(
        (150, 125, 100, 60, detector.HOTDOG_CLASS, 0.90),
        (155, 127, 100, 60, detector.HOTDOG_CLASS, 0.80),
        (250, 125, 40, 40, detector.HOTDOG_CLASS, 0.70),
        (160, 100, 40, 40, 1, 0.99),
    )

    detections = detector.postprocess(output, 0.35, 0.5, 100, 50, 400, 300)

    assert len(detections) == 2
    assert detections[0]["confidence"] == 0.9
    assert detections[0]["box"] == [0.0, 90.0, 200.0, 210.0]
    assert detections[1]["box"] == [260.0, 110.0, 340.0, 190.0]


def test_postprocess_rejects_bad_output_and_filters_invalid_boxes():
    with pytest.raises(detector.DetectorError, match="unsupported_model_output"):
        detector.postprocess(np.zeros((1, 84, 3), dtype=np.float32), 0.35, 1, 0, 0, 10, 10)
    bad = model_output((0, 0, 20, 20, detector.HOTDOG_CLASS, 0.8))
    bad[0, 0, 0] = np.nan
    with pytest.raises(detector.DetectorError, match="unsupported_model_output"):
        detector.postprocess(bad, 0.35, 1, 0, 0, 10, 10)
    zero = model_output((0, 0, -20, 20, detector.HOTDOG_CLASS, 0.8))
    assert detector.postprocess(zero, 0.35, 1, 0, 0, 10, 10) == []


def test_preprocess_handles_extreme_aspect_ratio_without_zero_dimension():
    tensor, scale, left, top = detector.preprocess(Image.new("RGB", (2000, 1), "white"))
    assert tensor.shape == (1, 3, 640, 640)
    assert scale == 0.32
    assert left == 0
    assert top == 319


def test_postprocess_requires_hotdog_to_be_winning_class():
    output = model_output((320, 320, 80, 80, detector.HOTDOG_CLASS, 0.8))
    output[0, 5, 0] = 0.9
    assert detector.postprocess(output, 0.35, 1, 0, 0, 640, 640) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "bad", 0.01, 0.96])
def test_threshold_value_rejects_invalid_values(value):
    with pytest.raises(detector.DetectorError, match="invalid_threshold"):
        detector.threshold_value(value)


def test_decode_image_rejects_corrupt_and_oversized_inputs():
    with pytest.raises(detector.DetectorError, match="invalid_image"):
        detector.decode_image(b"not an image")
    with pytest.raises(detector.DetectorError, match="image_size_limit"):
        detector.decode_image(b"x" * (detector.MAX_BYTES + 1))


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), "white").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def running_server(monkeypatch):
    monkeypatch.setattr(app.HotdogDetector, "load", lambda self: None)
    monkeypatch.setattr(app.HotdogDetector, "detect", lambda self, data, confidence: {
        "verdict": "not_hotdog", "hotdog": False, "confidence": None,
        "threshold": float(confidence), "detections": [], "image": {"width": 8, "height": 6},
    })
    server = app.PhotoServer(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        assert not thread.is_alive()


def request(server, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response, payload


def test_http_bootstrap_is_one_use_and_authenticated_status(running_server):
    response, _ = request(running_server, "GET", f"/?bootstrap={running_server.token}")
    assert response.status == 303
    cookie = response.getheader("Set-Cookie")
    assert cookie and cookie.startswith("session=")
    headers = {"Cookie": cookie.split(";", 1)[0]}
    response, payload = request(running_server, "GET", "/api/status", headers)
    assert response.status == 200
    assert b'"ready": true' in payload
    response, _ = request(running_server, "GET", f"/?bootstrap={running_server.token}")
    assert response.status == 401


def test_http_rejects_unauthorized_host_and_origin(running_server):
    response, _ = request(running_server, "GET", "/api/status")
    assert response.status == 401
    response, _ = request(running_server, "GET", f"/?bootstrap={running_server.token}", {"Host": "evil.test"})
    assert response.status == 403


def test_http_sanitizes_unicode_bootstrap_and_invalid_cookie(running_server):
    response, payload = request(running_server, "GET", "/?bootstrap=%C3%A9")
    assert response.status == 401
    assert payload == b'{"error": "unauthorized"}'
    response, payload = request(running_server, "GET", "/api/status", {"Cookie": "session=%%%"})
    assert response.status == 401
    assert payload == b'{"error": "unauthorized"}'


def test_http_methods_require_authentication(running_server):
    for method in ("OPTIONS", "HEAD", "PUT", "PATCH", "DELETE", "TRACE"):
        response, _ = request(running_server, method, "/api/status")
        assert response.status == 401, method


def authenticated_headers(server):
    response, _ = request(server, "GET", f"/?bootstrap={server.token}")
    return {"Cookie": response.getheader("Set-Cookie").split(";", 1)[0]}


def test_http_post_requires_same_origin_and_rejects_oversized_body(running_server):
    headers = authenticated_headers(running_server)
    headers.update({"Content-Type": "application/octet-stream", "Content-Length": "1"})
    response, payload = request(running_server, "POST", "/api/detect", headers, b"x")
    assert response.status == 403
    assert payload == b'{"error": "invalid_origin"}'
    headers["Origin"] = running_server.origin
    headers["Content-Length"] = str(detector.MAX_BYTES + 1)
    response, payload = request(running_server, "POST", "/api/detect", headers, b"x")
    assert response.status == 413
    assert payload == b'{"error": "image_size_limit"}'


def test_http_second_upload_is_rejected_while_detector_is_busy(running_server, monkeypatch):
    headers = authenticated_headers(running_server)
    headers.update({"Origin": running_server.origin, "Content-Type": "application/octet-stream"})
    entered = threading.Event()
    release = threading.Event()

    def waiting_detect(data, confidence):
        entered.set()
        assert release.wait(timeout=3)
        return {"verdict": "not_hotdog", "hotdog": False, "confidence": None,
                "threshold": float(confidence), "detections": [], "image": {"width": 8, "height": 6}}

    monkeypatch.setattr(running_server.detector, "detect", waiting_detect)
    first_result = []
    first = threading.Thread(target=lambda: first_result.append(request(
        running_server, "POST", "/api/detect", headers, image_bytes())))
    first.start()
    assert entered.wait(timeout=3)
    response, payload = request(running_server, "POST", "/api/detect", headers, image_bytes())
    assert response.status == 429
    assert payload == b'{"error": "detector_busy"}'
    release.set()
    first.join(timeout=3)
    assert not first.is_alive()
    assert first_result and first_result[0][0].status == 200
    response, _ = request(running_server, "GET", f"/?bootstrap={running_server.token}", {"Origin": "http://evil.test"})
    assert response.status == 403


def test_http_detects_image_and_reports_detector_errors(running_server, monkeypatch):
    response, _ = request(running_server, "GET", f"/?bootstrap={running_server.token}")
    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
    headers = {"Cookie": cookie, "Origin": running_server.origin, "Content-Type": "application/octet-stream"}
    response, payload = request(running_server, "POST", "/api/detect", headers, image_bytes())
    assert response.status == 200
    assert b'"verdict": "not_hotdog"' in payload

    monkeypatch.setattr(running_server.detector, "detect", lambda data, confidence: (_ for _ in ()).throw(detector.DetectorError("invalid_image")))
    response, payload = request(running_server, "POST", "/api/detect", headers, b"bad")
    assert response.status == 400
    assert payload == b'{"error": "invalid_image"}'
