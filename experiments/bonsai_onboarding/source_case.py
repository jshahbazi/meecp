"""Bounded, read-only source discovery fixture for the original hotdog import."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

SOURCE_ROOT = Path(
    "/Users/johnshahbazian/Library/Application Support/mcpme/imports/"
    "a90d7f04-6d37-4a84-9180-150c970cd256/source"
)
SOURCE_IDENTITY = "sha256:d8123c391f8f01eae50d696e0a16e5f1875fdfd0d1e19f63a45693c3e6443ed0"
README_SHA256 = "8ee87400256a51ed830883ae2efce2b0bd5d04c5997589fba9bbb3330ed207c3"
DETECTOR_SHA256 = "87920cf5742c1bcd3527e184074390c593c64e937167b1ca1c84ac724edb8b70"
MAX_RAW_CHARS = 6500

# This is grader-side metadata, deliberately separate from the model input.
EXPECTED_GRADER = {
    "template": "python-function-v1",
    "module": "detector",
    "function": "detect_hotdog",
    "required_parameters": ["image_path"],
    "optional_defaults": {"confidence": 0.35},
    "model_filename": "models/yolo11n.onnx",
}

SOURCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": list(EXPECTED_GRADER),
    "properties": {
        "template": {"enum": ["python-function-v1", "existing-mcp-v1", "jevk5-llamacpp-v1", None]},
        "module": {"type": "string"},
        "function": {"type": "string"},
        "required_parameters": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "optional_defaults": {"type": "object", "additionalProperties": {"type": "number"}},
        "model_filename": {"type": ["string", "null"]},
    },
}


def _read(name: str, limit: int | None = None) -> str:
    value = (SOURCE_ROOT / name).read_text(encoding="utf-8")
    return value if limit is None else value[:limit]


def _sha256(name: str) -> str:
    return hashlib.sha256((SOURCE_ROOT / name).read_bytes()).hexdigest()


def _detector_excerpt() -> str:
    source = _read("detector.py")
    tree = ast.parse(source, filename="detector.py")
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "detect_hotdog"
    )
    segment = ast.get_source_segment(source, function) or ""
    return source[:1200] + "\n\n" + segment[:2200]


def _requirements_excerpt() -> str:
    lines = []
    for line in _read("requirements.txt").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "==" in stripped:
            lines.append(stripped.split("\\", 1)[0].strip())
    return "\n".join(lines)


def load_source_case() -> dict:
    """Return bounded trusted intent and source excerpts, without executing source."""
    if not SOURCE_ROOT.is_dir():
        raise FileNotFoundError(SOURCE_ROOT)
    if _sha256("README.md") != README_SHA256 or _sha256("detector.py") != DETECTOR_SHA256:
        raise ValueError("original_source_digest_mismatch")
    provenance = json.loads(_read("model-provenance.json", 1000))
    files = {
        "README.md": _read("README.md", 1800),
        "detector.py": _detector_excerpt(),
        "model-provenance.json": json.dumps(provenance, sort_keys=True, separators=(",", ":"))[:1000],
        "requirements.txt": _requirements_excerpt(),
    }
    raw_chars = sum(len(value) for value in files.values())
    if raw_chars > MAX_RAW_CHARS:
        raise ValueError("source_excerpt_limit")
    return {
        "id": "original_snapshot_discovery",
        "expected": dict(EXPECTED_GRADER),
        "file_sha256": {name: _sha256(name) for name in files},
        "payload": {
            "trusted_intent": "Detect whether or not an image is a hot dog",
            "offered_templates": ["python-function-v1", "existing-mcp-v1", "jevk5-llamacpp-v1"],
            "files": files,
            "file_sha256": {name: _sha256(name) for name in files},
        },
    }
