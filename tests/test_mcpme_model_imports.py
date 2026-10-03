from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import threading
from types import SimpleNamespace
import uuid

import pytest

from mcpme.imports import ImportError, ImportStore
from mcpme import model_intake
from test_mcpme_packages import edit_manifest, make_package


@pytest.fixture
def weights(tmp_path, monkeypatch):
    content = b"GGUF" + struct.pack("<IQQ", 3, 2, 3) + b"\x00\xffsynthetic tensor bytes"
    pin = {**model_intake.MODEL, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    monkeypatch.setattr(model_intake, "MODEL", pin)
    source = tmp_path / "raw.gguf"
    source.write_bytes(content)
    return source


@pytest.fixture
def engine(tmp_path):
    directory = tmp_path / "engine build"
    paths = ["llama/llama-server.real", "llama/plugins/libggml-metal.so", "llama/lib/libggml-base.dylib"]
    records = {}
    for relative in paths:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00synthetic native artifact " + relative.encode())
        path.chmod(0o755)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        records[relative] = {"sha256": sha, "source_sha256": sha}
    (directory / "licenses").mkdir()
    for filename in ("llama.cpp-LICENSE", "ggml-LICENSE"):
        (directory / "licenses" / filename).write_text("Synthetic license\n")
    (directory / "runtime-manifest.json").write_text(json.dumps({"llama": records}))
    return directory


def requirements():
    return {"engine_profile": model_intake.PROFILE, "profile": "model-service", "tools": ["jevk5_decide", "jevk5_status"]}


def test_raw_weights_and_engine_snapshot_without_execution_or_content_journal(tmp_path, weights, engine, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("intake must not run a selected engine/source")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(weights, name="Model", intent="Expose decisions", model_engine=engine)
        source = draft["source"]
        assert source["kind"] == "artifact_collection" and source["contract_version"] == 1
        assert source["digest"] == model_intake.canonical_digest(source["files"])
        assert source["revision"] == "sha256:" + source["digest"]
        assert {entry["role"] for entry in source["files"]} == {"model-weights", "runtime-file", "license", "receipt"}
        snapshot = Path(source["snapshot_path"])
        for record in source["files"]:
            copied = snapshot / record["path"]
            assert hashlib.sha256(copied.read_bytes()).hexdigest() == record["sha256"]
            assert stat.S_IMODE(copied.stat().st_mode) == (0o700 if record["executable"] else 0o600)
        assert b"synthetic tensor bytes" not in store.path.read_bytes()
        assert b"synthetic native artifact" not in store.path.read_bytes()
        revised = store.revise(draft["draft_id"], 1, requirements=requirements())
        assert revised["blockers"] == [{"code": "prepared_package_required", "field": "attachment"}]
        exported = store.export(draft["draft_id"], 2)
        assert "artifact_collection contract version 1" in exported["agent_instructions"]
        assert "prepare.py --setup-export" in exported["agent_instructions"]
        weights.unlink()
        assert (snapshot / "weights" / model_intake.MODEL["file"]).is_file()


def test_model_only_requires_explicit_profile_tools_and_engine_and_can_resume(tmp_path, weights, engine):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(weights, name="Model", intent="Expose decisions")
        assert {item["code"] for item in draft["blockers"]} == {"engine_artifacts_required", "model_profile_required",
                                                                   "model_service_required", "model_tools_required", "prepared_package_required"}
        first_identity = draft["source"]["revision"]
        weights.unlink()  # Engine selection uses the immutable saved weights.
        revised = store.add_engine(draft["draft_id"], 1, engine)
        assert revised["revision"] == 2 and revised["source"]["revision"] != first_identity
        assert revised["source"]["engine"]["build"] == model_intake.ENGINE_BUILD
        with pytest.raises(ImportError, match="stale_import_revision"):
            store.add_engine(draft["draft_id"], 1, engine)
    with ImportStore(tmp_path / "state") as resumed:
        assert resumed.inspect(draft["draft_id"])["source"] == revised["source"]


@pytest.mark.parametrize("change,code", [("version", "unsupported_model_format"), ("header", "unsupported_model_format"),
                                         ("metadata", "invalid_model_metadata"), ("bytes", "unsupported_model_artifact")])
def test_unknown_or_malformed_models_block_before_publication(tmp_path, weights, change, code):
    content = bytearray(weights.read_bytes())
    if change == "version":
        content[4:8] = struct.pack("<I", 2)
    elif change == "header":
        content[:4] = b"NOPE"
    elif change == "metadata":
        content[8:16] = struct.pack("<Q", 0)
    else:
        content[-1] ^= 1
    weights.write_bytes(content)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match=code):
            store.create(weights, name="Model")
        assert not store.list() and not list(store.staging.iterdir())


@pytest.mark.parametrize("unsafe", ["leaf_link", "ancestor_link", "hardlink", "fifo"])
def test_binary_intake_rejects_unsafe_sources(tmp_path, weights, unsafe):
    if unsafe == "leaf_link":
        selected = tmp_path / "linked.gguf"
        selected.symlink_to(weights)
    elif unsafe == "ancestor_link":
        link = tmp_path / "linked"
        link.symlink_to(tmp_path, target_is_directory=True)
        selected = link / weights.name
    elif unsafe == "hardlink":
        selected = tmp_path / "linked.gguf"
        os.link(weights, selected)
    else:
        selected = tmp_path / "pipe.gguf"
        os.mkfifo(selected)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises((ImportError, OSError), match="unsafe_source"):
            store.create(selected, name="Model")
        assert not store.list()


@pytest.mark.parametrize("damage,code", [("receipt", "engine_receipt_invalid"), ("hash", "engine_identity_mismatch"),
                                         ("traversal", "engine_receipt_invalid"), ("metal", "engine_build_unsupported"),
                                         ("license", "engine_license_required"), ("secret", "source_file_excluded")])
def test_engine_static_blockers(tmp_path, weights, engine, damage, code):
    receipt = engine / "runtime-manifest.json"
    content = json.loads(receipt.read_text())
    if damage == "receipt":
        receipt.write_text("{}")
    elif damage == "hash":
        (engine / "llama/llama-server.real").write_bytes(b"changed")
    elif damage == "traversal":
        content["llama"]["llama/../external"] = next(iter(content["llama"].values()))
        receipt.write_text(json.dumps(content))
    elif damage == "metal":
        del content["llama"]["llama/plugins/libggml-metal.so"]
        receipt.write_text(json.dumps(content))
    elif damage == "license":
        (engine / "licenses/ggml-LICENSE").unlink()
    else:
        (engine / "licenses/credentials.json").write_text("private")
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match=code):
            store.create(weights, name="Model", model_engine=engine)
        assert not store.list()


