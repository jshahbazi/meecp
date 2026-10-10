"""Convert pinned merged JevK5 weights with the pinned, locked Lalamo runtime.

Run using uv in runtime/uzu-rust/lalamo, with --locked --no-dev. Only conversion
metadata is reported; upstream console output and exception dumps are suppressed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from acquire_jev_uzu_models import provenance as pinned_provenance

ROOT = Path(__file__).resolve().parents[1]
LALAMO_REVISION = "831accb8252a6b6606c378048caeb44178f1a050"
os.environ["JAX_PLATFORMS"] = "cpu"
os.environ["JAX_DISABLE_JIT"] = "1"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "models/uzu-jevk5/source")
    parser.add_argument("--output", type=Path, default=ROOT / "models/uzu-jevk5/bf16")
    args = parser.parse_args()
    stage = "validate"
    try:
        source = args.source.resolve()
        output = args.output.resolve()
        manifest = ROOT / "experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json"
        provenance = json.loads(manifest.read_text())
        pinned = pinned_provenance()
        if any(provenance.get(key) != pinned[key] for key in ("source", "llama_reference", "calibration")):
            raise ValueError("source_provenance_mismatch")
        checkout = ROOT / "runtime/uzu-rust/lalamo"
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
        if revision != LALAMO_REVISION:
            raise ValueError("converter_revision_mismatch")
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout):
            raise ValueError("converter_source_modified")
        for record in provenance["source"]["files"]:
            path = source / record["name"]
            if path.stat().st_size != record["bytes"] or digest(path) != record["sha256"]:
                raise ValueError("source_artifact_mismatch")
        if output.exists():
            raise ValueError("output_already_exists")
        config = json.loads((source / "config.json").read_text())
        if config["model_type"] != "qwen3_5_text" or config["architectures"] != ["Qwen3_5ForCausalLM"]:
            raise ValueError("source_architecture_mismatch")
        # Lalamo names the same text architecture qwen3_5. Adapt metadata only;
        # the pinned source files and merged checkpoint remain byte-identical.
        config["model_type"] = "qwen3_5"
        print(json.dumps({"stage": "convert", "dtype": "bfloat16"}), flush=True)
        stage = "convert"
        with tempfile.TemporaryDirectory(prefix="meecp-jev-conversion-") as directory:
            adapted = Path(directory)
            for record in provenance["source"]["files"]:
                if record["name"] != "config.json":
                    os.link(source / record["name"], adapted / record["name"])
            (adapted / "config.json").write_text(json.dumps(config))
            adapted_config_hash = digest(adapted / "config.json")
            with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                from lalamo.commands import DType, convert
                from lalamo.model_import.model_configs.huggingface import HFQwen35Config
                from lalamo.model_import.model_spec import ConfigMap, FileSpec, LanguageModelSpec
                from lalamo.model_import.origins import LocalOrigin

                specification = LanguageModelSpec(
                    vendor="alibiserikbay", family="JevK5-Qwen3.5", name="JevK5-4B-v0.3", size="4B",
                    origin=LocalOrigin(root=str(adapted), weight_files=("model.safetensors",)),
                    config_type=HFQwen35Config,
                    configs=ConfigMap(model_config=FileSpec("config.json"), tokenizer=FileSpec("tokenizer.json"),
                        tokenizer_config=FileSpec("tokenizer_config.json"),
                        generation_config=FileSpec("generation_config.json"),
                        chat_template=FileSpec("chat_template.jinja")),
                )
                convert(specification, output, dtype=DType.BFLOAT16)
        stage = "verify"
        for name in ("jevk5_config.json", "chat_template.jinja", "tokenizer_config.json", "generation_config.json"):
            shutil.copyfile(source / name, output / name)
        if digest(output / "tokenizer.json") != digest(source / "tokenizer.json"):
            raise ValueError("tokenizer_export_mismatch")
        artifacts = [{"name": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}
                     for path in sorted(output.iterdir()) if path.is_file()]
        provenance["conversion"] = {
            "status": "complete", "repository": "https://github.com/trymirai/lalamo",
            "revision": revision, "license": "MIT", "uv_lock_sha256": digest(checkout / "uv.lock"),
            "script_sha256": digest(Path(__file__)), "dtype": "bfloat16", "quantization": "none",
            "metadata_adaptation": {"model_type": "qwen3_5_text -> qwen3_5",
                                    "adapted_config_sha256": adapted_config_hash},
            "source_weights_sha256": digest(source / "model.safetensors"), "files": artifacts,
            "limitations": "Native BF16 export versus separately converted Q8_0 GGUF; numerical equality is not presumed.",
        }
        manifest.write_text(json.dumps(provenance, indent=2) + "\n")
        license_path = manifest.parent / "licenses/LALAMO_LICENSE"
        license_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / "LICENSE", license_path)
        print(json.dumps({"stage": "complete", "verified_files": len(artifacts)}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"stage": stage, "state": "failed", "error_type": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
