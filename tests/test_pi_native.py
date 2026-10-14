"""Managed Metal preparation admission, helper provenance, and owned cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from meecp.pi_native import parse_probe, verified_probe
from meecp.pi_tools import PiToolBroker, PiToolError
from meecp.processes import OwnedProcess, OwnershipLost, ProcessError
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
        for argv in (["/usr/bin/git", "--version"], ["git", "--version"], ["/bin/sh", "-c", "git --version"]):
            git = await broker.invoke("run_command", {"argv": argv})
            assert git["success"] and json.loads(git["output"])["stdout"].startswith("git version ")
        started = await broker.invoke("start_process", {"argv": ["/usr/bin/git", "--version"]})
        assert started["success"]
        identity = json.loads(started["output"])["process_id"]
        async with asyncio.timeout(5):
            while True:
                read = await broker.invoke("read_process", {"process_id": identity})
                value = json.loads(read["output"])
                if value["state"] == "exited":
                    break
                await asyncio.sleep(0.01)
        assert value["returncode"] == 0 and value["stdout"].startswith("git version ")
        assert (await broker.invoke("stop_process", {"process_id": identity}))["success"]
        assert not broker.owner.instances
        assert broker.capabilities()["developer_tools"]["git"] == result["command_tools"]["git"]["path"]
        assert capfd.readouterr() == ("", "")
    finally:
        await broker.close()


@native
async def test_fixed_owned_discovery_repins_replaced_toolchain_without_widening_generic_commands(
    tmp_path, group_reaper, monkeypatch, capfd
):
    from meecp.pi_toolchain import owned_host_command
    from test_pi_toolchain import FakeBroker, _patch_trust, _runner, _tree

    _patch_trust(monkeypatch)
    full = _tree(tmp_path / "public-xcode")
    broker = PiToolBroker(tmp_path / "workspace", group_reaper)
    manager = broker.toolchain_manager
    manager._candidates = [full]
    runner, _, _ = _runner(FakeBroker(tmp_path / "public-xcode"), [full])
    original_run = broker._run

    async def fixed_runner(argv, environment, timeout=15):
        # Real native ownership/admission surrounds the controlled Apple facts.
        await owned_host_command(broker, ["/usr/bin/true"], environment, timeout=timeout)
        return await runner(argv, environment, timeout)

    async def compile(args, **options):
        if Path(args["argv"][0]).name != "metal":
            return await original_run(args, **options)
        broker._check_roots()
        broker._write(".toolchain/probe.air", "fixed public synthetic compiled artifact")
        return {"success": True, "output": ""}

    manager._runner = fixed_runner
    broker._run = compile
    try:
        assert (await manager.ensure({"required": False}))["available"]
        old_identity = manager._reads[0].identity
        old = full.parent / "PreviousDeveloper"
        full.rename(old)
        shutil.copytree(old, full)

        assert await broker.invoke("run_command", {"argv": ["/usr/bin/true"]}) == {
            "success": False, "output": "developer_tools_changed",
        }
        result = await broker.invoke("ensure_metal_toolchain", {"required": False})
        assert result["success"] and json.loads(result["output"])["available"]
        assert manager._reads[0].identity != old_identity
        launch = broker.launch_spec(["/usr/bin/git", "--version"])
        assert launch[1][2] == str(full / "usr/bin/git")
        assert broker.capabilities()["developer_tools"]["git"] == str(full / "usr/bin/git")
        assert (await broker.invoke("run_command", {"argv": ["/usr/bin/true"]}))["success"]
        assert not broker.owner.instances
        assert capfd.readouterr() == ("", "")
    finally:
        await broker.close()


@pytest.mark.parametrize("guard", ["workspace", "host_root", "ownership"])
async def test_fixed_discovery_never_bypasses_workspace_runtime_or_ownership_guards(tmp_path, guard):
    from meecp.pi_toolchain import _Directory

    host = tmp_path / "host"
    host.mkdir(mode=0o700)
    developer = tmp_path / "developer"
    developer.mkdir(mode=0o700)
    broker = PiToolBroker(tmp_path / "workspace", Path("/unused-reaper"), read_roots=(host,))
    broker.toolchain_manager._reads = (_Directory.pin(developer),)
    developer.rename(tmp_path / "old-developer")
    developer.mkdir(mode=0o700)
    try:
        if guard == "ownership":
            broker.owner.instances["lost"] = SimpleNamespace(ownership_lost=True)
            with pytest.raises(OwnershipLost):
                broker._toolchain_discovery_admitted()
        else:
            root = broker.workspace if guard == "workspace" else host
            root.rename(tmp_path / (guard + "-old"))
            root.mkdir(mode=0o700)
            with pytest.raises(PiToolError, match="^root_changed$"):
                broker._toolchain_discovery_admitted()
    finally:
        broker.owner.instances.clear()
        await broker.close()


@pytest.mark.parametrize("outcome", ["failed", "successful", "timeout", "ownership_lost"])
async def test_compiler_probe_cleanup_cannot_follow_background_replaced_directory(tmp_path, monkeypatch, outcome):
    from test_pi_toolchain import FakeBroker, _patch_trust, _runner, _tree

    _patch_trust(monkeypatch)
    full = _tree(tmp_path / "public-xcode")
    broker = PiToolBroker(tmp_path / "workspace", Path("/unused-reaper"))
    manager = broker.toolchain_manager
    manager._candidates = [full]
    manager._runner, _, _ = _runner(FakeBroker(tmp_path / "public-xcode"), [full])
    outside = tmp_path / "unselected"
    outside.mkdir(mode=0o700)
    for name in ("probe.metal", "probe.air"):
        (outside / name).write_text("preserved outside synthetic sentinel")

    async def replace_directory(args):
        # An owned background process can rename/link within its workspace.
        directory = broker.workspace / ".toolchain"
        directory.rename(broker.workspace / ".toolchain-original")
        directory.symlink_to(outside, target_is_directory=True)
        if outcome == "timeout":
            raise PiToolError("command_timeout")
        if outcome == "ownership_lost":
            raise OwnershipLost()
        return {"success": outcome == "successful", "output": ""}

    broker._run = replace_directory
    try:
        if outcome == "ownership_lost":
            with pytest.raises(OwnershipLost):
                await manager.ensure({"required": False})
        else:
            result = await manager.ensure({"required": False})
            assert not result["available"]
        for name in ("probe.metal", "probe.air"):
            assert (outside / name).read_text() == "preserved outside synthetic sentinel"
        assert not (broker.workspace / ".toolchain-original/probe.metal").exists()
    finally:
        await broker.close()


@native
async def test_prebuilt_probe_computes_without_offline_compiler_and_reaps(
    tmp_path, group_reaper, metal_device, compiled_probe, capfd
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


@pytest.mark.parametrize("device,residency", [(False, False), (True, False), (True, True)])
async def test_preflight_reports_missing_device_or_failed_compute_without_claiming_readiness(tmp_path, device, residency):
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    _, receipt = make_receipt(root)
    broker = PiToolBroker(tmp_path / "workspace", Path("/unused"), read_roots=(root,))
    broker.host_root = root

    async def ensure(args):
        assert args == {"required": False}
        return {"available": False, "status": "missing", "required": False}

    async def runner(args):
        return {"success": False, "output": json.dumps({"returncode": 1, "stdout": json.dumps({
            "schema_version": 1, "device_available": device, "residency_available": residency,
            "compute_passed": False})})}

    broker.toolchain_manager.ensure = ensure
    broker.native_runner = runner
    try:
        report = await broker.preflight()
        assert report["metal_runtime"] == {"status": "failed", "device_available": device,
            "residency_available": residency, "compute_passed": False,
            "helper_sha256": receipt["helper_sha256"], "helper_source_sha256": receipt["helper_source_sha256"]}
        assert report["metal_compiler"]["available"] is False and not broker.owner.instances
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