def test_large_model_copy_does_not_hold_sqlite_write_transaction(tmp_path, weights, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = model_intake.copy_artifact
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(model_intake, "copy_artifact", paused)
    with ImportStore(tmp_path / "state") as first, ImportStore(tmp_path / "state") as second:
        result = []
        worker = threading.Thread(target=lambda: result.append(first.create(weights, name="Model")))
        worker.start()
        assert entered.wait(10)
        assert not first._connection.in_transaction
        source = tmp_path / "source"
        source.mkdir()
        (source / "tool.py").write_text("pass\n")
        assert second.create(source, name="Independent")["revision"] == 1
        release.set()
        worker.join(10)
        assert len(result) == 1


def test_disk_headroom_fails_without_published_draft(tmp_path, weights, monkeypatch):
    monkeypatch.setattr(model_intake.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 1})())
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="insufficient_disk_space"):
            store.create(weights, name="Model")
        assert not store.list()


def test_text_source_limits_and_identity_remain_unchanged_with_binary_contract(tmp_path, monkeypatch):
    source = tmp_path / "text source"
    source.mkdir()
    (source / "tool.py").write_text("pass\n")
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source, name="Text")
        entries = [{"path": "tool.py", "sha256": hashlib.sha256(b"pass\n").hexdigest(), "size": 5}]
        assert draft["source"]["files"] == entries
        assert draft["source"]["revision"] == "sha256:" + model_intake.canonical_digest(entries)
        assert "contract_version" not in draft["source"]
        monkeypatch.setattr("mcpme.imports.MAX_SOURCE_FILE_BYTES", 4)
        with pytest.raises(ImportError, match="source_size_exceeded"):
            store.create(source, name="Limited")


