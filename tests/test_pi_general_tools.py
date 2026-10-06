"""General Pi facilities exercised through real macOS confinement and ownership."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import platform
import shutil
import ssl
import subprocess
import sys

import pytest

from mcpme.pi_tools import MAX_OUTPUT, MAX_READ, PiToolBroker
from test_preparation_processes import gone, group_reaper


MACOS = (platform.system() == "Darwin" and platform.machine() == "arm64"
         and platform.mac_ver()[0].startswith("26."))


@pytest.fixture
async def broker(tmp_path):
    instance = PiToolBroker(tmp_path / "workspace", Path("/unused"))
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def native_broker(tmp_path, group_reaper):
    if not MACOS:
        pytest.skip("exact macOS 26 arm64 confinement")
    python = Path(sys.executable).resolve()
    instance = PiToolBroker(tmp_path / "workspace", group_reaper, read_roots=(python.parents[1],))
    try:
        yield instance, python
    finally:
        await instance.close()


def payload(result):
    assert result["success"] is True
    return json.loads(result["output"])


async def test_capability_discovery_exposes_generic_facilities_without_recipes(broker):
    discovered = payload(await broker.invoke("capabilities", {}))
    assert discovered["package"]["recipe_required"] is False
    assert {"list_files", "read_file", "start_process", "read_process", "stop_process",
            "prepare_environment", "extract_archive", "ask_user"} <= set(discovered["tools"])
    assert discovered["platform"] == {"os": platform.system(), "architecture": platform.machine()}
    assert discovered["workspace"] == str(broker.workspace)


async def test_paged_reads_reconstruct_large_utf8_without_splitting_characters(broker):
    text = "abé漢字🙂Z\n" * 20000
    raw = text.encode("utf-8")
    assert len(raw) > MAX_READ
    path = broker.workspace / "large.txt"
    path.write_bytes(raw)
    assert await broker.invoke("read_file", {"path": "large.txt"}) == {
        "success": False, "output": "file_size_limit"}
    position, pages = 0, []
    while True:
        page = payload(await broker.invoke("read_file", {"path": "large.txt", "offset": position, "limit": 4097}))
        pages.append(page["content"])
        assert page["next_offset"] - position == len(page["content"].encode("utf-8"))
        position = page["next_offset"]
        if page["eof"]:
            break
    assert position == len(raw) and "".join(pages) == text
    assert await broker.invoke("read_file", {"path": "large.txt", "offset": 2, "limit": 1}) == {
        "success": False, "output": "read_limit_too_small"}
    assert await broker.invoke("read_file", {"path": "large.txt", "offset": 3, "limit": 10}) == {
        "success": False, "output": "file_not_utf8"}


async def test_recursive_listing_pages_do_not_follow_external_links(broker, tmp_path):
    root = broker.workspace / "project"
    root.mkdir(mode=0o700)
    (root / "nested").mkdir(mode=0o700)
    for index in range(7):
        (root / "nested" / f"file-{index}.txt").write_text(str(index))
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "private-sentinel.txt").write_text("private-value")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    offset, entries = 0, []
    while True:
        page = payload(await broker.invoke("list_files", {"path": "project", "recursive": True,
                                                                  "offset": offset, "limit": 3}))
        assert len(page["entries"]) <= 3
        entries.extend(page["entries"])
        assert page["next_offset"] == offset + len(page["entries"])
        offset = page["next_offset"]
        if page["eof"]:
            break
    assert [entry["path"] for entry in entries] == sorted(entry["path"] for entry in entries)
    assert {entry["path"] for entry in entries} == {"escape", "nested", *(f"nested/file-{i}.txt" for i in range(7))}
    assert next(entry for entry in entries if entry["path"] == "escape")["kind"] == "link"
    assert "private-sentinel" not in json.dumps(entries)
    assert (await broker.invoke("list_files", {"path": "project/escape", "recursive": True}))["success"] is False


@pytest.mark.parametrize("tool,arguments", [
    ("list_files", {"limit": True}), ("list_files", {"limit": 501}),
    ("list_files", {"offset": -1}), ("list_files", {"recursive": "true"}),
    ("read_file", {"path": "file", "offset": True}),
    ("read_file", {"path": "file", "limit": MAX_READ + 1}),
])
async def test_paging_bounds_reject_invalid_types_and_sizes(broker, tool, arguments):
    (broker.workspace / "file").write_text("source")
    assert await broker.invoke(tool, arguments) == {"success": False, "output": "invalid_arguments"}


@pytest.mark.parametrize("returncode", [0, 4])
async def test_opaque_credentials_are_injected_with_all_output_hidden(native_broker, capfd, returncode):
    broker, python = native_broker
    secret = "synthetic-general-tool-secret-never-returned"
    bindings = {"SERVICE_API_KEY": "opaque-job-secret-id"}
    calls = []
    def resolve(references):
        calls.append(dict(references))
        if references != bindings:
            raise ValueError("invalid-secret-reference")
        return {"SERVICE_API_KEY": secret}
    broker.credential_resolver = resolve
    code = ("import os,sys; value=os.environ['SERVICE_API_KEY']; print(value*10000); print(value,file=sys.stderr); "
            f"sys.exit({returncode})")
    result = await broker.invoke("run_command", {"argv": [str(python), "-I", "-S", "-B", "-c", code],
                                                  "credentials": bindings, "timeout_seconds": 10})
    assert result["success"] is (returncode == 0)
    assert json.loads(result["output"]) == {"returncode": returncode, "stdout": "", "output_hidden": True}
    assert secret not in json.dumps(result) and calls == [bindings]
    assert secret not in "".join(capfd.readouterr())
    rejected = await broker.invoke("run_command", {"argv": ["/bin/true"],
                                                    "credentials": {"SERVICE_API_KEY": "unrelated-id"}})
    assert rejected == {"success": False, "output": "credentials_unavailable"}
    assert not broker.owner.instances


@pytest.fixture
def local_certificate(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is required for the controlled HTTPS fixture")
    config = tmp_path / "certificate.cnf"
    config.write_text("[req]\nprompt=no\ndistinguished_name=subject\nx509_extensions=extensions\n"
                      "[subject]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
                      "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyCertSign\n"
                      "extendedKeyUsage=serverAuth\n")
    certificate, key = tmp_path / "certificate.pem", tmp_path / "certificate.key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-config", str(config), "-keyout", str(key), "-out", str(certificate)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    return certificate, key


async def test_network_option_permits_real_authenticated_https_and_default_denies_it(native_broker, local_certificate):
    broker, python = native_broker
    certificate, key = local_certificate
    staged_certificate = broker.workspace / "test-ca.pem"
    staged_certificate.write_bytes(certificate.read_bytes())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    requests, handlers = [], set()
    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            assert b"Authorization: Bearer synthetic-network-fixture\r\n" in request
            requests.append(request)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\nConnection: close\r\n\r\nfixture-https-ok")
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            handlers.discard(asyncio.current_task())
    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    code = ("import ssl,urllib.request; "
            "context=ssl.create_default_context(cafile='test-ca.pem'); "
            f"request=urllib.request.Request('https://127.0.0.1:{port}/fixture', "
            "headers={'Authorization':'Bearer synthetic-network-fixture'}); "
            "print(urllib.request.urlopen(request,context=context,timeout=3).read().decode())")
    command = {"argv": [str(python), "-I", "-S", "-B", "-c", code], "timeout_seconds": 10}
    try:
        blocked = await broker.invoke("run_command", command)
        assert blocked["success"] is False
        assert not requests
        result = await broker.invoke("run_command", {**command, "network": True})
        assert payload(result)["stdout"] == "fixture-https-ok\n"
        assert len(requests) == 1
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*tuple(handlers), return_exceptions=True), 5)
    assert not broker.owner.instances


async def test_network_listeners_bind_loopback_and_reject_wildcard(native_broker):
    broker, python = native_broker
    for address in ("127.0.0.1", "0.0.0.0"):
        code = "import socket,time; s=socket.socket(); s.bind((" + repr(address) + ",0)); s.listen(); time.sleep(20)"
        result = await broker.invoke("start_process", {"argv": [str(python), "-I", "-S", "-B", "-c", code], "network": True})
        if address == "127.0.0.1":
            identity = json.loads(result["output"])["process_id"]
            await asyncio.sleep(.3)
            assert (await broker.invoke("read_process", {"process_id": identity}))["success"]
            await broker.invoke("stop_process", {"process_id": identity})
        else:
            if result["success"]:
                identity = json.loads(result["output"])["process_id"]
                async with asyncio.timeout(5):
                    while result["success"]:
                        await asyncio.sleep(.05)
                        result = await broker.invoke("read_process", {"process_id": identity})
            assert result == {"success": False, "output": "listener_not_loopback"}


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0"])
async def test_network_foreground_commands_audit_live_listeners(native_broker, address):
    broker, python = native_broker
    code = ("import os,socket,time; s=socket.socket(); s.bind((" + repr(address) + ",0)); "
            "s.listen(); open('foreground.pid','w').write(str(os.getpid())); time.sleep(.8)")
    result = await broker.invoke("run_command", {"argv": [str(python), "-I", "-S", "-B", "-c", code],
                                                  "network": True, "timeout_seconds": 10})
    if address == "127.0.0.1":
        assert payload(result)["returncode"] == 0
    else:
        assert result == {"success": False, "output": "listener_not_loopback"}
    await gone(int((broker.workspace / "foreground.pid").read_text()))
    assert not broker.owner.instances


async def test_network_foreground_listener_audit_is_joined_on_cancellation(native_broker):
    broker, python = native_broker
    code = ("import os,socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
            "open('foreground.pid','w').write(str(os.getpid())); time.sleep(20)")
    task = asyncio.create_task(broker.invoke("run_command", {"argv": [str(python), "-I", "-S", "-B", "-c", code],
                                                             "network": True, "timeout_seconds": 30}))
    async with asyncio.timeout(5):
        while not (broker.workspace / "foreground.pid").is_file():
            await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await gone(int((broker.workspace / "foreground.pid").read_text()))
    assert not broker.owner.instances


async def wait_service_files(broker):
    async with asyncio.timeout(5):
        while len(list(broker.workspace.glob("service-pid-*"))) < 2:
            await asyncio.sleep(.01)
    return [int(path.read_text()) for path in broker.workspace.glob("service-pid-*")]


@pytest.mark.parametrize("termination", ["stop", "close", "cancel-close"])
async def test_background_service_lifecycle_reaps_descendants(native_broker, termination):
    broker, python = native_broker
    code = ("import os,time,signal; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); child=os.fork(); "
            "Path('service-pid-'+str(os.getpid())).write_text(str(os.getpid())); "
            f"print('x'*{MAX_OUTPUT + 1000},flush=True); time.sleep(100)")
    started = payload(await broker.invoke("start_process", {"argv": [str(python), "-I", "-S", "-B", "-c", code]}))
    identity = started["process_id"]
    pids = await wait_service_files(broker)
    async with asyncio.timeout(5):
        while True:
            state = payload(await broker.invoke("read_process", {"process_id": identity}))
            if state["stdout"]:
                break
            await asyncio.sleep(.01)
    assert state["state"] == "running" and 0 < len(state["stdout"].encode()) <= MAX_OUTPUT
    if termination == "stop":
        stopped = payload(await broker.invoke("stop_process", {"process_id": identity}))
        assert stopped["state"] == "exited" and stopped["returncode"] is not None
    elif termination == "close":
        await broker.close()
    else:
        closing = asyncio.create_task(broker.close())
        await asyncio.sleep(.01)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
    for pid in pids:
        await gone(pid)
    assert not broker.owner.instances


async def test_background_credential_output_is_never_retained(native_broker, capfd):
    broker, python = native_broker
    secret = "synthetic-background-credential"
    broker.credential_resolver = lambda references: {"SERVICE_KEY": secret}
    code = "import os,time; print(os.environ['SERVICE_KEY'],flush=True); open('ready','w').close(); time.sleep(100)"
    started = payload(await broker.invoke("start_process", {"argv": [str(python), "-I", "-S", "-B", "-c", code],
                                                            "credentials": {"SERVICE_KEY": "opaque-id"}}))
    identity = started["process_id"]
    async with asyncio.timeout(5):
        while not (broker.workspace / "ready").exists():
            await asyncio.sleep(.01)
    result = payload(await broker.invoke("read_process", {"process_id": identity}))
    assert result["state"] == "running" and result["stdout"] == "" and result["output_hidden"] is True
    assert broker.services[identity]["output"] == b""
    stopped = await broker.invoke("stop_process", {"process_id": identity})
    assert secret not in json.dumps(stopped) and secret not in "".join(capfd.readouterr())
    assert not broker.owner.instances


async def test_actual_system_compiler_builds_and_runs_custom_native_program(native_broker):
    broker, _ = native_broker
    source = "#include <stdio.h>\nint main(int argc,char **argv) { if(argc != 2) return 2; printf(\"native:%s\\n\",argv[1]); return 0; }\n"
    assert (await broker.invoke("write_file", {"path": "custom.c", "content": source}))["success"] is True
    build = await broker.invoke("run_command", {"argv": ["/usr/bin/clang", "custom.c", "-O2", "-o", "custom"],
                                                 "timeout_seconds": 30})
    assert payload(build)["returncode"] == 0
    executed = await broker.invoke("run_command", {"argv": [str(broker.workspace / "custom"), "staged"],
                                                    "timeout_seconds": 5})
    assert payload(executed) == {"returncode": 0, "stdout": "native:staged\n"}
    empty = await broker.invoke("run_command", {"argv": [str(broker.workspace / "custom"), ""], "timeout_seconds": 5})
    assert payload(empty) == {"returncode": 0, "stdout": "native:\n"}
    assert not broker.owner.instances
