from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import platform
import sys
import tomllib

import pytest

from mcpme.pi_dependencies import (
    MARKER, MCP_VERSION, PYTHON_VERSION, _Installer, _profile, dependency_project, install_dependencies,
)
from mcpme.pi_tools import PiToolBroker
from mcpme.preparation_contracts import PreparationError
from test_preparation_processes import group_reaper


def project_fixture(broker, text='[project]\nname="pi-example"\nversion="1.0.0"\ndependencies=["anyio>=4"]\n'):
    project = broker.workspace / "app"
    project.mkdir(mode=0o700)
    (project / "pyproject.toml").write_text(text)
    return project


def test_static_project_ignores_hooks_sources_indexes_and_groups():
    data = b'''[project]
name = "pi-example"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["anyio>=4"]
[build-system]
requires = ["unsafe @ https://private.example/package.whl"]
build-backend = "selected_code"
[tool.uv.sources]
anyio = {git = "https://private.example/repo"}
[[tool.uv.index]]
url = "https://user:secret@private.example/simple"
[dependency-groups]
dev = ["private @ https://private.example/package.whl"]
'''
    value = tomllib.loads(dependency_project(data).decode())
    assert set(value) == {"project"}
    assert value["project"]["dependencies"] == ["anyio>=4", "mcp==" + MCP_VERSION]
    assert value["project"]["requires-python"] == "==" + PYTHON_VERSION
    assert b"private.example" not in dependency_project(data)


@pytest.mark.parametrize("text,code", [
    ('[project]\nname="app"\ndependencies=["unsafe @ https://example.com/a.whl"]', "pi_dependency_direct_url_unsupported"),
    ('[project]\nname="app"\ndependencies=["mcp<1"]', "pi_dependency_mcp_version_unsupported"),
    ('[project]\nname="app"\nrequires-python=">=3.13"', "pi_dependency_python_version_unsupported"),
    ('[project]\nname="app"\ndynamic=["dependencies"]', "pi_dependency_project_unsupported"),
])
def test_rejects_unsupported_dependency_declarations(text, code):
    with pytest.raises(PreparationError, match=code):
        dependency_project(text.encode())


async def fake_build(self, work, source):
    runtime = work / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "python/bin").mkdir(parents=True)
    (runtime / "python/bin/python3.12").write_bytes(b"synthetic private interpreter")
    (runtime / "python/site-packages").mkdir()
    lock = b'version=1\n[[package]]\nname="mcp"\nversion="1.30.0"\nsource={registry="https://pypi.org/simple"}\n'
    (runtime / "uv.lock").write_bytes(lock)
    (runtime / "requirements.txt").write_text("mcp==1.30.0 --hash=sha256:" + "a" * 64 + "\n")
    return runtime, lock, [{"name": "mcp", "version": MCP_VERSION}]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS private runtime")
async def test_dependency_result_publishes_public_lock_and_private_copied_runtime(tmp_path, group_reaper, monkeypatch):
    monkeypatch.setattr(_Installer, "build", fake_build)
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper)
    broker.host_root = tmp_path.resolve()
    project = project_fixture(broker)
    try:
        result = await broker.invoke("install_dependencies", {"ecosystem": "python", "project_path": "app"})
        assert result["success"], result["output"]
        metadata = json.loads(result["output"])
        runtime = Path(metadata["runtime_path"])
        assert runtime == project / ".mcpme-runtime"
        assert Path(metadata["python_path"]).is_file()
        assert not Path(metadata["python_path"]).is_symlink()
        assert (project / "uv.lock").read_bytes() == (runtime / "uv.lock").read_bytes()
        assert b"https://pypi.org/simple" in (project / "uv.lock").read_bytes()
        assert metadata["versions"] == [{"name": "mcp", "version": MCP_VERSION}]
        assert not list(broker.workspace.glob(".mcpme-dependencies-*"))
        # Repeated installs replace only the previously generated dependency tree.
        (runtime / "old-file").write_text("previous dependency")
        repeated = await broker.invoke("install_dependencies", {"ecosystem": "python", "project_path": "app"})
        assert repeated["success"] and not (runtime / "old-file").exists()
        assert (runtime / MARKER).is_file()
    finally:
        await broker.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS private runtime")