def test_raw_weight_pin_matches_tracked_model_provenance():
    model = json.loads((Path(__file__).resolve().parents[1] / "model-manifest.json").read_bytes())
    assert model_intake.MODEL == {key: model[key] for key in model_intake.MODEL}


def test_carry_attachment_settings_revision_preserves_artifact_and_invalidates_revision(tmp_path):
    package = make_package(tmp_path / "package")
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(package, name="Prepared")
        path = Path(draft["attachment"]["path"])
        updated = store.revise(draft["draft_id"], 1, carry_attachment=True, name="Renamed", start=True,
                               requirements={"configuration": {}, "secret_references": {}})
        assert updated["revision"] == 2 and updated["attachment"] == draft["attachment"] and path.is_dir()
        assert updated["state"] == "ready_to_plan"
        with pytest.raises(ImportError, match="stale_import_revision"):
            store.revise(draft["draft_id"], 1, carry_attachment=True, start=False)
        with pytest.raises(ImportError, match="invalid_import_update"):
            store.revise(draft["draft_id"], 2, carry_attachment=True, intent="changed")
        with pytest.raises(ImportError, match="invalid_import_update"):
            store.revise(draft["draft_id"], 2, carry_attachment=True, document_role="instructions")
        with pytest.raises(ImportError, match="invalid_import_update"):
            store.revise(draft["draft_id"], 2, carry_attachment=True, requirements={"tools": ["other"]})
        (path / "LICENSE").write_text("changed")
        with pytest.raises((ImportError, ValueError)):
            store.revise(draft["draft_id"], 2, carry_attachment=True, start=False)


def test_carry_attachment_cannot_bypass_preparation_for_unattached_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "tool.py").write_text("pass\n")
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source, name="Unprepared", intent="Expose validator")
        with pytest.raises(ImportError, match="^invalid_import_update$"):
            store.revise(draft["draft_id"], 1, carry_attachment=True, name="Renamed",
                         requirements={"configuration": {}, "secret_references": {}})
        assert store.inspect(draft["draft_id"]) == draft


def test_carry_attachment_saves_partial_settings_and_retains_installation_blockers(tmp_path):
    package = make_package(tmp_path / "package")
    edit_manifest(package, configuration={"fields": {
        "greeting": {"type": "string", "required": True, "min_length": 1},
        "count": {"type": "integer", "required": True, "minimum": 0},
        "enabled": {"type": "boolean", "default": True}}}, access={"secrets": ["service_key", "other_key"]})
    reference = str(uuid.uuid4())
    state = tmp_path / "state"
    with ImportStore(state) as store:
        draft = store.create(package, name="Configured")
        partial = store.revise(draft["draft_id"], 1, carry_attachment=True, name="Saved partial", start=True,
            requirements={"configuration": {"count": 2, "enabled": False}, "secret_references": {"service_key": reference}})
        assert partial["attachment"] == draft["attachment"] and partial["source"] == draft["source"]
        assert partial["revision"] == 2 and partial["name"] == "Saved partial" and partial["start"] is True
        assert partial["state"] == "package_received"
        assert {item["code"] for item in partial["blockers"]} == {"configuration_required"}
        with pytest.raises(ImportError, match="^import_not_ready$"):
            store.validate_binding(draft["draft_id"], 2, draft["attachment"]["digest"])
        with pytest.raises(ImportError, match="^import_not_ready$"):
            store.submit(draft["draft_id"], 2, draft["attachment"]["digest"], str(uuid.uuid4()))
    with ImportStore(state) as store:
        assert store.inspect(draft["draft_id"]) == partial
        configured = store.revise(draft["draft_id"], 2, carry_attachment=True,
            requirements={"configuration": {"count": 2, "enabled": False, "greeting": "Hello"},
                          "secret_references": {"service_key": reference}})
        assert configured["state"] == "package_received"
        assert {item["code"] for item in configured["blockers"]} == {"secret_reference_unavailable"}
        with pytest.raises(ImportError, match="^import_not_ready$"):
            store.validate_binding(draft["draft_id"], 3, draft["attachment"]["digest"])
        complete = store.revise(draft["draft_id"], 3, carry_attachment=True,
            requirements={**configured["requirements"], "secret_references": {"service_key": reference, "other_key": str(uuid.uuid4())}})
        assert complete["revision"] == 4 and complete["attachment"] == draft["attachment"]
        assert complete["source"] == draft["source"] and complete["state"] == "ready_to_plan"
        assert store.validate_binding(draft["draft_id"], 4, draft["attachment"]["digest"]) == complete
        with pytest.raises(ImportError, match="^stale_import_revision$"):
            store.validate_binding(draft["draft_id"], 3, draft["attachment"]["digest"])


