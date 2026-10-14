"""Managed Metal preparation admission, helper provenance, and owned cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest
from meecp.pi_native import parse_probe, verified_probe
from meecp.pi_tools import PiToolBroker, PiToolError
from meecp.processes import OwnedProcess, ProcessError
from test_preparation_processes import gone, wait_file
from test_preparation_processes import group_reaper as _group_reaper

MACOS = platform.system() == "Darwin" and platform.machine() == "arm64" and platform.mac_ver()[0].startswith("26.")
native = pytest.mark.skipif(not MACOS, reason="exact macOS 26 arm64 Metal confinement")


@pytest.fixture(scope="module")
def group_reaper(tmp_path_factory):
    return _group_reaper.__wrapped__(tmp_path_factory)


@pytest.fixture
async def broker(tmp_path):
    broker = PiToolBroker(tmp_path / "workspace", Path("/unused-reaper"))
    try:
        yield broker
    finally:
        await broker.close()


@pytest.mark.parametrize(
    "change,code",
    [
        ({"memory_mb": True}, "invalid_memory_limit"),
        ({"memory_mb": 0}, "invalid_memory_limit"),
        ({"memory_mb": 65537}, "invalid_memory_limit"),
        ({"timeout_seconds": True}, "invalid_timeout"),
        ({"timeout_seconds": 1801}, "invalid_timeout"),
        ({"require_metal_compiler": 1}, "invalid_arguments"),
        ({"network": 1}, "invalid_arguments"),
        ({"gpu": True}, "invalid_arguments"),
        ({"env": {"DEVELOPER_DIR": "/untrusted"}}, "invalid_environment"),
        ({"env": {"SDKROOT": "/untrusted"}}, "invalid_environment"),
        ({"env": {"TOOLCHAINS": "untrusted"}}, "invalid_environment"),
        ({"credentials": {"DEVELOPER_DIR": "opaque"}}, "invalid_credentials"),
        ({"cwd": "../outside"}, "invalid_path"),
    ],
)
async def test_native_request_rejected_before_host_admission(broker, change, code):
    calls = []

    async def runner(args):
        calls.append(args)
        return {"success": True, "output": "ready"}

    broker.native_runner = runner
    result = await broker.invoke("native_command", {"argv": ["/bin/true"], "memory_mb": 256, **change})
    assert result == {"success": False, "output": code} and calls == []


async def test_native_command_requires_host_and_generic_command_cannot_acquire_gpu(broker):
    args = {"argv": ["/bin/true"], "memory_mb": 256}
    assert await broker.invoke("native_command", args) == {"success": False, "output": "native_command_unavailable"}
    assert await broker.invoke("run_command", {**args, "gpu": True}) == {
        "success": False,
        "output": "invalid_arguments",
    }


async def test_compiler_is_requested_only_for_source_build_and_action_is_returned(broker, monkeypatch):
    calls = []

    async def ensure(args):
        calls.append(("compiler", args))
        return {
            "status": "action_required",
            "available": False,
            "required": True,
            "action_required": {"kind": "open_xcode"},
            "continuation": "ensure_metal_toolchain",
        }

    async def runner(args):
        calls.append(("runner", args))
        return {"success": True, "output": "ready"}

    broker.native_runner = runner
    monkeypatch.setattr(broker.toolchain_manager, "ensure", ensure)
    args = {"argv": ["/bin/true"], "memory_mb": 256, "require_metal_compiler": False}
    assert (await broker.invoke("native_command", args))["success"]
    assert calls == [("runner", args)]
    result = await broker.invoke("native_command", {**args, "require_metal_compiler": True})
    assert not result["success"] and json.loads(result["output"])["status"] == "action_required"
    assert calls[-1] == ("compiler", {"required": True}) and len(calls) == 2


def make_receipt(root, source=b"fixed-synthetic-source\n", binary=b"fixed-helper"):
    (root / "bin").mkdir(mode=0o700)
    helper = root / "bin/meecp-metal-probe"
    helper.write_bytes(binary)
    helper.chmod(0o700)
    (root / "native-metal-probe.m").write_bytes(source)
    value = {
        "schema_version": 1,
        "helper": "bin/meecp-metal-probe",
        "helper_sha256": hashlib.sha256(binary).hexdigest(),
        "helper_source_sha256": hashlib.sha256(source).hexdigest(),
    }
    (root / "native-metal-probe.json").write_text(json.dumps(value))
    return helper, value


@pytest.mark.parametrize("mutation", ["helper", "source", "receipt", "symlink", "writable"])
async def test_preflight_requires_exact_trusted_helper_and_source(tmp_path, mutation):
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    helper, receipt = make_receipt(root)
    broker = PiToolBroker(tmp_path / "workspace", Path("/unused"), read_roots=(root,))
    broker.host_root = root
    try:
        assert verified_probe(broker) == (helper, receipt)
        if mutation == "helper":
            helper.write_bytes(b"replacement")
        elif mutation == "source":
            (root / "native-metal-probe.m").write_text("replacement")
        elif mutation == "receipt":
            receipt["helper"] = "bin/other"
            (root / "native-metal-probe.json").write_text(json.dumps(receipt))
        elif mutation == "symlink":
            helper.rename(root / "bin/original")
            helper.symlink_to("original")
        else:
            helper.chmod(0o722)
        with pytest.raises(PiToolError):
            verified_probe(broker)
    finally:
        await broker.close()


@pytest.mark.parametrize(
    "probe,code",
    [
        ({"schema_version": True, "device_available": True, "residency_available": True, "compute_passed": True}, 0),
        ({"schema_version": 1, "device_available": False, "residency_available": True, "compute_passed": True}, 0),
        ({"schema_version": 1, "device_available": True, "residency_available": True, "compute_passed": False}, 0),
        (
            {
                "schema_version": 1,
                "device_available": True,
                "residency_available": True,
                "compute_passed": True,
                "raw_diagnostics": "private",
            },
            0,
        ),
    ],
)
def test_probe_protocol_rejects_invalid_or_extra_metadata(probe, code):
    result = {"success": code == 0, "output": json.dumps({"returncode": code, "stdout": json.dumps(probe)})}
    with pytest.raises(PiToolError, match="^metal_probe_invalid$"):
        parse_probe(result)


@pytest.fixture(scope="module")
def compiled_probe(tmp_path_factory):
    if not MACOS:
        pytest.skip("native Metal requires macOS 26 arm64")
    root = tmp_path_factory.mktemp("host-metal-probe")
    source = Path(__file__).resolve().parents[1] / "scripts/meecp-metal-probe.m"
    helper = root / "meecp-metal-probe"
    # CPU-only fixture build; every GPU execution below uses the native owner.
    subprocess.run(
        [
            "/usr/bin/clang",
            "-fobjc-arc",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-framework",
            "Foundation",
            "-framework",
            "Metal",
            str(source),
            "-o",
            str(helper),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return source.read_bytes(), helper.read_bytes()


@native
async def test_actual_toolchain_discovery_and_confined_compile_are_owned(tmp_path, group_reaper, capfd):
    broker = PiToolBroker(tmp_path / "workspace", group_reaper)
    try:
        result = await broker.toolchain_manager.ensure({"required": False})
        assert result["status"] in {"ready", "missing"}
        assert not result["acquired"] and not broker.owner.instances
        if not result["available"]:
            pytest.skip("installed Metal compiler unavailable; no acquisition during tests")
        assert result["developer_dir"] == broker.env["DEVELOPER_DIR"]
        assert result["apple_signature_verified"] is True
        assert len(result["metal_sha256"]) == 64 and len(result["clang_sha256"]) == 64
        assert broker.toolchain_manager.read_roots
        mount = Path("/private/var/run/com.apple.security.cryptexd/mnt")
        asset = next(
            (
                path
                for path in Path(result["metal_path"]).parents
                if path.parent == mount and path.name.startswith("com.apple.MobileAsset.MetalToolchain-")
            ),
            None,
        )
        if asset is not None:
            assert asset in broker.toolchain_manager.read_roots
        assert mount not in broker.toolchain_manager.read_roots
        assert not any("iokit-open" in value for value in broker.launch_spec(["/bin/true"])[1])
        assert not (broker.workspace / ".toolchain/probe.metal").exists()
        assert not (broker.workspace / ".toolchain/probe.air").exists()
        assert capfd.readouterr() == ("", "")
    finally:
        await broker.close()


@native
async def test_prebuilt_probe_computes_without_offline_compiler_and_reaps(
    tmp_path, group_reaper, compiled_probe, capfd
):
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    source, binary = compiled_probe
    helper, receipt = make_receipt(root, source, binary)
    broker = PiToolBroker(tmp_path / "workspace", group_reaper, read_roots=(root,))
    broker.host_root = root
    calls = []

    async def ensure(args):
        assert args == {"required": False}
        return {"available": False, "status": "missing", "required": False}

    async def runner(args):
        calls.append(args)
        result = await broker.native_run_impl(args)
        assert not broker.owner.instances
        return result

    broker.toolchain_manager.ensure = ensure
    broker.native_runner = runner
    try:
        report = await broker.preflight()
        assert report["metal_compiler"]["available"] is False
        runtime = report["metal_runtime"]
        assert runtime["status"] == "ready" and runtime["compute_passed"] is True
        assert runtime["device_available"] is True and runtime["residency_available"] is True
        assert runtime["helper_sha256"] == receipt["helper_sha256"]
        assert calls[0]["argv"] == [str(helper)] and calls[0]["require_metal_compiler"] is False
        assert "DEVELOPER_DIR" not in broker.env and not broker.owner.instances
        assert capfd.readouterr() == ("", "")
    finally:
        await broker.close()


@native
@pytest.mark.parametrize("termination", ["timeout", "cancel", "repeat_cancel"])
async def test_native_command_settles_descendants_before_callback_release(tmp_path, group_reaper, termination):
    python = Path(sys.executable).resolve()
    broker = PiToolBroker(tmp_path / "workspace", group_reaper, read_roots=(python.parents[1],))
    reserved = []

    async def runner(args):
        reserved.append(True)
        try:
            return await broker.native_run_impl(args)
        finally:
            assert not broker.owner.instances
            reserved.pop()

    broker.native_runner = runner
    code = (
        "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "child=os.fork(); Path('children').open('a').write(str(os.getpid())+' '); time.sleep(100)"
    )
    args = {
        "argv": [str(python), "-c", code],
        "memory_mb": 256,
        "timeout_seconds": 1 if termination == "timeout" else 20,
    }
    try:
        task = asyncio.create_task(broker.invoke("native_command", args))
        path = broker.workspace / "children"
        await wait_file(path)
        async with asyncio.timeout(5):
            while len(path.read_text().split()) < 2:
                await asyncio.sleep(0.01)
        pids = [int(value) for value in path.read_text().split()]
        assert reserved
        if termination != "timeout":
            task.cancel()
            if termination == "repeat_cancel":
                await asyncio.sleep(0.03)
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == {"success": False, "output": "command_timeout"}
        assert not reserved and not broker.owner.instances
        for pid in pids:
            await gone(pid)
    finally:
        await broker.close()


@native
async def test_native_cleanup_failure_keeps_settlement_false_and_live_owner(tmp_path, group_reaper, monkeypatch):
    broker = PiToolBroker(tmp_path / "workspace", group_reaper)
    original = OwnedProcess.stop
    async def incomplete_stop(process, timeout=None):
        raise ProcessError("reap_timeout")
    monkeypatch.setattr(OwnedProcess, "stop", incomplete_stop)
    try:
        with pytest.raises(ProcessError, match="^reap_timeout$"):
            await broker.native_run_impl({"argv": ["/bin/sleep", "30"], "memory_mb": 256, "timeout_seconds": 1})
        assert not broker.native_ownership_settled and broker.owner.instances
    finally:
        monkeypatch.setattr(OwnedProcess, "stop", original)
        await broker.close()
    assert not broker.owner.instances