async def test_rejection_precedes_install_and_preserves_source(tmp_path, group_reaper, monkeypatch):
    calls = []
    async def forbidden(*args):
        calls.append(True)
        raise AssertionError("must not execute")
    monkeypatch.setattr(_Installer, "build", forbidden)
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper)
    project = project_fixture(broker, '[project]\nname="app"\ndependencies=["unsafe @ https://example.com/a.whl"]')
    original = (project / "pyproject.toml").read_bytes()
    try:
        result = await install_dependencies(broker, {"ecosystem": "python", "project_path": "app"}, tmp_path.resolve())
        assert result == {"success": False, "output": "pi_dependency_direct_url_unsupported"}
        assert calls == [] and (project / "pyproject.toml").read_bytes() == original
        assert not (project / "uv.lock").exists()
        assert not list(broker.workspace.glob(".mcpme-dependencies-*"))
    finally:
        await broker.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS private runtime")
@pytest.mark.parametrize("target", ["../outside", "app", "app/occupied", "app/link"])
async def test_target_cannot_escape_or_replace_source_and_unowned_content(tmp_path, group_reaper, monkeypatch, target):
    monkeypatch.setattr(_Installer, "build", fake_build)
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper)
    project = project_fixture(broker)
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (outside / "kept").write_text("private")
    (project / "occupied").mkdir()
    (project / "occupied/kept").write_text("source")
    (project / "link").symlink_to(outside, target_is_directory=True)
    try:
        result = await install_dependencies(broker, {"ecosystem": "python", "project_path": "app", "target_path": target}, tmp_path.resolve())
        assert not result["success"]
        assert (outside / "kept").read_text() == "private"
        assert (project / "occupied/kept").read_text() == "source"
    finally:
        await broker.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native ownership")
async def test_cancel_settles_installer_child_before_workspace_cleanup(tmp_path, group_reaper, monkeypatch):
    started = asyncio.Event()
    async def long_build(self, work, source):
        async def execute():
            started.set()
            await self.executor.execute(Path(sys.executable).resolve(), ["-I", "-S", "-c", "import time;time.sleep(60)"],
                work, {}, timeout_seconds=120)
        await self.controlled(execute())
    monkeypatch.setattr(_Installer, "build", long_build)
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper)
    project_fixture(broker)
    try:
        task = asyncio.create_task(install_dependencies(broker, {"ecosystem": "python", "project_path": "app"}, tmp_path.resolve()))
        await started.wait()
        async with asyncio.timeout(10):
            while not broker.owner.instances:
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not broker.owner.instances
        assert not list(broker.workspace.glob(".mcpme-dependencies-*"))
    finally:
        await broker.close()


def test_resolver_profile_allows_only_exact_installer_and_broker(tmp_path):
    from types import SimpleNamespace
    work = tmp_path.resolve() / "work"
    runtime = tmp_path.resolve() / "runtime"
    uv = tmp_path.resolve() / "uv"
    python = runtime / "bin/python3.12"
    toolchain = SimpleNamespace(python_runtime=runtime, uv=uv)
    text = _profile(work, toolchain, uv, python, port=32123)
    assert '(remote tcp "localhost:32123")' in text
    assert '(subpath "/System")' not in text
    assert '(deny syscall-unix (syscall-number 82 147)' in text
    assert '(allow file-read-metadata)' not in text
    assert '(allow process-exec)' not in text
    assert 'network-outbound' not in _profile(work, toolchain, uv, python)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS installer confinement")