@pytest.mark.parametrize("requirements", [
    {"configuration": {"count": "2"}, "secret_references": {}},
    {"configuration": {"count": -1}, "secret_references": {}},
    {"configuration": {"unknown": True}, "secret_references": {}},
    {"configuration": {}, "secret_references": {"unknown": "00000000-0000-4000-8000-000000000001"}},
])
def test_carry_attachment_rejects_invalid_supplied_partial_choices(tmp_path, requirements):
    package = make_package(tmp_path / "package")
    edit_manifest(package, configuration={"fields": {"count": {"type": "integer", "required": True, "minimum": 0}}},
                  access={"secrets": ["service_key"]})
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(package, name="Configured")
        with pytest.raises(ImportError, match="^invalid_import_requirements$"):
            store.revise(draft["draft_id"], 1, carry_attachment=True, name="Invalid partial", requirements=requirements)
        assert store.inspect(draft["draft_id"]) == draft


def generator():
    path = Path(__file__).resolve().parents[1] / "templates/jevk5-llamacpp-v1/prepare.py"
    spec = importlib.util.spec_from_file_location("jevk5_preparation_template", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_consumes_exact_private_binary_export_and_rejects_tampering(tmp_path, weights, engine):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(weights, name="Model", intent="Decisions", model_engine=engine)
        revised = store.revise(draft["draft_id"], 1, requirements=requirements())
        exported = store.export(draft["draft_id"], revised["revision"])
        template = generator()
        template.exported_snapshot(exported, tmp_path / "agent input")
        output = tmp_path / "agent input/weights" / model_intake.MODEL["file"]
        assert output.read_bytes() == weights.read_bytes()
        altered = json.loads(json.dumps(exported))
        altered["source"]["files"][0]["sha256"] = "0" * 64
        with pytest.raises(ValueError, match="source_identity_mismatch"):
            template.exported_snapshot(altered, tmp_path / "changed input")
        snapshot = Path(exported["source"]["snapshot_path"])
        (snapshot / "engine/llama/llama-server.real").write_bytes(b"changed")
        with pytest.raises(ValueError, match="engine_identity_mismatch"):
            template.exported_snapshot(exported, tmp_path / "edited input")


@pytest.mark.parametrize("version", ["version: 0.5.0 (build 11146, commit 7fe450e19)",
                                     "version: 0.5.0 (build 11145, commit 7fe450e19)",
                                     "version: 0.5.0 (build 11146, commit 012345678)",
                                     "version: 0.6.0 (build 11146, commit 7fe450e19)"])
def test_generator_probes_actual_engine_build_only_under_explicit_preparation(engine, monkeypatch, version):
    template = generator()
    def metadata(*args):
        if args[0] == "/usr/bin/lipo":
            return "arm64"
        return "library:\n /usr/lib/libSystem.B.dylib (compatibility version 1.0.0)"
    monkeypatch.setattr(template, "run", metadata)
    calls = []
    def probe(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=version.encode(), stderr=b"")
    monkeypatch.setattr(template.subprocess, "run", probe)
    if version == "version: 0.5.0 (build 11146, commit 7fe450e19)":
        template.verify_engine(engine)
    else:
        with pytest.raises(ValueError, match="engine_build_unsupported"):
            template.verify_engine(engine)
    assert calls == [[str(engine / "llama/llama-server.real"), "--version"]]
