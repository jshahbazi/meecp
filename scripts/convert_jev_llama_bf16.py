"""Export a BF16 GGUF control from the verified JevK5 source checkpoint.

Run with the locked experiments/uzu-rust/jev-scoring/llama-converter project.
Upstream output is discarded; only authored conversion metadata is reported.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments/uzu-rust/jev-scoring"
REVISION = "86d86ed4396b4130922f7b9af26e3d9fc11a591b"
EXPORT = ROOT / "runtime/uzu-rust/llama-converter"
ARCHIVE = EXPORT.with_suffix(".tar")
PROJECT = EXPERIMENT / "llama-converter"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("conversion_identity_invalid")


def verified_source(source: Path, manifest: dict) -> None:
    from acquire_jev_uzu_models import provenance

    require(manifest["source"] == provenance()["source"])
    # The upstream reader follows an optional shard index and otherwise scans
    # model*.safetensors. Reject every unpinned weight input it could consume.
    require({path.name for path in source.glob("model*.safetensors")} == {"model.safetensors"}
            and not (source / "model.safetensors.index.json").exists()
            and not (source / "pytorch_model.bin.index.json").exists()
            and not any(source.glob("pytorch_model*.bin")))
    for record in manifest["source"]["files"]:
        path = source / record["name"]
        require(path.is_file() and not path.is_symlink()
                and path.stat().st_size == record["bytes"] and digest(path) == record["sha256"])
    config = json.loads((source / "config.json").read_bytes())
    require(config["architectures"] == ["Qwen3_5ForCausalLM"]
            and config["model_type"] == "qwen3_5_text" and config["dtype"] == "bfloat16")
    with (source / "model.safetensors").open("rb") as stream:
        header_size = int.from_bytes(stream.read(8), "little")
        require(0 < header_size < 16 * 1024 * 1024)
        tensors = json.loads(stream.read(header_size))
    tensors.pop("__metadata__", None)
    require(tensors and all(record["dtype"] == "BF16" for record in tensors.values())
            and not any(name.startswith(("mtp.", "model.mtp.")) for name in tensors))


def export_converter(repository: Path) -> dict[str, str]:
    # git archive reads immutable committed objects, excluding unrelated local
    # modifications in the user's existing checkout.
    result = subprocess.run(["git", "archive", "--format=tar", REVISION], cwd=repository,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            check=True, timeout=60)
    if ARCHIVE.exists():
        require(ARCHIVE.is_file() and not ARCHIVE.is_symlink()
                and digest(ARCHIVE) == hashlib.sha256(result.stdout).hexdigest())
    else:
        ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        ARCHIVE.write_bytes(result.stdout)
    if EXPORT.exists():
        require(EXPORT.is_dir() and not EXPORT.is_symlink())
    else:
        EXPORT.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(EXPORT, filter="data")
    expected = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for member in archive:
            if member.isfile() and (member.name.endswith(".py") or member.name == "LICENSE"):
                stream = archive.extractfile(member)
                require(stream is not None)
                expected[member.name] = hashlib.sha256(stream.read()).hexdigest()
    for name, sha256 in expected.items():
        path = EXPORT / name
        require(path.is_file() and not path.is_symlink() and digest(path) == sha256)
    actual = {str(path.relative_to(EXPORT)) for path in EXPORT.rglob("*.py") if path.is_file()}
    require(actual == set(expected) - {"LICENSE"})
    return expected


def inspect_gguf(path: Path) -> tuple[dict[str, int], int]:
    sys.path.insert(0, str(EXPORT / "gguf-py"))
    import gguf

    reader = gguf.GGUFReader(path)
    require(reader.get_field("general.architecture").contents() == "qwen35")
    require(reader.get_field("general.file_type").contents() == int(gguf.LlamaFileType.MOSTLY_BF16))
    require(reader.get_field("qwen35.block_count").contents() == 32)
    nextn = reader.get_field("qwen35.nextn_predict_layers")
    require(nextn is None or nextn.contents() == 0)
    counts = dict(Counter(tensor.tensor_type.name for tensor in reader.tensors))
    require(counts.get("BF16", 0) > 0 and set(counts).issubset({"BF16", "F32"}))
    return counts, len(reader.tensors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "models/uzu-jevk5/source")
    parser.add_argument("--output", type=Path, default=ROOT / "models/uzu-jevk5/JevK5-4B-v0.3-BF16.gguf")
    parser.add_argument("--converter-repository", type=Path, default=Path.home() / ".unsloth/llama.cpp")
    args = parser.parse_args()
    stage = "validate"
    partial = None
    try:
        source, output = args.source.resolve(), args.output.resolve()
        original_manifest = EXPERIMENT / "MODEL_PROVENANCE.json"
        original_manifest_hash = digest(original_manifest)
        manifest = json.loads(original_manifest.read_bytes())
        control_manifest = EXPERIMENT / "BF16_CONTROL_PROVENANCE.json"
        require(not output.exists() and not control_manifest.exists())
        verified_source(source, manifest)
        require(manifest["conversion"]["source_weights_sha256"]
                == digest(source / "model.safetensors"))
        source_files = export_converter(args.converter_repository.resolve())
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_suffix(".partial.gguf")
        require(not partial.exists())
        stage = "convert"
        print(json.dumps({"stage": stage, "dtype": "bfloat16", "quantization": "none"}), flush=True)
        environment = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                           CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="1", TOKENIZERS_PARALLELISM="false")
        subprocess.run([sys.executable, str(EXPORT / "convert_hf_to_gguf.py"), str(source),
                        "--outfile", str(partial), "--outtype", "bf16", "--no-mtp"], cwd=EXPORT,
                       env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=600)
        stage = "verify"
        counts, tensor_count = inspect_gguf(partial)
        verified_source(source, manifest)
        require(source_files == export_converter(args.converter_repository.resolve()))
        require(digest(original_manifest) == original_manifest_hash)
        output_hash, output_size = digest(partial), partial.stat().st_size
        require(output_size > 8_000_000_000)
        partial.rename(output)
        weights_hash = digest(source / "model.safetensors")
        identity = {
            "schema_version": 1, "profile": "bf16-same-checkpoint",
            "source": manifest["source"], "calibration": manifest["calibration"],
            "uzu_model_manifest_sha256": digest(original_manifest),
            "llama_reference": {"file": output.name, "bytes": output_size, "sha256": output_hash,
                                "license": "Apache-2.0", "dtype": "bfloat16", "quantization": "none",
                                "source_weights_sha256": weights_hash},
            "llama_conversion": {
                "status": "complete", "repository": "https://github.com/ggml-org/llama.cpp",
                "revision": REVISION, "license": "MIT", "source_weights_sha256": weights_hash,
                "dtype": "bfloat16", "quantization": "none", "metadata_adaptation": "none",
                "mtp_policy": "excluded: no MTP tensors in source checkpoint",
                "script": {"path": "scripts/convert_jev_llama_bf16.py", "sha256": digest(Path(__file__))},
                "dependency_lock": {"path": str((PROJECT / "uv.lock").relative_to(ROOT)),
                                    "sha256": digest(PROJECT / "uv.lock")},
                "pyproject": {"path": str((PROJECT / "pyproject.toml").relative_to(ROOT)),
                              "sha256": digest(PROJECT / "pyproject.toml")},
                "converter_script_sha256": source_files["convert_hf_to_gguf.py"],
                "archive_sha256": digest(ARCHIVE), "converter_source_files": source_files,
                "tensor_type_counts": counts, "tensor_count": tensor_count,
                "precision_note": "BF16 multidimensional weights with converter-selected F32 tensors; no quantized tensors.",
            },
        }
        license_path = EXPERIMENT / "licenses/LLAMA_CPP_CONVERTER_LICENSE"
        shutil.copyfile(EXPORT / "LICENSE", license_path)
        control_manifest.write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"stage": "complete", "bytes": output_size, "sha256": output_hash,
                          "tensor_type_counts": counts, "tensor_count": tensor_count}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"stage": stage, "state": "failed", "error_type": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
