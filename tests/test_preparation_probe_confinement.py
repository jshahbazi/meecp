from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile

import pytest

from mcpme.contracts import validate_package
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_probe_confinement import ProbeConfinementV1
from mcpme.preparation_verifier import verify_prepared_package
from test_preparation_verifier import probe_fixture, probe_reaper

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or platform.machine() != "arm64"
                               or not platform.mac_ver()[0].startswith("26."), reason="macOS 26 arm64 probe policy")


def _lock_all(root):
    manifest = json.loads((root / "mcpme.package.json").read_bytes())
    lock = json.loads((root / "mcpme.lock.json").read_bytes())
    lock["manifest_sha256"] = hashlib.sha256((root / "mcpme.package.json").read_bytes()).hexdigest()
    lock["artifacts"] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() or path.name in {"mcpme.package.json", "mcpme.lock.json"}:
            continue
        value = path.read_bytes()
        digest = hashlib.sha256(value).hexdigest()
        lock["artifacts"].append({"path": str(path.relative_to(root)), "size": len(value), "sha256": digest,
                                  "executable": bool(path.stat().st_mode & 0o111), "source": "prepared:sha256:" + digest,
                                  "provenance": "Synthetic private runtime fixture", "license": "LICENSE"})
    (root / "mcpme.lock.json").write_text(json.dumps(lock))


