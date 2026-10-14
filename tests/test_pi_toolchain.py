from __future__ import annotations

from pathlib import Path

import pytest
from meecp import pi_toolchain
from meecp.pi_toolchain import ToolchainManager
from meecp.pi_tools import PiToolError


class FakeBroker:
    def __init__(self, root: Path):
        self.workspace = root / "workspace"
        self.home = self.workspace / ".home"
        self.temporary = self.workspace / ".tmp"
        self.workspace.mkdir()
        self.home.mkdir()
        self.temporary.mkdir()
        self.env = {}
        self.runs = []
        self.writes = []
        self.compile_results = []

    def _arguments(self, value, required, optional):
        if not isinstance(value, dict) or set(value) - set(optional) or set(required) - set(value):
            raise PiToolError("invalid_arguments")

    def _write(self, path, content):
        self.writes.append(path)
        target = self.workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    async def _run(self, args):
        self.runs.append(args)
        target = Path(args["argv"][-1])
        if self.compile_results:
            success = self.compile_results.pop(0)
        else:
            success = True
        if success:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic-air")
        return {"success": success, "output": ""}


def _tree(tmp_path, *, full=True, metal=True):
    developer = tmp_path / ("Xcode.app/Contents/Developer" if full else "CommandLineTools")
    (developer / "usr/bin").mkdir(parents=True)
    (developer / "SDKs/macosx.sdk").mkdir(parents=True)
    (developer / "usr/bin/clang").write_bytes(b"clang")
    (developer / "usr/bin/clang").chmod(0o700)
    if full:
        (developer / "usr/bin/xcodebuild").write_bytes(b"xcodebuild")
        (developer / "usr/bin/xcodebuild").chmod(0o700)
    if metal:
        (developer / "Toolchains/Metal.xctoolchain/usr/bin").mkdir(parents=True)
        metal_path = developer / "Toolchains/Metal.xctoolchain/usr/bin/metal"
        metal_path.write_bytes(b"metal")
        metal_path.chmod(0o700)
    return developer


def _patch_trust(monkeypatch):
    def trusted(value, *, directory=False):
        path = Path(value).resolve(strict=True)
        if directory and not path.is_dir():
            raise PiToolError("developer_tools_untrusted")
        if not directory and (not path.is_file() or not path.stat().st_mode & 0o111):
            raise PiToolError("developer_tools_untrusted")
        return path

    monkeypatch.setattr(pi_toolchain, "_trusted_path", trusted)


def _runner(broker, developers, *, download_code=0, compiler_failures=0):
    calls = []
    remaining_failures = [compiler_failures]

    async def run(argv, environment, timeout=15):
        calls.append((argv, dict(environment), timeout))
        if argv[0] == "/usr/bin/codesign":
            return 0, b""
        if argv[1:3] == ["--print-path"]:
            return 0, (str(developers[0]) + "\n").encode()
        if argv[1:3] == ["--find", "clang"]:
            dev = Path(environment["DEVELOPER_DIR"])
            return 0, str(dev / "usr/bin/clang").encode()
        if argv[1:3] == ["--find", "metal"]:
            dev = Path(environment["DEVELOPER_DIR"])
            metal = dev / "Toolchains/Metal.xctoolchain/usr/bin/metal"
            return (0, str(metal).encode()) if metal.is_file() else (1, b"")
        if argv[1:4] == ["--sdk", "macosx", "--show-sdk-path"]:
            dev = Path(environment["DEVELOPER_DIR"])
            return 0, str(dev / "SDKs/macosx.sdk").encode()
        if argv[1:2] == ["-version"]:
            return 0, b"Xcode 26.4.1\nBuild version 17E202\n"
        if argv[1:3] == ["-downloadComponent", "MetalToolchain"]:
            return download_code, b""
        return 1, b""

    return run, calls, remaining_failures


@pytest.mark.asyncio
async def test_selects_full_xcode_when_command_line_tools_lacks_metal(tmp_path, monkeypatch):
    _patch_trust(monkeypatch)
    clt = _tree(tmp_path, full=False, metal=False)
    full = _tree(tmp_path, full=True, metal=True)
    broker = FakeBroker(tmp_path)
    runner, calls, _ = _runner(broker, [clt, full])
    manager = ToolchainManager(broker, runner=runner, candidates=[clt, full])

    result = await manager.ensure({"required": False})

    assert result["available"] is True and result["full_xcode"] is True
    assert result["developer_dir"] == str(full)
    assert broker.env["DEVELOPER_DIR"] == str(full)
    assert not any(call[0][1:3] == ["-downloadComponent", "MetalToolchain"] for call in calls)


@pytest.mark.asyncio
async def test_optional_missing_toolchain_does_not_download_or_select_globally(tmp_path, monkeypatch):
    _patch_trust(monkeypatch)
    clt = _tree(tmp_path, full=False, metal=False)
    broker = FakeBroker(tmp_path)
    runner, calls, _ = _runner(broker, [clt])
    manager = ToolchainManager(broker, runner=runner, candidates=[clt])

    result = await manager.ensure({"required": False})

    assert result["status"] == "missing" and result["available"] is False
    assert not any(call[0][1:2] == ["-downloadComponent"] for call in calls)
    assert not any(call[0][1:3] == ["--print-path"] for call in calls)


