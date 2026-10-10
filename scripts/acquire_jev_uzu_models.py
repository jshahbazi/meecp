"""Acquire the pinned merged JevK5 checkpoint for isolated Uzu scoring tests."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c4f7fdb3aeab5582336406e78d3bef11bf98833d"
REPOSITORY = "alibiserikbay/JevK5"
FILES = [
    ("chat_template.jinja", 7756, "a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715"),
    ("config.json", 1979, "63f47812d0f11118e4d252d2b3ad488707eb9287a11589f4fd382a1d31182724"),
    ("generation_config.json", 116, "62153eb6c69f2e1f426beaa8002b7186437e949c7588167085df14e10e9c0a73"),
    ("jevk5_config.json", 52, "0d689fd13d15dc962265e2ae10b56359706ab5d05ad24e00e6334e4c19cf83d2"),
    ("model.safetensors", 8411558400, "13824e47f2e40fe052f06943976cf742cb366ba305741a111e75a8ebae907a9c"),
    ("README.md", 14966, "ae04e164af42c70837b1e79a5d5414c3b9c5641a7651047223756404fd6a2786"),
    ("tokenizer_config.json", 1125, "9cf04fffe3d8c3b85e439fb35c7acad0761ab51c422a8c4256d9f887c3a0be7d"),
    ("tokenizer.json", 19989325, "06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523"),
]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matches(path: Path, size: int, checksum: str) -> bool:
    return path.is_file() and path.stat().st_size == size and digest(path) == checksum


def ranged_download(url: str, partial: Path, size: int, *, chunk_size: int = 4 * 1024**2) -> None:
    """Bounded parallel ranges; validate every range and the final artifact hash."""
    descriptor = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    receipt_path = partial.with_suffix(partial.suffix + ".receipt.json")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = {"url": url, "size": size, "chunk_size": chunk_size}
        recovered = {}
        if receipt_path.exists() and os.fstat(descriptor).st_size == size:
            prior = json.loads(receipt_path.read_text())
            if prior.get("identity") == identity:
                for offset, checksum in prior.get("completed", {}).items():
                    start = int(offset)
                    if start < 0 or start >= size or start % chunk_size:
                        continue
                    block = os.pread(descriptor, min(chunk_size, size - start), start)
                    if len(block) == min(chunk_size, size - start) and hashlib.sha256(block).hexdigest() == checksum:
                        recovered[str(start)] = checksum
        # Filesystem extents include preallocated/unwritten bytes. Reuse only
        # explicit completed-chunk receipts whose local bytes still match.
        os.ftruncate(descriptor, size)

        def checkpoint() -> None:
            with tempfile.NamedTemporaryFile(mode="w", dir=partial.parent, delete=False) as stream:
                json.dump({"identity": identity, "completed": recovered}, stream)
                temporary = Path(stream.name)
            os.replace(temporary, receipt_path)

        checkpoint()

        def fetch(start: int) -> tuple[int, int, str]:
            end = min(start + chunk_size, size) - 1
            for attempt in range(3):
                try:
                    range_url = url + f"?download=true&range_offset={start}"
                    curl = shutil.which("curl")
                    if curl:
                        with tempfile.NamedTemporaryFile(dir=partial.parent) as temporary:
                            result = subprocess.run([curl, "-q", "--location", "--fail", "--silent",
                                "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "5",
                                "--connect-timeout", "15", "--max-time", "120", "--max-filesize", str(end-start+1),
                                "--range", f"{start}-{end}", "--dump-header", "-", "--output", temporary.name,
                                range_url], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=130)
                            ranges = re.findall(rb"(?im)^content-range:\s*([^\r\n]+)", result.stdout)
                            statuses = re.findall(rb"(?m)^HTTP/\S+\s+(\d+)", result.stdout)
                            if result.returncode or not ranges or not statuses or statuses[-1] != b"206" or ranges[-1] != f"bytes {start}-{end}/{size}".encode():
                                raise ValueError("artifact_range_mismatch")
                            block = Path(temporary.name).read_bytes()
                    else:
                        request = urllib.request.Request(range_url, headers={
                            "User-Agent": "meecp-pinned-model-acquisition/1", "Range": f"bytes={start}-{end}"})
                        with urllib.request.urlopen(request, timeout=120) as response:
                            if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                                raise ValueError("artifact_range_mismatch")
                            block = response.read(end - start + 2)
                    if len(block) != end - start + 1:
                        raise ValueError("artifact_range_mismatch")
                    position = 0
                    while position < len(block):
                        position += os.pwrite(descriptor, block[position:], start + position)
                    return start, len(block), hashlib.sha256(block).hexdigest()
                except Exception:
                    if attempt == 2:
                        raise ValueError("artifact_range_failed") from None
                    time.sleep(attempt + 1)
            raise ValueError("artifact_range_failed")

        completed = sum(min(chunk_size, size - int(start)) for start in recovered)
        print(json.dumps({"state": "range_resume", "recovered_bytes": completed,
                          "expected_bytes": size}), flush=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(fetch, start) for start in range(0, size, chunk_size) if str(start) not in recovered]
            for index, future in enumerate(as_completed(futures), 1):
                start, count, checksum = future.result()
                recovered[str(start)] = checksum
                checkpoint()
                completed += count
                if index % 32 == 0 or completed == size:
                    print(json.dumps({"state": "downloading", "completed_bytes": completed, "expected_bytes": size}), flush=True)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def acquire(destination: Path, offline: bool, xet: bool = False, *,
            repository: str = REPOSITORY, revision: str = REVISION,
            files: list[tuple[str, int, str]] = FILES) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, size, checksum in files:
        target = destination / name
        if matches(target, size, checksum):
            print(json.dumps({"artifact": name, "state": "verified"}), flush=True)
            continue
        if offline:
            raise ValueError("artifact_unavailable")
        partial = target.with_suffix(target.suffix + ".partial")
        print(json.dumps({"artifact": name, "state": "downloading", "bytes": size}), flush=True)
        if xet and size >= 128 * 1024**2:
            # Optional fast transport from Lalamo's immutable uv.lock. The
            # checkpoint is still accepted only by the pinned size and SHA-256.
            os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
            os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
            os.environ["HF_HUB_VERBOSITY"] = "error"
            with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                from huggingface_hub import hf_hub_download
                hf_hub_download(repository, name, revision=revision, local_dir=destination, token=False)
            if not matches(target, size, checksum):
                raise ValueError("artifact_checksum_mismatch")
            partial.unlink(missing_ok=True)
            print(json.dumps({"artifact": name, "state": "verified"}), flush=True)
            continue
        request = urllib.request.Request(
            f"https://huggingface.co/{repository}/resolve/{revision}/{name}",
            headers={"User-Agent": "meecp-pinned-model-acquisition/1"},
        )
        try:
            if size >= 128 * 1024**2:
                ranged_download(request.full_url, partial, size)
            else:
                with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as stream:
                    total = 0
                    while block := response.read(8 * 1024**2):
                        total += len(block)
                        if total > size:
                            raise ValueError("artifact_size_mismatch")
                        stream.write(block)
            if not matches(partial, size, checksum):
                raise ValueError("artifact_checksum_mismatch")
            os.replace(partial, target)
        except Exception as error:
            if isinstance(error, ValueError) and error.args == ("artifact_checksum_mismatch",):
                partial.unlink(missing_ok=True)
            raise
        print(json.dumps({"artifact": name, "state": "verified"}), flush=True)


def provenance() -> dict:
    return {
        "schema_version": 1,
        "source": {
            "repository": REPOSITORY,
            "revision": REVISION,
            "license": "Apache-2.0",
            "url": f"https://huggingface.co/{REPOSITORY}/tree/{REVISION}",
            "format": "merged BF16 safetensors, Qwen3.5-4B",
            "files": [{"name": n, "bytes": s, "sha256": h} for n, s, h in FILES],
        },
        "llama_reference": {
            "repository": "alibiserikbay/JevK5-GGUF",
            "revision": "ec67b0bfce5119a8b11a2cdb430bb43e3fa3e82a",
            "file": "jevk5-4b-v0.3-Q8_0.gguf",
            "bytes": 4482402720,
            "sha256": "aea433883bc7ed399f2fbd539e53d2eac7caf71a946fe6650995a413979d4a30",
            "license": "Apache-2.0",
        },
        "calibration": {"temperature": 1.22, "knockout_temperature": 0.93},
        "conversion": {"status": "pending"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "models/uzu-jevk5/source")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--xet", action="store_true", help="Use the pinned Lalamo environment's HF/Xet transport")
    args = parser.parse_args()
    try:
        acquire(args.destination, args.offline, args.xet)
        calibration = json.loads((args.destination / "jevk5_config.json").read_text())
        if calibration != {"temperature": 1.22, "knockout_temperature": 0.93}:
            raise ValueError("calibration_mismatch")
        reference = ROOT / "models/jevk5-4b-v0.3-Q8_0.gguf"
        reference_record = provenance()["llama_reference"]
        acquire(reference.parent, args.offline, args.xet,
                repository=reference_record["repository"], revision=reference_record["revision"],
                files=[(reference_record["file"], reference_record["bytes"], reference_record["sha256"])])
        if not matches(reference, reference_record["bytes"], reference_record["sha256"]):
            raise ValueError("llama_artifact_mismatch")
        manifest = ROOT / "experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        record = provenance()
        if manifest.exists():
            record["conversion"] = json.loads(manifest.read_text()).get("conversion", record["conversion"])
        manifest.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"state": "complete", "verified_files": len(FILES) + 1}), flush=True)
        return 0
    except Exception as error:
        code = error.args[0] if isinstance(error, ValueError) and error.args else "acquisition_failed"
        if code not in {"artifact_unavailable", "artifact_size_mismatch", "artifact_checksum_mismatch", "artifact_range_failed",
                        "calibration_mismatch", "llama_artifact_mismatch"}:
            code = "acquisition_failed"
        print(json.dumps({"state": "failed", "code": code}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