@pytest.fixture(scope="module")
def private_runtime(tmp_path_factory):
    destination = tmp_path_factory.mktemp("confined-private-runtime") / "python"
    source = Path(sys.executable).resolve().parents[1]
    shutil.copytree(source, destination, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"))
    # All retained bytes are copied private files. No development interpreter
    # path is admitted by the runtime policy below.
    for path in destination.rglob("*"):
        path.chmod(0o700 if path.is_dir() or path.stat().st_mode & 0o111 else 0o600)
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return destination


def _private_fixture(tmp_path, runtime, *, checks=""):
    package, descriptor = probe_fixture(tmp_path)
    bootstrap = (package / "bin/service").read_text().split("\n", 1)[1]
    bootstrap = "import os,sys,socket,json\n" + checks + bootstrap
    (package / "bootstrap.py").write_text(bootstrap)
    (package / "bin/service").write_text(
        '#!/bin/sh\nset -eu\nBIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'PACKAGE=$(CDPATH= cd -- "$BIN/.." && pwd)\n'
        'exec "$PACKAGE/python/bin/python3.12" -I -S -B "$PACKAGE/bootstrap.py" "$@" 2>/dev/null\n')
    shutil.copytree(runtime, package / "python")
    workspace = tmp_path / "private-probe"
    workspace.mkdir(mode=0o700)
    _lock_all(package)
    return package, descriptor, workspace


async def test_actual_private_sdk_probe_denies_external_effects(private_runtime, probe_reaper, tmp_path):
    canary = tmp_path / "nonsecret-host-canary"
    canary.write_text("synthetic canary")
    observed = []
    def accepted(reader, writer):
        observed.append(True)
        writer.close()
    listener = await asyncio.start_server(accepted, "127.0.0.1", 0)
    unix_root = tempfile.TemporaryDirectory(prefix="mpp-", dir="/private/tmp")
    socket_path = Path(unix_root.name) / "canary.socket"
    unix_listener = None
    port = listener.sockets[0].getsockname()[1]
    checks = (
        "def denied(action):\n"
        " try: action()\n"
        " except OSError: return\n"
        " raise RuntimeError('unexpected permission')\n"
        "denied(lambda:open(" + repr(str(canary)) + ").read())\n"
        "denied(lambda:os.stat(" + repr(str(canary)) + "))\n"
        "denied(lambda:open('LICENSE','w'))\n"
        "denied(lambda:socket.create_connection(('127.0.0.1'," + str(port) + "),.1))\n"
        "denied(lambda:socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b'canary',('127.0.0.1'," + str(port) + ")))\n"
        "denied(lambda:socket.socket(socket.AF_UNIX).connect(" + repr(str(socket_path)) + "))\n"
        "denied(os.fork)\n"
        "denied(os.setsid)\n"
        "denied(lambda:os.setpgid(0,0))\n"
        "denied(lambda:os.posix_spawn(sys.executable,[sys.executable,'-c','pass'],{},setsid=True))\n"
        "denied(lambda:os.posix_spawn(sys.executable,[sys.executable,'-c','pass'],{},setpgroup=0))\n"
        "open(os.path.join(os.environ['HOME'],'permitted-cache'),'w').write('synthetic')\n"
        "open(os.path.join(os.environ['TMPDIR'],'permitted-data'),'w').write('synthetic')\n"
    )
    try:
        unix_listener = await asyncio.start_unix_server(accepted, path=str(socket_path))
        package, descriptor, workspace = _private_fixture(tmp_path, private_runtime, checks=checks)
        validated = validate_package(package)
        plan = ProbeConfinementV1.capture(validated, workspace, probe_reaper)
        report = await verify_prepared_package(package, descriptor, probe_reaper, workspace, probe_confinement=plan)
        assert report.tools_checked == report.fixtures_checked == 1
        assert 0 < report.peak_memory_bytes <= 64 * 1024**2
        assert (plan.home / "permitted-cache").read_text() == "synthetic"
        assert (plan.temporary / "permitted-data").read_text() == "synthetic"
        assert not observed
    finally:
        listener.close()
        await listener.wait_closed()
        if unix_listener is not None:
            unix_listener.close()
            await unix_listener.wait_closed()
        unix_root.cleanup()


def test_unknown_launcher_cannot_select_private_policy(private_runtime, probe_reaper, tmp_path):
    package, descriptor, workspace = _private_fixture(tmp_path, private_runtime)
    (package / "bin/service").write_text('#!/bin/sh\nexec /usr/bin/true\n')
    _lock_all(package)
    with pytest.raises(PreparationError) as caught:
        ProbeConfinementV1.capture(validate_package(package), workspace, probe_reaper)
    assert caught.value.code == "unsupported_preparation_probe_confinement"
    assert not (workspace / "confined-home").exists()


def test_policy_cannot_be_widened_after_capture(private_runtime, probe_reaper, tmp_path):
    package, descriptor, workspace = _private_fixture(tmp_path, private_runtime)
    validated = validate_package(package)
    plan = ProbeConfinementV1.capture(validated, workspace, probe_reaper)
    widened = replace(plan, profile=plan.profile + "\n(allow network*)")
    with pytest.raises(PreparationError) as caught:
        widened.check(validated, probe_reaper)
    assert caught.value.code == "preparation_probe_confinement_mismatch"


async def test_canceled_confinement_check_is_joined_before_return(private_runtime, probe_reaper, tmp_path, monkeypatch):
    import threading
    package, descriptor, workspace = _private_fixture(tmp_path, private_runtime)
    validated = validate_package(package)
    plan = ProbeConfinementV1.capture(validated, workspace, probe_reaper)
    entered, release = threading.Event(), threading.Event()
    original = ProbeConfinementV1.launch
    def held(self, *args):
        entered.set()
        assert release.wait(5)
        return original(self, *args)
    monkeypatch.setattr(ProbeConfinementV1, "launch", held)
    task = asyncio.create_task(verify_prepared_package(package, descriptor, probe_reaper, workspace, probe_confinement=plan))
    async with asyncio.timeout(10):
        while not entered.is_set():
            await asyncio.sleep(.01)
    task.cancel()
    await asyncio.sleep(.02)
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("replacement", ["symlink", "regular"])
def test_plan_rejects_replaced_private_directory(private_runtime, probe_reaper, tmp_path, replacement):
    package, descriptor, workspace = _private_fixture(tmp_path, private_runtime)
    plan = ProbeConfinementV1.capture(validate_package(package), workspace, probe_reaper)
    plan.home.rename(workspace / "displaced-home")
    if replacement == "symlink":
        plan.home.symlink_to(workspace / "displaced-home")
    else:
        plan.home.mkdir(mode=0o700)
    with pytest.raises(PreparationError) as caught:
        plan.check(validate_package(package), probe_reaper)
    assert caught.value.code == ("unsafe_preparation_probe_path" if replacement == "symlink" else "preparation_probe_confinement_mismatch")
