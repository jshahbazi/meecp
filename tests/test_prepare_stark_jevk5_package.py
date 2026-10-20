"""Seal complete synthetic packages and reject changed or escaping inputs."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from meecp.contracts import validate_package

REPOSITORY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stark_jevk5_prepare", REPOSITORY / "templates/jevk5-stark-v1/prepare.py")
assert spec is not None and spec.loader is not None
preparer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparer)


def write(path: Path, data: bytes | str, *, executable: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(0o755 if executable else 0o644)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    root, build, model, runtime = [tmp_path / name for name in ("source", "engine-build", "bf16", "host")]
    lock = {"revision": "a" * 40, "tree": "b" * 40, "protocol": {"worker_mode": "exact-token-scoring-cache-v1"}}
    write(root / "engines/stark.lock.json", json.dumps(lock))
    for name in ("jevk5_app", "meecp_jevk5", "meecp_stark"):
        write(root / "backend" / name / "__init__.py", "# sealed synthetic package\n")
    for name in ("__init__.py", "processes.py"):
        write(root / "backend/meecp" / name, "# sealed synthetic ownership source\n")
    write(root / "backend/meecp_local_transport.py", "# synthetic transport\n")
    write(root / "backend/meecp/legacy_jevk5_contract.json", (REPOSITORY / "backend/meecp/legacy_jevk5_contract.json").read_bytes())
    for name in ("contracts.py", "limits.py"):
        write(root / "backend/meecp" / name, "# sealed synthetic package validation source\n")
    for name in ("__init__.py", "prompt.py", "gguf.py", "LICENSE", "NOTICE"):
        write(root / "vendor/jevk5" / name, "synthetic pinned vendor file\n")
    for name in ("JEVK5_MODEL_LICENSE", "LALAMO_LICENSE"):
        write(root / "experiments/uzu-rust/jev-scoring/licenses" / name, "synthetic license\n")
    write(root / "uv.lock", "# locked synthetic dependency graph\n")
    write(root / "scripts/stark_dependency.py", "# sealed synthetic dependency verifier source\n")
    model.mkdir()
    for name in preparer.MODEL_FILES:
        write(model / name, "synthetic " + name + "\n")
    provenance = {"schema_version": 1, "source": {"revision": "c" * 40},
                  "calibration": {"temperature": 1.22, "knockout_temperature": 0.93},
                  "conversion": {"status": "complete", "dtype": "bfloat16", "quantization": "none",
                                 "files": [{"name": name, "bytes": (model / name).stat().st_size,
                                            "sha256": preparer.sha(model / name)} for name in preparer.MODEL_FILES]}}
    write(root / "experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json", json.dumps(provenance))
    write(build / "bin/stark-score", "synthetic pinned worker\n", executable=True)
    write(build / "source/LICENSE", "synthetic engine license\n")
    receipt = {"source": {"revision": lock["revision"], "tree": lock["tree"], "dirty": False, "development_only": False},
               "binaries": {"stark-score": preparer.sha(build / "bin/stark-score")}}
    write(build / "BUILD_RECEIPT.json", json.dumps(receipt))
    write(build / "DEPENDENCY_LOCK.json", json.dumps(lock))
    write(runtime / "python/bin/python3.12", "synthetic private Python\n", executable=True)
    write(runtime / "python/site-packages/dependency.py", "# pinned dependency\n")
    write(runtime / "python/licenses/LICENSE", "synthetic CPython license\n")
    write(runtime / "bin/meecp-reap", "synthetic native owned-process reaper\n", executable=True)
    runtime_receipt = {"python": "Python 3.12.13", "artifact_sha256": {"bin/meecp-reap": preparer.sha(runtime / "bin/meecp-reap")}}
    write(runtime / "build.json", json.dumps(runtime_receipt))
    monkeypatch.setattr(preparer, "ROOT", root)
    monkeypatch.setattr(preparer, "dependency_module", lambda: SimpleNamespace(verify_build=lambda *_: json.loads((build / "BUILD_RECEIPT.json").read_bytes())))
    monkeypatch.setattr(preparer, "validate_output", lambda destination: validate_package(destination).digest)
    def command(args, *, code, **kwargs):
        if args[:2] == ["/usr/bin/lipo", "-archs"]:
            return b"arm64\n"
        if args[:2] == ["/usr/bin/otool", "-L"]:
            return b"synthetic:\n /usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        if args[0] == "/usr/bin/cc":
            if "--version" in args:
                return b"Apple clang synthetic version\n"
            write(Path(args[-1]), "synthetic launcher\n", executable=True)
            return b""
        if args[0] == "/usr/bin/codesign":
            return b""
        if args[-1] == "--version":
            return b"Python 3.12.13\n"
        raise AssertionError("unexpected synthetic command")
    monkeypatch.setattr(preparer, "run", command)
    return SimpleNamespace(root=root, build=build, model=model, runtime=runtime, output=tmp_path / "prepared", command=command)


def prepare(value, **kwargs):
    return preparer.prepare(value.output, value.build, value.model, value.runtime, **kwargs)


def test_full_package_is_distinct_sealed_and_default_cache_off(inputs):
    result = prepare(inputs)
    package = validate_package(inputs.output)
    assert result["package_digest"] == package.digest
    assert package.manifest.package_id == "local.jevk5.stark"
    assert package.manifest.model.profile == "jevk5-stark-bf16-v1"
    assert package.manifest.model.parameters == {"temperature": 1.22, "knockout_temperature": 0.93, "consecutive_cache": False}
    assert package.manifest.model.engine_identity["name"] == "stark"
    assert package.manifest.model.sha256 == preparer.sha(inputs.model / "model.safetensors")
    assert [item.readiness.echo_generation for item in package.manifest.processes] == [True, True]
    assert package.manifest.resources.gpu and package.manifest.resources.memory_mb == 12288
    assert [tool.name for tool in package.manifest.tools] == ["jevk5_decide", "jevk5_status"]
    assert (inputs.output / "native/meecp-reap").read_bytes() == (inputs.runtime / "bin/meecp-reap").read_bytes()
    assert {item.name for item in (inputs.output / preparer.MODEL_DIRECTORY).iterdir()} == set(preparer.MODEL_FILES)
    assert not list(inputs.output.rglob("*.pyc"))
    assert not (inputs.output / "llama").exists()


def test_cache_requires_explicit_generator_option(inputs):
    result = prepare(inputs, consecutive_cache=True)
    assert result["consecutive_cache_enabled"] is True
    assert validate_package(inputs.output).manifest.model.parameters["consecutive_cache"] is True
    assert json.loads((inputs.output / "preparation-receipt.json").read_bytes())["consecutive_cache_enabled"] is True


@pytest.mark.parametrize("mutation", ["weights", "extra", "symlink"])
def test_model_modified_extra_or_symlink_rejected(inputs, mutation):
    if mutation == "extra":
        write(inputs.model / "extra.safetensors", "unexpected shard")
        code = "model_inventory_mismatch"
    elif mutation == "weights":
        write(inputs.model / "model.safetensors", "changed weights")
        code = "model_identity_mismatch"
    else:
        original = inputs.model / "config.json"
        target = inputs.model.parent / "outside.json"
        target.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(target)
        code = "model_identity_mismatch"
    with pytest.raises(preparer.PreparationError, match=code):
        prepare(inputs)
    assert not inputs.output.exists()


def test_development_engine_is_rejected(inputs):
    path = inputs.build / "BUILD_RECEIPT.json"
    receipt = json.loads(path.read_bytes())
    receipt["source"]["development_only"] = True
    path.write_text(json.dumps(receipt))
    with pytest.raises(preparer.PreparationError, match="engine_development_build_rejected"):
        prepare(inputs)


def test_host_reaper_must_match_existing_receipt(inputs):
    write(inputs.runtime / "bin/meecp-reap", "substituted reaper", executable=True)
    with pytest.raises(preparer.PreparationError, match="runtime_receipt_invalid"):
        prepare(inputs)


def test_runtime_internal_file_links_copy_regular_bytes(inputs):
    alias = inputs.runtime / "python/bin/python3"
    alias.symlink_to("python3.12")
    prepare(inputs)
    copied = inputs.output / "python/bin/python3"
    assert not copied.is_symlink() and copied.read_bytes() == alias.read_bytes()


def test_runtime_escape_is_rejected(inputs):
    outside = inputs.runtime.parent / "outside.py"
    write(outside, "outside runtime")
    (inputs.runtime / "python/site-packages/escape.py").symlink_to(outside)
    with pytest.raises(preparer.PreparationError, match="unsafe_path"):
        prepare(inputs)


@pytest.mark.parametrize("mutation", ["content", "new_source"])
def test_generation_input_mutation_removes_incomplete_output(inputs, monkeypatch, mutation):
    def command(args, **kwargs):
        result = inputs.command(args, **kwargs)
        if args[0] == "/usr/bin/cc" and "--version" not in args:
            source = inputs.root / "backend/meecp_stark" / ("__init__.py" if mutation == "content" else "extra.py")
            write(source, "# changed while preparing\n")
        return result
    monkeypatch.setattr(preparer, "run", command)
    with pytest.raises(preparer.PreparationError, match="input_changed"):
        prepare(inputs)
    assert not inputs.output.exists()


def test_output_inside_runtime_is_rejected(inputs):
    inputs.output = inputs.runtime / "prepared"
    with pytest.raises(preparer.PreparationError, match="unsafe_path"):
        prepare(inputs)


def test_metadata_changed_between_admission_and_seal_is_rejected(inputs, monkeypatch):
    original = preparer.inventory
    changed = False
    def inventory(files):
        nonlocal changed
        result = original(files)
        if "provenance/JEVK5_MODEL.json" in files and not changed:
            changed = True
            path = inputs.root / "experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json"
            value = json.loads(path.read_bytes())
            value["source"]["revision"] = "d" * 40
            path.write_text(json.dumps(value))
        return result
    monkeypatch.setattr(preparer, "inventory", inventory)
    with pytest.raises(preparer.PreparationError, match="input_changed"):
        prepare(inputs)
    assert not inputs.output.exists()


def test_private_runtime_probe_cannot_change_sealed_inputs(inputs, monkeypatch):
    def command(args, **kwargs):
        result = inputs.command(args, **kwargs)
        if args[-1] == "--version" and args[0] != "/usr/bin/cc":
            write(inputs.runtime / "python/bin/python3.12", "changed runtime", executable=True)
        return result
    monkeypatch.setattr(preparer, "run", command)
    with pytest.raises(preparer.PreparationError, match="input_changed"):
        prepare(inputs)
    assert not inputs.output.exists()


def test_raw_compiler_diagnostic_is_never_returned(inputs, monkeypatch, capsys):
    def command(args, **kwargs):
        if args[0] == "/usr/bin/cc":
            preparer.fail("compiler_failed")
        return inputs.command(args, **kwargs)
    monkeypatch.setattr(preparer, "run", command)
    monkeypatch.setattr(sys, "argv", ["prepare.py", "--build", str(inputs.build), "--model", str(inputs.model),
                                      "--runtime", str(inputs.runtime), "--output", str(inputs.output)])
    assert preparer.main() == 2
    assert json.loads(capsys.readouterr().out) == {"prepared": False, "error": "compiler_failed"}
    assert not inputs.output.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS launcher")
def test_native_launcher_routes_after_relocation_and_bounds_descriptor(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "bin").mkdir()
    compile_result = subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                                    str(REPOSITORY / "templates/jevk5-stark-v1/launcher.c"),
                                    "-o", str(original / "bin/jevk5-stark-engine")], capture_output=True)
    assert compile_result.returncode == 0
    shutil.copyfile(original / "bin/jevk5-stark-engine", original / "bin/jevk5-stark-adapter")
    (original / "bin/jevk5-stark-adapter").chmod(0o755)
    write(original / "python/bin/python3.12", f"#!{sys.executable}\nimport json, os, sys\n"
          "print(json.dumps({'args': sys.argv[1:], 'search': os.environ['PYTHONPATH'], 'usersite': os.environ['PYTHONNOUSERSITE']}))\n",
          executable=True)
    relocated = tmp_path / "relocated package with spaces"
    shutil.move(original, relocated)
    for name, module, role in (("jevk5-stark-engine", "meecp_stark", "engine"),
                              ("jevk5-stark-adapter", "meecp_jevk5", "adapter")):
        executable = relocated / "bin" / name
        result = subprocess.run([str(executable), "--meecp-config-fd", "3"], capture_output=True, check=True)
        value = json.loads(result.stdout)
        assert value["args"] == ["-B", "-P", "-m", module, role, "--meecp-config-fd", "3"]
        assert value["search"] == ":".join(str(relocated / part) for part in ("backend", "vendor", "python/site-packages"))
        assert value["usersite"] == "1"
        for arguments in ([], ["--meecp-config-fd", "2"], ["--meecp-config-fd", "3;bad"],
                          ["--meecp-config-fd", "99999999999"], ["--meecp-config-fd", "3", "extra"]):
            rejected = subprocess.run([str(executable), *arguments], capture_output=True)
            assert rejected.returncode == 2 and rejected.stdout == b"" and rejected.stderr == b""