async def test_actual_installer_sandbox_denies_external_connections_user_files_and_group_escape(tmp_path, group_reaper):
    from types import SimpleNamespace
    from mcpme.preparation_processes import PreparationExecutor
    work = tmp_path.resolve() / "work"
    work.mkdir(mode=0o700)
    secret = tmp_path.resolve() / "outside-source"
    secret.write_text("synthetic private bytes")
    python = Path(sys.executable).resolve()
    toolchain = SimpleNamespace(python_runtime=python.parents[1], uv=python)
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    alias = "/System/Volumes/Data" + str(secret)
    code = "import json,os,socket;results=[]\n"
    code += "for action in [lambda:open(" + repr(str(secret)) + ").read(),lambda:open(" + repr(alias) + ").read(),lambda:socket.create_connection(('127.0.0.1'," + str(port) + "),.1),lambda:os.setpgid(0,0)]:\n"
    code += " try: action();results.append(False)\n except OSError:results.append(True)\nprint(json.dumps(results))\n"
    executor = PreparationExecutor(group_reaper)
    try:
        result = await executor.execute(Path("/usr/bin/sandbox-exec"), ["-p", _profile(work, toolchain, python, python), str(python), "-I", "-S", "-B", "-c", code],
            work, {"PATH": "/usr/bin:/bin", "HOME": str(work), "TMPDIR": str(work)}, timeout_seconds=10)
        assert json.loads(result.stdout) == [True, True, True, True]
    finally:
        await executor.close()
        server.close()
        await server.wait_closed()


async def test_node_reports_unavailable_without_running_installer(tmp_path, group_reaper):
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper)
    try:
        result = await install_dependencies(broker, {"ecosystem": "node", "project_path": "."}, None)
        assert result == {"success": False, "output": "pi_node_dependencies_unavailable"}
        assert not broker.owner.instances
    finally:
        await broker.close()


@pytest.mark.skipif(os.environ.get("MCPME_PI_DEPENDENCY_LIVE_TEST") != "1", reason="explicit public PyPI integration check")
async def test_private_uv_live_public_registry_install_is_locked_copied_and_offline(tmp_path, group_reaper):
    host_root = Path(os.environ.get("MCPME_PI_HOST_ROOT", "/Applications/mcpme.app/Contents/Resources/host")).resolve()
    assert platform.system() == "Darwin" and platform.machine() == "arm64"
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper, read_roots=[host_root])
    project = project_fixture(broker, '[project]\nname="pi-public-smoke"\nversion="1.0.0"\ndependencies=["anyio==4.11.0"]\n')
    try:
        result = await install_dependencies(broker, {"ecosystem": "python", "project_path": "app"}, host_root)
        assert result["success"], result["output"]
        metadata = json.loads(result["output"])
        runtime = Path(metadata["runtime_path"])
        lock = (runtime / "uv.lock").read_bytes()
        assert b"https://pypi.org/simple" in lock and b"https://files.pythonhosted.org" in lock
        assert b"127.0.0.1" not in lock and b"mcpme-pi-dependencies" not in lock
        assert {item["name"]: item["version"] for item in metadata["versions"]}["mcp"] == MCP_VERSION
        assert all(not item.is_symlink() for item in runtime.rglob("*"))
        # The finished app can import SDK dependencies with its copied Python,
        # without giving that interpreter any outbound network permission.
        from mcpme.preparation_processes import PreparationExecutor
        from mcpme.preparation_builder import load_private_toolchain
        toolchain = load_private_toolchain(host_root)
        python = Path(metadata["python_path"])
        profile = _profile(runtime, toolchain, python, python)
        code = "import sys;sys.path.insert(0,sys.argv[1]);import mcp,anyio;print('dependencies_imported')"
        executor = PreparationExecutor(group_reaper, owner=broker.owner)
        result = await executor.execute(Path("/usr/bin/sandbox-exec"), ["-p", profile, str(python), "-I", "-S", "-B", "-c", code, metadata["site_packages_path"]],
            runtime, {"PATH": "/usr/bin:/bin", "HOME": str(runtime), "TMPDIR": str(runtime)}, timeout_seconds=20)
        assert result.stdout == b"dependencies_imported\n"
    finally:
        await broker.close()