async def test_optional_missing_prefers_ready_full_xcode_acquisition_over_clt(tmp_path, monkeypatch):
    _patch_trust(monkeypatch)
    clt = _tree(tmp_path, full=False, metal=False)
    full = _tree(tmp_path, full=True, metal=False)
    broker = FakeBroker(tmp_path)
    runner, calls, _ = _runner(broker, [clt, full])
    manager = ToolchainManager(broker, runner=runner, candidates=[clt, full])

    result = await manager.ensure({"required": False})

    assert not result["available"] and result["developer_dir"] == str(full)
    assert result["first_launch_ready"] and result["acquisition_ready"]
    assert len(result["readiness_fingerprint"]) == 64
    assert broker.env["DEVELOPER_DIR"] == str(full)
    assert not any(call[0][1:2] == ["-downloadComponent"] for call in calls)


@pytest.mark.parametrize("unavailable", ["clang", "sdk", "version"])
async def test_signed_installed_xcode_first_launch_gap_requests_open_not_reinstall(tmp_path, monkeypatch, unavailable):
    _patch_trust(monkeypatch)
    full = _tree(tmp_path, full=True, metal=False)
    broker = FakeBroker(tmp_path)
    runner, calls, _ = _runner(broker, [full])

    async def first_launch(argv, environment, timeout=15):
        if (
            (unavailable == "clang" and argv[1:3] == ["--find", "clang"])
            or (unavailable == "sdk" and argv[1:4] == ["--sdk", "macosx", "--show-sdk-path"])
            or (unavailable == "version" and argv[1:2] == ["-version"])
        ):
            calls.append((argv, dict(environment), timeout))
            return 1, b""
        return await runner(argv, environment, timeout)

    manager = ToolchainManager(broker, runner=first_launch, candidates=[full])
    result = await manager.ensure({"required": True})

    assert result["status"] == "action_required" and result["full_xcode"]
    assert result["apple_signature_verified"] and not result["first_launch_ready"]
    assert not result["acquisition_ready"]
    assert result["action_required"]["kind"] == "open_xcode"
    assert result["action_required"]["application_path"] == str(full.parent.parent)
    assert "None" not in result.values()
    assert not broker.runs
    assert not any(call[0][1:2] == ["-downloadComponent"] for call in calls)
    assert not any("-license" in call[0] or "-runFirstLaunch" in call[0] for call in calls)


@pytest.mark.asyncio
async def test_required_download_uses_selected_xcode_and_rechecks(tmp_path, monkeypatch):
    _patch_trust(monkeypatch)
    full = _tree(tmp_path, full=True, metal=True)
    broker = FakeBroker(tmp_path)
    broker.compile_results = [False, True]
    runner, calls, _ = _runner(broker, [full], download_code=0)
    manager = ToolchainManager(broker, runner=runner, candidates=[full])

    result = await manager.ensure({"required": True})

    assert result["available"] is True and result["acquired"] is True
    downloads = [item for item in calls if item[0][1:3] == ["-downloadComponent", "MetalToolchain"]]
    assert len(downloads) == 1
    assert downloads[0][1]["DEVELOPER_DIR"] == str(full)
    assert downloads[0][1]["HOME"] == str(Path.home())


@pytest.mark.asyncio
async def test_failed_download_returns_action_required_without_license_acceptance(tmp_path, monkeypatch):
    _patch_trust(monkeypatch)
    full = _tree(tmp_path, full=True, metal=True)
    broker = FakeBroker(tmp_path)
    broker.compile_results = [False, False]
    runner, calls, _ = _runner(broker, [full], download_code=1)
    manager = ToolchainManager(broker, runner=runner, candidates=[full])

    result = await manager.ensure({"required": True})

    assert result["status"] == "action_required"
    assert result["action_required"]["code"] == "xcode_components_or_license_required"
    assert result["continuation"] == "ensure_metal_toolchain"
    assert not any("license" in call[0] for call in calls)


@pytest.mark.asyncio
async def test_tool_routing_rejects_unknown_and_non_boolean_arguments(tmp_path):
    broker = FakeBroker(tmp_path)
    manager = ToolchainManager(broker, runner=lambda *args: None, candidates=[])
    with pytest.raises(PiToolError, match="^invalid_arguments$"):
        await manager.ensure({"required": 1})
    with pytest.raises(PiToolError, match="^invalid_arguments$"):
        await manager.ensure({"required": False, "unexpected": True})


def test_real_trust_rejects_writable_directory_and_validate_detects_replacement(tmp_path):
    writable = tmp_path / "writable"
    writable.mkdir(mode=0o777)
    writable.chmod(0o777)
    with pytest.raises(PiToolError, match="^developer_tools_untrusted$"):
        pi_toolchain._trusted_path(str(writable), directory=True)

    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "bin").mkdir()
    (root / "bin/tool").write_bytes(b"tool")
    (root / "bin/tool").chmod(0o700)
    directory = pi_toolchain._Directory.pin(root)
    root.rename(tmp_path / "old")
    root.mkdir(mode=0o700)
    with pytest.raises(PiToolError, match="^developer_tools_changed$"):
        directory.validate()


async def test_unsigned_candidate_and_agent_workspace_are_never_host_toolchains(tmp_path, monkeypatch):
    _patch_trust(monkeypatch)
    full = _tree(tmp_path, full=True, metal=True)
    broker = FakeBroker(tmp_path)
    calls = []

    async def unsigned(argv, env, timeout=15):
        calls.append(argv)
        return 1, b""

    manager = ToolchainManager(broker, runner=unsigned, candidates=[full])
    result = await manager.ensure({"required": False})
    assert not result["available"]
    assert len(calls) == 1 and calls[0][0] == "/usr/bin/codesign"
    assert not broker.runs
    inside = _tree(broker.workspace, full=True, metal=True)
    manager = ToolchainManager(broker, runner=unsigned, candidates=[inside])
    calls.clear()
    result = await manager.ensure({"required": True})
    assert not result["available"] and result["action_required"]["code"] == "full_xcode_required"
    assert calls == []
