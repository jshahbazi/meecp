"""Exact development-fixture identities, independent of JevK5 profiles."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct

MODEL_REVISION = "9217f5db79a29953eb74d5343926648285ec7e67"
MODEL_FILENAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
MODEL_BYTES = 491400032
MODEL_SHA256 = "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
LICENSE_SHA256 = "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e"
TEMPLATE_SHA256 = "4e9918361c284a93880606d182d64da6a9fe97cdc1f5c5a78c1c8840444246fc"
EMBEDDED_TEMPLATE_SHA256 = "d5495a1e5db0611132a97e46a65dbb64a642a499421228b9c8b93229097fa9a4"
TOKENIZER_SHA256 = "c828d4ced3ffa97554baf70310ae1b232a03bb63eff517952a9222567b44b5b8"
ENGINE_BUILD = "11146-7fe450e19305b828c199d602c23a8337aaa1f03b"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tokenizer_metadata(path: Path) -> dict:
    """Read bounded GGUF v3 metadata after the caller verifies exact file hash.

    Values are retained only in memory for identity checks, never emitted by the
    acceptance harness. This is an exact-fixture reader, not a GGUF importer.
    """
    with path.open("rb") as stream:
        def read(size):
            if size < 0 or stream.tell() + size > 16 * 1024**2:
                raise ValueError("metadata_bounds")
            raw = stream.read(size)
            if len(raw) != size:
                raise ValueError("metadata_bounds")
            return raw

        def number(fmt):
            return struct.unpack("<" + fmt, read(struct.calcsize(fmt)))[0]

        def string():
            return read(number("Q")).decode("utf-8")

        def value(kind, depth=0):
            if depth > 1:
                raise ValueError("metadata_bounds")
            formats = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
                       6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
            if kind in formats:
                return number(formats[kind])
            if kind == 8:
                return string()
            if kind == 9:
                element, count = number("I"), number("Q")
                if count > 250000:
                    raise ValueError("metadata_bounds")
                return [value(element, depth + 1) for _ in range(count)]
            raise ValueError("metadata_type")

        if read(4) != b"GGUF" or number("I") != 3:
            raise ValueError("metadata_version")
        number("Q")
        count = number("Q")
        if count > 1000:
            raise ValueError("metadata_bounds")
        result = {}
        for _ in range(count):
            key = string()
            item = value(number("I"))
            if key.startswith("tokenizer."):
                if key in result:
                    raise ValueError("metadata_duplicate")
                result[key] = item
        return result
