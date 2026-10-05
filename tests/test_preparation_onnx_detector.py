"""Task tests run with the locked ONNX template development environment."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path
import threading

import pytest

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pinned_onnx_detector_test", ROOT / "templates/onnx-hotdog-v1/detector.py")
detector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detector)


def encoded(size=(32,24), kind="PNG"):
    buffer = io.BytesIO(); Image.new("RGB", size).save(buffer, format=kind)
    return buffer.getvalue()


def test_controlled_positive_tensor_nms_scaling_clipping_and_class_selection():
    output = np.zeros((1,84,8400),dtype=np.float32)
    for index, (x,y,w,h,kind,score) in enumerate([(150,125,100,60,52,.9), (155,127,100,60,52,.8),
                                               (250,125,40,40,52,.7), (160,100,40,40,1,.99)]):
        output[0,:4,index] = [x,y,w,h]; output[0,4+kind,index] = score
    assert detector.postprocess(output,.35,.5,100,50,400,300) == [
        {"confidence":.9,"box":[0.,90.,200.,210.]}, {"confidence":.7,"box":[260.,110.,340.,190.]}]


@pytest.mark.parametrize("kind", ["shape","nan","inf","float64","score"])
def test_wrong_tensor_contract_is_rejected(kind):
    output = np.zeros((1,84,8400), dtype=np.float32)
    if kind == "shape": output = output[:,:,:1]
    if kind == "nan": output[0,4,0] = np.nan
    if kind == "inf": output[0,4,0] = np.inf
    if kind == "float64": output = output.astype(np.float64)
    if kind == "score": output[0,56,0] = 1.01
    with pytest.raises(detector.DetectorError, match="unsupported_model_output"):
        detector.postprocess(output,.35,1.,0,0,640,640)


@pytest.mark.parametrize("value", [True,".35",None,float("nan"),float("inf"),.049,.951])
def test_threshold_requires_bounded_finite_number(value):
    with pytest.raises(detector.DetectorError, match="invalid_threshold"):
        detector.threshold_value(value)


@pytest.mark.parametrize("value", ["", "!!!!", "YQ==\n", "YR==", "data:image/png;base64,YQ=="])
def test_base64_is_canonical_and_never_a_path_or_url(value):
    with pytest.raises(detector.DetectorError, match="invalid_image_data"):
        detector.inline_image(value)


def test_encoded_byte_ceiling_is_exact():
    assert detector.inline_image(base64.b64encode(b"a" * detector.MAX_BYTES).decode()) == b"a" * detector.MAX_BYTES
    with pytest.raises(detector.DetectorError, match="image_size_limit"):
        detector.inline_image(base64.b64encode(b"a" * (detector.MAX_BYTES+1)).decode())


@pytest.mark.parametrize("size", [(1024,1024),(4096,256),(256,4096)])
def test_maximum_accepted_pixel_envelope(size):
    with detector.decode_image(encoded(size)) as image:
        assert image.size == size
        tensor, scale, left, top = detector.preprocess(image)
        assert tensor.shape == (1,3,640,640) and tensor.dtype == np.float32
        assert tensor.flags.c_contiguous and 0 < scale <= 1


@pytest.mark.parametrize("size", [(1025,1024),(4097,1),(1,4097)])
def test_pixel_or_dimension_boundary_plus_one_rejected(size):
    with pytest.raises(detector.DetectorError, match="image_pixel_limit"):
        detector.decode_image(encoded(size))


def test_extreme_aspect_letterbox_never_uses_zero_pixels():
    with Image.new("RGB", (4096,1)) as image:
        tensor, _, _, _ = detector.preprocess(image)
        assert tensor.shape == (1,3,640,640)


def test_compressed_png_metadata_has_independent_memory_ceiling():
    from PIL.PngImagePlugin import PngInfo
    information = PngInfo(); information.add_text("description", "a" * (64*1024+1), zip=True)
    buffer = io.BytesIO(); Image.new("RGB",(8,8)).save(buffer,format="PNG",pnginfo=information)
    assert len(buffer.getvalue()) < detector.MAX_BYTES
    with pytest.raises(detector.DetectorError,match="invalid_image"):
        detector.decode_image(buffer.getvalue())


def test_gif_uses_first_animation_frame_only():
    buffer = io.BytesIO()
    first,second = Image.new("RGB",(8,6),(255,0,0)),Image.new("RGB",(8,6),(0,0,255))
    first.save(buffer,format="GIF",save_all=True,append_images=[second],duration=100,loop=0)
    with detector.decode_image(buffer.getvalue()) as decoded:
        assert decoded.getpixel((0,0)) == (255,0,0)


def test_exact_session_layout_classes_export_metadata_and_cpu_provider(monkeypatch):
    expected = json.loads((ROOT / "templates/onnx-hotdog-v1/model-metadata.json").read_bytes())
    class Declaration:
        def __init__(self, value): self.__dict__.update(value)
    class Session:
        def get_providers(self): return ["CPUExecutionProvider"]
        def get_inputs(self): return [Declaration(item) for item in expected["inputs"]]
        def get_outputs(self): return [Declaration(item) for item in expected["outputs"]]
        def get_modelmeta(self):
            class Metadata: custom_metadata_map = expected["metadata"]
            return Metadata()
    session = Session(); detector.validate_session(session)
    for field, value in [("task","classify"),("names","{52: 'hot dog'}"),("args","{'nms': True}")]:
        old = expected["metadata"][field]; expected["metadata"][field] = value
        with pytest.raises(detector.DetectorError, match="unsupported_model_layout"):
            detector.validate_session(session)
        expected["metadata"][field] = old
    monkeypatch.setattr(session, "get_providers", lambda: ["CoreMLExecutionProvider","CPUExecutionProvider"])
    with pytest.raises(detector.DetectorError, match="unsupported_model_layout"):
        detector.validate_session(session)


def test_decode_is_serialized_with_inference(monkeypatch):
    instance = detector.HotdogDetector()
    reached = threading.Event()
    def decoded(data):
        assert instance._lock.locked(); reached.set(); raise detector.DetectorError("invalid_image")
    monkeypatch.setattr(detector, "decode_image", decoded)
    with pytest.raises(detector.DetectorError): instance.detect(b"synthetic")
    assert reached.is_set()
