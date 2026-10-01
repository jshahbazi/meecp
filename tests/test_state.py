import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from jevk5_app.state import PrivateState, verified_model


def test_owner_permissions_stable_token_and_exclusive_lock(tmp_path):
    directory = tmp_path / "state"
    first = PrivateState(directory)
    token = first.token
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "connection.json").stat().st_mode) == 0o600
    with pytest.raises(RuntimeError, match="already running"):
        PrivateState(directory)
    first.close()
    second = PrivateState(directory)
    assert second.token == token
    second.close()


def test_hash_verification_and_changed_model(tmp_path):
    resources = tmp_path / "resources"
    (resources / "models").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    content = b"synthetic model bytes"
    model = resources / "models" / "test.gguf"
    model.write_bytes(content)
    manifest = {"file": "test.gguf", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                "temperature": 1.22, "knockout_temperature": 0.93, "context_tokens": 8192}
    (resources / "model-manifest.json").write_text(json.dumps(manifest))
    verified_model(resources, state)
    assert json.loads((state / "model-verification.json").read_text())["sha256"] == manifest["sha256"]
    verified_model(resources, state)
    model.write_bytes(b"x" * len(content))
    with pytest.raises(ValueError, match="hash"):
        verified_model(resources, state)


def test_access_time_change_during_read_is_not_model_mutation(tmp_path, monkeypatch):
    resources = tmp_path / "resources"
    (resources / "models").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    content = b"synthetic model bytes"
    model = resources / "models" / "test.gguf"
    model.write_bytes(content)
    manifest = {"file": "test.gguf", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                "temperature": 1.22, "knockout_temperature": 0.93, "context_tokens": 8192}
    (resources / "model-manifest.json").write_text(json.dumps(manifest))
    original = Path.stat
    reads = 0

    def changed_access_time(path, *args, **kwargs):
        nonlocal reads
        result = original(path, *args, **kwargs)
        if path == model:
            reads += 1
            if reads == 2:
                fields = {field: getattr(result, field) for field in ["st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev"]}
                return SimpleNamespace(**fields, st_atime=result.st_atime + 100)
        return result

    monkeypatch.setattr(Path, "stat", changed_access_time)
    verified_model(resources, state)
    assert reads >= 2
