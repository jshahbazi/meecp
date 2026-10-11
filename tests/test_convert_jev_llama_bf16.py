"""Authored fixtures for the BF16 control's source and precision fences."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "convert_jev_llama_bf16", ROOT / "scripts/convert_jev_llama_bf16.py"
)
conversion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conversion)


def safetensors_bytes(name="model.weight", dtype="BF16"):
    header = json.dumps({name: {"dtype": dtype, "shape": [1], "data_offsets": [0, 2]}}).encode()
    return struct.pack("<Q", len(header)) + header + b"\x00\x00"


@pytest.fixture
def verified_source(tmp_path, monkeypatch):
    import acquire_jev_uzu_models

    source = tmp_path / "source"
    source.mkdir()
    config = {"architectures": ["Qwen3_5ForCausalLM"],
              "model_type": "qwen3_5_text", "dtype": "bfloat16",
              "num_hidden_layers": 32, "mtp_num_hidden_layers": 1}
    files = {"config.json": json.dumps(config).encode(),
             "model.safetensors": safetensors_bytes(),
             "tokenizer.json": b"authored tokenizer fixture"}
    records = []
    for name, contents in files.items():
        (source / name).write_bytes(contents)
        records.append({"name": name, "bytes": len(contents),
                        "sha256": hashlib.sha256(contents).hexdigest()})
    identity = {"repository": "authored fixture", "revision": "fixed", "files": records}
    monkeypatch.setattr(acquire_jev_uzu_models, "provenance", lambda: {"source": identity})
    return source, {"source": identity}


def test_source_requires_pinned_identity(verified_source):
    source, manifest = verified_source
    conversion.verified_source(source, manifest)
    changed = json.loads(json.dumps(manifest))
    changed["source"]["revision"] = "different"
    with pytest.raises(ValueError, match="conversion_identity_invalid"):
        conversion.verified_source(source, changed)


@pytest.mark.parametrize("mutation", ["same_size_tamper", "missing", "symlink"])
def test_source_rejects_unverified_weight_bytes(verified_source, mutation):
    source, manifest = verified_source
    weights = source / "model.safetensors"
    if mutation == "same_size_tamper":
        weights.write_bytes(b"x" * weights.stat().st_size)
    elif mutation == "missing":
        weights.unlink()
    else:
        external = source.parent / "external.safetensors"
        weights.rename(external)
        weights.symlink_to(external)
    with pytest.raises(ValueError, match="conversion_identity_invalid"):
        conversion.verified_source(source, manifest)


@pytest.mark.parametrize("injected_name", [
    "model.safetensors.index.json", "model-00001-of-00001.safetensors",
    "pytorch_model.bin.index.json", "pytorch_model.bin",
])
def test_source_rejects_unverified_weight_selection(verified_source, injected_name):
    source, manifest = verified_source
    (source / injected_name).write_bytes(b"authored injection fixture")
    with pytest.raises(ValueError, match="conversion_identity_invalid"):
        conversion.verified_source(source, manifest)


def test_source_allows_benign_acquisition_receipts(verified_source):
    source, manifest = verified_source
    (source / ".cache").mkdir()
    (source / "model.safetensors.partial.receipt.json").write_text("{}")
    conversion.verified_source(source, manifest)


@pytest.mark.parametrize("tensor_identity", [
    {"dtype": "F32"}, {"name": "mtp.weight"}, {"name": "model.mtp.weight"},
])
def test_source_header_refuses_precision_or_mtp_mismatch(verified_source, tensor_identity):
    source, manifest = verified_source
    contents = safetensors_bytes(**tensor_identity)
    (source / "model.safetensors").write_bytes(contents)
    record = next(item for item in manifest["source"]["files"] if item["name"] == "model.safetensors")
    record.update(bytes=len(contents), sha256=hashlib.sha256(contents).hexdigest())
    with pytest.raises(ValueError, match="conversion_identity_invalid"):
        conversion.verified_source(source, manifest)


def mock_gguf(monkeypatch, architecture="qwen35", file_type=30,
              tensor_types=("BF16", "F32"), block_count=32, nextn=None):
    fields = {"general.architecture": architecture, "general.file_type": file_type,
              "qwen35.block_count": block_count}
    if nextn is not None:
        fields["qwen35.nextn_predict_layers"] = nextn
    reader = SimpleNamespace(
        get_field=lambda name: SimpleNamespace(contents=lambda: fields[name]) if name in fields else None,
        tensors=[SimpleNamespace(tensor_type=SimpleNamespace(name=name)) for name in tensor_types],
    )
    monkeypatch.setitem(sys.modules, "gguf", SimpleNamespace(
        GGUFReader=lambda _: reader,
        LlamaFileType=SimpleNamespace(MOSTLY_BF16=30),
    ))


def test_gguf_accepts_bf16_with_converter_f32_tensors(monkeypatch, tmp_path):
    mock_gguf(monkeypatch, tensor_types=("BF16", "BF16", "F32"))
    assert conversion.inspect_gguf(tmp_path / "fixture.gguf") == ({"BF16": 2, "F32": 1}, 3)


def test_gguf_accepts_explicit_zero_mtp_layers(monkeypatch, tmp_path):
    mock_gguf(monkeypatch, nextn=0)
    assert conversion.inspect_gguf(tmp_path / "fixture.gguf")[1] == 2


@pytest.mark.parametrize("identity", [
    {"architecture": "different"},
    {"file_type": 7},
    {"tensor_types": ("BF16", "Q8_0")},
    {"tensor_types": ("BF16", "F16")},
    {"tensor_types": ("F32",)},
    {"block_count": 33},
    {"nextn": 1},
])
def test_gguf_refuses_false_bf16_claim(monkeypatch, tmp_path, identity):
    mock_gguf(monkeypatch, **identity)
    with pytest.raises(ValueError, match="conversion_identity_invalid"):
        conversion.inspect_gguf(tmp_path / "fixture.gguf")
