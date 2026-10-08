from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from meecp.preparation_contracts import PreparationError, parse_descriptor
from meecp.preparation_verifier import verify_prepared_package
from test_meecp_packages import edit_manifest, make_package, relock


@pytest.fixture(scope="module")
def probe_reaper(tmp_path_factory):
    output = tmp_path_factory.mktemp("probe-reaper") / "meecp-reap"
    source = Path(__file__).resolve().parents[1] / "scripts/meecp-reap.c"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(output)],
                   check=True, capture_output=True)
    return output


def probe_fixture(tmp_path, mode="valid"):
    package = make_package(tmp_path / "package")
    tool = {"name": "echo", "description": "Synthetic echo", "input_schema": {
        "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
        "annotations": {"readOnlyHint": True}}
    if mode == "bool_number":
        tool["output_schema"] = {"type": "object"}
    descriptor = parse_descriptor({"schema_version": 1, "recipe": {"id": "test.probe", "version": "1.0.0", "digest": "a"*64},
        "package": {"id": "local.fixture", "version": "1.0.0", "name": "Fixture"},
        "interface": {"kind": "python-callable", "module": "fixture", "function": "echo", "tools": [tool]},
        "dependencies": {"project": ".", "lock_sha256": "b"*64, "python_version": "3.12.13", "mcp_version": "1.30.0"},
        "source_license": "LICENSE", "resources": {"memory_mb": 64}, "validation": [
            {"id": "synthetic", "tool": "echo", "arguments": {"value": 7},
             "expected": {"value": True if mode=="bool_number" else 7}, "synthetic": True}]})
    edit_manifest(package, tools=[tool], lifecycle=descriptor.lifecycle.model_dump())
    # A real OS process emits MCP wire messages. The client is the locked SDK;
    # this fixture tests verifier decisions, not a private-runtime release gate.
    source = "\n".join([
        "#!" + sys.executable,
        "import json,sys,time,os,signal",
        "from pathlib import Path",
        "mode=" + repr(mode),
        "if mode=='timeout_tree':",
        "    signal.signal(signal.SIGTERM,signal.SIG_IGN)",
        "    child=os.fork()",
        "    Path(os.environ['TMPDIR'],'probe.pids').open('a').write(str(os.getpid())+' ')",
        "    if child==0: time.sleep(100); sys.exit(0)",
        "tool=" + repr(descriptor.interface.tools[0].mcp_schema()),
        "if mode=='memory': allocation=bytearray(128*1024*1024)",
        "for line in sys.stdin:",
        "    request=json.loads(line)",
        "    if 'id' not in request: continue",
        "    method=request['method']",
        "    if mode in {'timeout','timeout_tree'}: time.sleep(100)",
        "    if method=='initialize': result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'probe','version':'1.0.0'}}",
        "    elif method=='tools/list':",
        "        if mode=='schema': tool['description']='Different behavior'",
        "        result={'tools':[tool]}",
        "    elif method=='tools/call':",
        "        if mode=='mutate': Path('LICENSE').write_text('Changed notice')",
        "        if mode=='extra': Path('unexpected-file').write_text('Unlisted write')",
        "        output={'value':1 if mode=='bool_number' else 8 if mode=='wrong_result' else 7}",
        "        result={'content':[{'type':'text','text':json.dumps(output)}],'structuredContent':output,'isError':False}",
        "    else: result={}",
        "    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)",
    ]) + "\n"
    (package / "bin/service").write_text(source)
    relock(package)
    return package, descriptor


async def test_real_sdk_full_schema_fixture_and_relocation(probe_reaper, tmp_path):
    package, descriptor = probe_fixture(tmp_path)
    moved = tmp_path / "unrelated relocation"
    package.rename(moved)
    workspace = tmp_path / "probe-data"
    workspace.mkdir(mode=0o700)
    report = await verify_prepared_package(moved, descriptor, probe_reaper, workspace)
    assert report.tools_checked == 1 and report.fixtures_checked == 1
    assert report.protocol_version == "2025-11-25"
    assert len(report.package_digest) == 64
    assert 0 < report.peak_memory_bytes <= 64 * 1024 * 1024
    assert set(report.__dict__) == {"package_digest", "tools_checked", "fixtures_checked", "protocol_version", "peak_memory_bytes"}


@pytest.mark.parametrize("mode,code", [
    ("schema", "preparation_tool_schema_mismatch"),
    ("wrong_result", "preparation_fixture_failed"),
    ("bool_number", "preparation_fixture_failed"),
    ("mutate", "preparation_package_mutated"),
    ("extra", "preparation_package_mutated"),
    ("timeout", "preparation_probe_timeout"),
    ("memory", "preparation_probe_memory_limit"),
])
async def test_independent_probe_rejects_result(probe_reaper, tmp_path, mode, code, capfd):
    package, descriptor = probe_fixture(tmp_path, mode)
    with pytest.raises(PreparationError) as failure:
        await verify_prepared_package(package, descriptor, probe_reaper, tmp_path, timeout_seconds=0.5 if mode=="timeout" else 5)
    assert failure.value.code == code
    assert "Synthetic echo" not in str(failure.value)
    assert not "".join(capfd.readouterr())


async def test_probe_cancellation_propagates_after_reaping(probe_reaper, tmp_path):
    package, descriptor = probe_fixture(tmp_path, "timeout_tree")
    task = asyncio.create_task(verify_prepared_package(package, descriptor, probe_reaper, tmp_path))
    marker = tmp_path / "probe.pids"
    async with asyncio.timeout(5):
        while not marker.exists() or len(marker.read_text().split()) != 2:
            await asyncio.sleep(0.01)
    pids = [int(value) for value in marker.read_text().split()]
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(5):
        for pid in pids:
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
