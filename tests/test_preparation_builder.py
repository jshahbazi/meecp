"""The installed builder has an exact private closure and no process authority."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile

import pytest

from meecp.contracts import validate_package
from meecp.preparation_builder import (
    CommandSpec, PrivateToolchain, _dependency_notices, _wheel_requests,
    load_private_toolchain, prepare_bound,
)
from meecp import preparation_builder
from meecp.preparation_contracts import PreparationError
from test_meecp_preparations import descriptor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_preparation_toolchain import refresh_toolchain_manifest  # noqa: E402
import build_preparation_toolchain as toolchain_builder  # noqa: E402

WHEEL = b"Synthetic locked wheel; controlled runner fixture, never executed.\n"
WHEEL_DIGEST = hashlib.sha256(WHEEL).hexdigest()
MARKERS = {"implementation_name": "cpython", "implementation_version": "3.12.13", "os_name": "posix",
           "platform_machine": "arm64", "platform_release": "25.0.0", "platform_system": "Darwin",
           "platform_version": "Synthetic macOS", "python_full_version": "3.12.13",
           "platform_python_implementation": "CPython", "python_version": "3.12", "sys_platform": "darwin"}


@pytest.fixture(autouse=True)
def synthetic_license_pins(monkeypatch):
    # These static fixtures never distribute uv. Production pins remain intact
    # outside the individual test's explicitly synthetic retained texts.
    monkeypatch.setattr(preparation_builder, "UV_LICENSE_SHA256", {
        "licenses/uv-LICENSE-APACHE": hashlib.sha256(b"Synthetic Apache fixture license\n").hexdigest(),
        "licenses/uv-LICENSE-MIT": hashlib.sha256(b"Synthetic MIT fixture license\n").hexdigest(),
    })


def lock_text(*, url="https://wheels.example.invalid/mcp-1.30.0-py3-none-any.whl", size=len(WHEEL), hashed=WHEEL_DIGEST):
    return ("version = 1\nrevision = 3\n[[package]]\nname = 'mcp'\nversion = '1.30.0'\n"
            "source = { registry = 'https://wheels.example.invalid/simple' }\n"
            "wheels = [{ url = '" + url + "', hash = 'sha256:" + hashed + "', size = " + str(size) + " }]\n")


def toolchain(host: Path) -> PrivateToolchain:
    root = host / "preparation-toolchain"
    files = {"bin/uv": b"Synthetic private uv; callbacks own all execution.\n",
             "python/bin/python3.12": b"Synthetic private CPython; never run.\n",
             "python/lib/python3.12/LICENSE.txt": b"Synthetic PSF fixture license\n",
             "licenses/uv-LICENSE-APACHE": b"Synthetic Apache fixture license\n",
             "licenses/uv-LICENSE-MIT": b"Synthetic MIT fixture license\n", "licenses/NOTICE.md": b"Synthetic notice\n"}
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o700 if "/bin/" in "/" + relative else 0o600)
    for relative in ("prepare.py", "LICENSE", "python-function-v1/adapter.py"):
        output = root / "templates" / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "templates" / relative, output)
    (root / "toolchain.json").write_text(json.dumps({"schema_version": 1, "platform": "aarch64-apple-darwin",
        "uv_version": "0.12.13", "python_version": "3.12.13", "mcp_version": "1.30.0", "artifacts": {},
        "origin": {"uv_archive": preparation_builder.UV_ARCHIVE_URL,
                   "uv_archive_sha256": preparation_builder.UV_ARCHIVE_SHA256,
                   "uv_executable_sha256": preparation_builder.UV_EXECUTABLE_SHA256, "python": "Synthetic runtime"},
        "licenses": ["licenses/uv-LICENSE-APACHE", "licenses/uv-LICENSE-MIT", "licenses/NOTICE.md"]}))
    refresh_toolchain_manifest(root)
    return load_private_toolchain(host)


def draft_and_descriptor(tmp_path):
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "validator.py").write_text("raise RuntimeError('selected source imported during build')\n"
                                         "def validate_json(document): return {'valid': True}\n")
    (source / "LICENSE").write_text("Synthetic source license\n")
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\ndependencies=['mcp==1.30.0']\n")
    (source / "uv.lock").write_text(lock_text())
    records = [{"path": path.name, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
               for path in sorted(source.iterdir())]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    draft = {"draft_id": "synthetic-builder-draft", "revision": 2,
             "source": {"kind": "local_folder", "revision": "sha256:" + digest, "digest": digest,
                        "snapshot_path": str(source), "files": records}, "requirements": {}}
    value = descriptor({"recipe_id": "test.builder", "version": "1.0.0", "digest": "a" * 64},
                       hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest())
    return draft, value


def stub_runner(commands, *, license_present=True):
    def run(command):
        assert isinstance(command, CommandSpec)
        commands.append(command)
        assert Path(command.argv[0]).is_absolute()
        assert command.network_hosts == ()
        assert "UV_CACHE_DIR" in command.env and "UV_NO_CONFIG" in command.env
        assert command.env["UV_PYTHON_DOWNLOADS"] == "never"
        assert set(command.env) == {"PATH", "HOME", "TMPDIR", "UV_CACHE_DIR", "UV_PYTHON", "UV_PYTHON_DOWNLOADS",
                                   "UV_NO_CONFIG", "UV_NO_ENV_FILE", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE",
                                   "PYTHONSAFEPATH", "LC_ALL", "LANG"}
        if command.operation == "private_uv_version":
            return "uv 0.12.13 (synthetic callback)"
        if command.operation == "private_python_metadata":
            return json.dumps({"markers": MARKERS, "macos_version": [26, 0]})
        if command.operation == "dependency_export":
            assert "--frozen" in command.argv and "--offline" in command.argv
            assert "--locked" not in command.argv
            destination = Path(command.argv[command.argv.index("--output-file") + 1])
            destination.write_text("mcp==1.30.0 " + "\\" + "\n    --hash=sha256:" + WHEEL_DIGEST + "\n")
            return ""
        if command.operation == "dependency_install":
            assert all(flag in command.argv for flag in ("--offline", "--no-index", "--find-links",
                       "--only-binary", "--require-hashes", "--no-deps", "--link-mode"))
            assert command.argv[command.argv.index("--link-mode") + 1] == "copy"
            site = Path(command.argv[command.argv.index("--target") + 1])
            distribution = site / "mcp-1.30.0.dist-info"
            distribution.mkdir(parents=True)
            (distribution / "METADATA").write_text("Metadata-Version: 2.4\nName: mcp\nVersion: 1.30.0\nLicense-Expression: MIT\n")
            if license_present:
                (distribution / "LICENSE").write_text("Synthetic MIT license text\n")
            (site / "mcp.py").write_text("raise RuntimeError('dependency imported during build')\n")
            return ""
        if command.operation == "private_python_version":
            return "Python 3.12.13"
        pytest.fail("unexpected command operation")
    return run


def acquire(requests, destination, check_canceled):
    assert len(requests) == 1 and requests[0].sha256 == WHEEL_DIGEST
    check_canceled()
    (destination / requests[0].filename).write_bytes(WHEEL)


def test_private_builder_uses_only_injected_launches_frozen_wheels_and_retained_licenses(tmp_path, monkeypatch):
    private = toolchain(tmp_path / "relocated app Resources/host")
    draft, value = draft_and_descriptor(tmp_path)
    value["lifecycle"] = {"max_request_bytes": 24576, "max_sessions": 3, "idle_timeout_seconds": 23,
                          "operation_timeout_seconds": 17, "startup_timeout_seconds": 19, "stop_grace_seconds": 4}
    calls = []
    monkeypatch.setenv("PRIVATE_LEAK_SENTINEL", "ambient credential-like private value")
    def forbid(*args, **kwargs):
        pytest.fail("builder must never launch a process directly")
    monkeypatch.setattr("subprocess.run", forbid)
    monkeypatch.setattr("subprocess.Popen", forbid)
    package = prepare_bound(value, draft, private, tmp_path / "candidate", stub_runner(calls), lambda: None,
                            acquire_wheels=acquire)
    assert [item.operation for item in calls] == ["private_uv_version", "dependency_export", "private_python_metadata",
                                                 "dependency_install", "private_python_version"]
    assert "PRIVATE_LEAK_SENTINEL" not in json.dumps([item.env for item in calls])
    assert validate_package(package.root).digest == package.digest
    assert package.manifest.lifecycle.model_dump() == value["lifecycle"]
    notices = json.loads((package.root / "dependency-notices.json").read_bytes())
    assert notices["distributions"][0]["name"] == "mcp"
    assert notices["distributions"][0]["license_files"][0]["path"] == "mcp-1.30.0.dist-info/LICENSE"
    origin = json.loads((package.root / "python-runtime-origin.json").read_bytes())
    assert origin["toolchain_digest"] == private.identity
    assert load_private_toolchain(private.root.parent).identity == private.identity
    assert not list(private.root.rglob("__pycache__"))
    assert all(Path(item.env["UV_CACHE_DIR"]).is_relative_to(tmp_path / "candidate-builder") for item in calls)
    shutil.rmtree(Path(draft["source"]["snapshot_path"]))
    assert validate_package(package.root).digest == package.digest


@pytest.mark.parametrize("kind", ["changed", "extra", "missing", "symlink", "hardlink"])
def test_private_toolchain_rejects_changed_or_open_artifact_closure(tmp_path, kind):
    private = toolchain(tmp_path / "host")
    binary = private.uv
    if kind == "changed":
        binary.write_bytes(b"changed")
    elif kind == "extra":
        (private.root / "unrecorded.py").write_text("unrecorded")
    elif kind == "missing":
        binary.unlink()
    elif kind == "symlink":
        binary.unlink()
        binary.symlink_to(private.root / "python/bin/python3.12")
    else:
        import os
        os.link(binary, tmp_path / "outside-link")
    with pytest.raises(PreparationError):
        load_private_toolchain(private.root.parent)


def test_builder_requires_controlled_wheel_acquisition(tmp_path):
    private = toolchain(tmp_path / "host")
    draft, value = draft_and_descriptor(tmp_path)
    calls = []
    with pytest.raises(PreparationError, match="controlled_dependency_acquisition_required"):
        prepare_bound(value, draft, private, tmp_path / "candidate", stub_runner(calls), lambda: None)
    assert [item.operation for item in calls] == ["private_uv_version", "dependency_export"]


@pytest.mark.parametrize("tamper", ["bytes", "extra", "symlink"])
def test_builder_independently_checks_acquired_wheel_identity(tmp_path, tamper):
    private = toolchain(tmp_path / "host")
    draft, value = draft_and_descriptor(tmp_path)
    def corrupt(requests, destination, check):
        acquire(requests, destination, check)
        path = destination / requests[0].filename
        if tamper == "bytes":
            path.write_bytes(b"changed bytes")
        elif tamper == "extra":
            (destination / "extra.whl").write_bytes(WHEEL)
        else:
            path.unlink()
            path.symlink_to(Path(draft["source"]["snapshot_path"]) / "LICENSE")
    calls = []
    with pytest.raises(PreparationError):
        prepare_bound(value, draft, private, tmp_path / "candidate", stub_runner(calls), lambda: None,
                      acquire_wheels=corrupt)
    assert "dependency_install" not in [item.operation for item in calls]


def test_builder_rejects_missing_dependency_license_text(tmp_path):
    private = toolchain(tmp_path / "host")
    draft, value = draft_and_descriptor(tmp_path)
    with pytest.raises(PreparationError, match="dependency_notice_required"):
        prepare_bound(value, draft, private, tmp_path / "candidate", stub_runner([], license_present=False),
                      lambda: None, acquire_wheels=acquire)


def test_runtime_copy_observes_cancellation_before_launch(tmp_path):
    private = toolchain(tmp_path / "host")
    draft, value = draft_and_descriptor(tmp_path)
    calls = []
    class Canceled(RuntimeError):
        pass
    def cancel():
        raise Canceled("synthetic cancellation")
    with pytest.raises(Canceled):
        prepare_bound(value, draft, private, tmp_path / "candidate", stub_runner(calls), cancel, acquire_wheels=acquire)
    assert not calls and not (tmp_path / "candidate").exists()


@pytest.mark.parametrize("url", ["http://wheels.example.invalid/mcp-1.30.0-py3-none-any.whl",
    "https://user:password@wheels.example.invalid/mcp-1.30.0-py3-none-any.whl",
    "https://wheels.example.invalid/mcp-1.30.0-py3-none-any.whl?token=private",
    "https://wheels.example.invalid/mcp-1.30.0-py3-none-any.whl#fragment",
    "file:///mcp-1.30.0-py3-none-any.whl"])
def test_wheel_selection_rejects_unadmitted_locators(tmp_path, url):
    lock = tmp_path / "uv.lock"
    lock.write_text(lock_text(url=url))
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("mcp==1.30.0 --hash=sha256:" + WHEEL_DIGEST + "\n")
    with pytest.raises(PreparationError, match="unsupported_dependency_locator"):
        _wheel_requests(requirements, lock, {"markers": MARKERS, "macos_version": [26, 0]})


@pytest.mark.parametrize("filename", ["mcp-1.30.0-cp313-cp313-macosx_26_0_arm64.whl",
                                     "mcp-1.30.0-cp312-cp312-macosx_27_0_arm64.whl",
                                     "mcp-1.30.0-cp312-cp312-macosx_26_0_x86_64.whl"])
def test_wheel_selection_rejects_unverified_runtime_compatibility(tmp_path, filename):
    lock = tmp_path / "uv.lock"
    lock.write_text(lock_text(url="https://wheels.example.invalid/" + filename))
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("mcp==1.30.0 --hash=sha256:" + WHEEL_DIGEST + "\n")
    with pytest.raises(PreparationError, match="compatible_locked_wheel_required"):
        _wheel_requests(requirements, lock, {"markers": MARKERS, "macos_version": [26, 0]})


def test_requirements_inactive_markers_and_exact_hashes(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text(lock_text())
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("missing==1.0.0 ; sys_platform == 'win32' --hash=sha256:" + "b" * 64 + "\n"
                            "mcp==1.30.0 ; python_version == '3.12' --hash=sha256:" + WHEEL_DIGEST + "\n")
    requests = _wheel_requests(requirements, lock, {"markers": MARKERS, "macos_version": [26, 0]})
    assert [item.name for item in requests] == ["mcp"]
    requirements.write_text("mcp==1.30.0 --hash=sha256:" + "b" * 64 + "\n")
    with pytest.raises(PreparationError, match="invalid_locked_wheel"):
        _wheel_requests(requirements, lock, {"markers": MARKERS, "macos_version": [26, 0]})


def test_declarations_never_stand_in_for_retained_license_files(tmp_path):
    site = tmp_path / "site"
    distribution = site / "mcp-1.30.0.dist-info"
    distribution.mkdir(parents=True)
    (distribution / "METADATA").write_text("Metadata-Version: 2.4\nName: mcp\nVersion: 1.30.0\nLicense-File: LICENSE\n")
    with pytest.raises(PreparationError, match="dependency_notice_required"):
        _dependency_notices(site, tmp_path, lambda: None)


@pytest.mark.parametrize("change", ["boolean_version", "extra_field", "unrecorded_fifo", "license_origin"])
def test_toolchain_manifest_is_closed_and_exact(tmp_path, change):
    private = toolchain(tmp_path / "host")
    path = private.root / "toolchain.json"
    manifest = json.loads(path.read_bytes())
    if change == "boolean_version":
        manifest["schema_version"] = True
    elif change == "extra_field":
        manifest["shell_command"] = "must not execute"
    elif change == "unrecorded_fifo":
        import os
        os.mkfifo(private.root / "fifo")
    else:
        manifest["origin"]["uv_archive_sha256"] = "b" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(PreparationError):
        load_private_toolchain(private.root.parent)


def test_no_follow_toolchain_ancestry(tmp_path):
    private = toolchain(tmp_path / "host")
    alias = tmp_path / "alias"
    alias.symlink_to(private.root.parent, target_is_directory=True)
    with pytest.raises(PreparationError, match="private_preparation_toolchain_required"):
        load_private_toolchain(alias)


def bundled_inputs(tmp_path, monkeypatch):
    runtime = tmp_path / "standalone"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python3.12").write_bytes(b"Synthetic private interpreter")
    (runtime / "bin/python3.12").chmod(0o700)
    (runtime / "bin/python3").symlink_to("python3.12")
    (runtime / "lib/python3.12").mkdir(parents=True)
    (runtime / "lib/python3.12/LICENSE.txt").write_text("Synthetic PSF license")
    executable = b"Synthetic standalone uv executable"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo("uv-aarch64-apple-darwin/uv")
        member.size = len(executable)
        archive.addfile(member, io.BytesIO(executable))
    data = stream.getvalue()
    licenses = {"LICENSE-APACHE": b"Synthetic Apache fixture license\n", "LICENSE-MIT": b"Synthetic MIT fixture license\n"}
    monkeypatch.setattr(toolchain_builder, "UV_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(toolchain_builder, "UV_EXECUTABLE_SHA256", hashlib.sha256(executable).hexdigest())
    monkeypatch.setattr(toolchain_builder, "UV_LICENSES", {name: hashlib.sha256(value).hexdigest()
                                                         for name, value in licenses.items()})
    return runtime, data, licenses


def test_distribution_bundles_exact_uv_private_runtime_license_closure_and_refreshes_signed_bytes(tmp_path, monkeypatch):
    runtime, archive, licenses = bundled_inputs(tmp_path, monkeypatch)
    def forbid(*args, **kwargs):
        pytest.fail("explicit supplied build inputs must not fetch or execute anything")
    monkeypatch.setattr(toolchain_builder, "_fetch", forbid)
    monkeypatch.setattr("subprocess.run", forbid)
    bundled = toolchain_builder.bundle(tmp_path / "toolchain", runtime, archive=archive, license_bytes=licenses)
    manifest = json.loads((bundled / "toolchain.json").read_bytes())
    assert manifest["uv_version"] == "0.12.13"
    assert manifest["origin"]["uv_archive_sha256"] == hashlib.sha256(archive).hexdigest()
    assert not (bundled / "python/bin/python3").is_symlink()
    assert (bundled / "python/bin/python3").read_bytes() == (runtime / "bin/python3.12").read_bytes()
    for name in ("licenses/uv-LICENSE-APACHE", "licenses/uv-LICENSE-MIT", "licenses/NOTICE.md",
                 "python/lib/python3.12/LICENSE.txt", "templates/LICENSE", "templates/prepare.py"):
        assert name in manifest["artifacts"]
    binary = bundled / "bin/uv"
    binary.write_bytes(binary.read_bytes() + b" synthetic signature")
    refreshed = toolchain_builder.refresh_toolchain_manifest(bundled)
    assert refreshed["artifacts"]["bin/uv"]["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert refreshed["origin"] == manifest["origin"]


@pytest.mark.parametrize("change", ["archive", "license", "runtime_escape"])
def test_distribution_rejects_unverified_toolchain_inputs(tmp_path, monkeypatch, change):
    runtime, archive, licenses = bundled_inputs(tmp_path, monkeypatch)
    if change == "archive":
        archive += b"changed"
    elif change == "license":
        licenses["LICENSE-MIT"] = b"changed license"
    else:
        (runtime / "external").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        toolchain_builder.bundle(tmp_path / "toolchain", runtime, archive=archive, license_bytes=licenses)
