from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import hashlib
import threading
import re
from types import SimpleNamespace

import pytest

from meecp.preparation_contracts import PreparationError
from meecp.preparation_textgen_probe_confinement import role_profile, SANDBOX
from meecp import preparation_textgen_probe_confinement as confinement
from meecp.processes import ProcessOwner
from test_preparation_verifier import probe_reaper

pytestmark = pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64"
    or not platform.mac_ver()[0].startswith("26."), reason="macOS26arm64 policy")


@pytest.mark.parametrize("role", ["engine", "adapter"])
async def test_actual_role_policy_denies_outside_effects_and_allows_exact_endpoint(tmp_path, probe_reaper, role):
    root = tmp_path / "package"
    (root / "bin").mkdir(parents=True)
    home, temporary = tmp_path / "home", tmp_path / "temporary"
    home.mkdir(mode=0o700); temporary.mkdir(mode=0o700)
    key = tmp_path / "adapter.key"
    key.write_bytes(b"synthetic"); key.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_bytes(b"nonsecret")
    package_file = root / "preserve"
    package_file.write_bytes(b"nonsecret")
    executable = root / ("bin/textgen-" + role)
    source = Path(__file__).parent / "fixtures/textgen_policy_probe.c"
    result = subprocess.run(["/usr/bin/clang", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert result.returncode == 0
    observed = []
    def accepted(reader, writer):
        observed.append(True); writer.close()
    allowed = await asyncio.start_server(accepted, "127.0.0.1", 0)
    forbidden = await asyncio.start_server(lambda r,w:w.close(), "127.0.0.1", 0)
    # Keep the Unix socket path below Darwin's fixed sockaddr_un capacity.
    import tempfile
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="a4-policy-") as short:
        unix_path = Path(short) / "socket"
        unix = await asyncio.start_unix_server(lambda r,w:w.close(), str(unix_path))
        try:
            with socket.socket() as allocation:
                allocation.bind(("127.0.0.1",0))
                own_port = allocation.getsockname()[1]
            engine_port = allowed.sockets[0].getsockname()[1]
            forbidden_port = forbidden.sockets[0].getsockname()[1]
            profile = role_profile(root, home, temporary, role, own_port, engine_port if role == "adapter" else None, key)
            owner = ProcessOwner(probe_reaper)
            try:
                process = await owner.launch(SANDBOX, ["-p", profile, str(executable), str(outside),
                    str(package_file), str(forbidden_port), str(unix_path), str(engine_port), str(home / "cache"),
                    str(own_port), str(int(role == "adapter")), str(Path(sys.executable).resolve())],
                    root, {}, owned_group=True, startup_timeout=2, stop_grace=.1)
                process.stdin.close()
                output = await asyncio.wait_for(process.stdout.read(64), 4)
                await process.wait()
                assert process.returncode == 0 and output == b"13 4 0\n"
                assert package_file.read_bytes() == b"nonsecret" and bool(observed) == (role == "adapter")
            finally:
                await owner.close()
        finally:
            for listener in (allowed, forbidden, unix):
                listener.close(); await listener.wait_closed()


@pytest.mark.parametrize("wildcard", [False, True], ids=["pinned-loopback", "known-wildcard-limitation"])
async def test_owned_nonloopback_inbound_control(tmp_path, probe_reaper, wildcard):
    discovery=subprocess.run(["/sbin/ifconfig","-a"],capture_output=True)
    addresses=[item for item in re.findall(rb"\binet (\d+\.\d+\.\d+\.\d+)",discovery.stdout)
               if not item.startswith((b"127.",b"169.254."))]
    if not addresses: pytest.skip("no local nonloopback IPv4 interface")
    root=tmp_path/"package";(root/"bin").mkdir(parents=True)
    home=tmp_path/"home";temporary=tmp_path/"tmp";home.mkdir();temporary.mkdir()
    executable=root/"bin/textgen-engine"
    result=subprocess.run(["/usr/bin/clang",str(Path(__file__).parent/"fixtures/textgen_policy_probe.c"),
                           "-o",str(executable)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    assert result.returncode==0
    with socket.socket() as held:
        held.bind(("127.0.0.1",0));port=held.getsockname()[1]
    profile=role_profile(root,home,temporary,"engine",port,None,tmp_path/"engine.key")
    owner=ProcessOwner(probe_reaper)
    try:
        process=await owner.launch(SANDBOX,["-p",profile,str(executable),"inbound",str(port),str(int(wildcard))],
                                   root,{},owned_group=True,startup_timeout=2,stop_grace=.1)
        process.stdin.close()
        assert await asyncio.wait_for(process.stdout.readline(),2)==b"ready\n"
        try:
            _,writer=await asyncio.wait_for(asyncio.open_connection(addresses[0].decode(),port),.3)
            writer.close();await writer.wait_closed()
        except (OSError,TimeoutError): pass
        output=await asyncio.wait_for(process.stdout.read(16),2)
        await process.wait()
        assert process.returncode==0
        if wildcard:
            # Exact-platform localhost policy cannot certify address binding:
            # actual runs observed accepted nonloopback traffic (1). Timing or
            # host networking can also prevent delivery (0). This is a recorded
            # limitation control, never a passing denial/containment assertion.
            assert output in (b"0\n",b"1\n")
        else:
            assert output==b"0\n"
    finally: await owner.close()


@pytest.mark.parametrize("role,port,engine", [("other",1,None),("engine",True,None),
    ("engine",0,None),("engine",65536,None),("engine",1,2),("adapter",1,None),
    ("adapter",1,1),("adapter",1,True)])
def test_invalid_role_endpoint_policy_rejected(tmp_path, role, port, engine):
    with pytest.raises(PreparationError):
        role_profile(tmp_path,tmp_path,tmp_path,role,port,engine,tmp_path/"key")


@pytest.fixture
def binding_fixture(tmp_path, monkeypatch, probe_reaper):
    """Only exercise host binding guards; no synthetic model can prove A4."""
    root=tmp_path/"bound-package"; root.mkdir()
    names=("bin/textgen-engine","bin/textgen-adapter","python/bin/python3.12",
           "llama/llama-server.real","model.gguf","meecp.preparation.json","meecp.package.json","meecp.lock.json")
    for name in names:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b"bounded binding fixture");path.chmod(0o700 if "/" in name else 0o600)
    package=SimpleNamespace(root=root,digest="a"*64,metadata_paths=(root/names[-2],root/names[-1]),lock=SimpleNamespace(
        artifacts=[SimpleNamespace(path=name) for name in names[:-2]]))
    descriptor=SimpleNamespace(digest="b"*64)
    workspace=tmp_path/"workspace";workspace.mkdir(mode=0o700)
    monkeypatch.setattr(confinement,"validate_package",lambda path:package)
    monkeypatch.setattr(confinement,"validate_model_package",lambda *a,**k:None)
    return package,descriptor,workspace,probe_reaper,hashlib.sha256(b"bounded binding fixture").hexdigest()


def captured(binding_fixture):
    package,descriptor,workspace,reaper,identity=binding_fixture
    return confinement.TextgenProbeConfinementV1.capture(package,descriptor,workspace,reaper,
                                                        expected_launcher_sha256=identity)


@pytest.mark.parametrize("mutation",["content","unlisted","directory","workspace","mode","plan","launcher"])
def test_native_metadata_fence_rejects_mutation(binding_fixture,mutation):
    plan=captured(binding_fixture)
    root=plan.package_root
    if mutation=="content": (root/"model.gguf").write_bytes(b"changed")
    elif mutation=="unlisted": (root/"new-file").write_bytes(b"extra")
    elif mutation=="directory":
        (root/"llama").rename(root/"old");(root/"llama").mkdir()
    elif mutation=="workspace":
        plan.workspace.rename(plan.workspace.with_name("moved"));plan.workspace.mkdir(mode=0o700)
    elif mutation=="mode": (root/"model.gguf").chmod(0o666)
    elif mutation=="launcher": (root/"bin/textgen-engine").write_bytes(b"unknown")
    else: plan=replace(plan,package_digest="f"*64)
    with pytest.raises(PreparationError): plan.admit()


def test_capture_rejects_mutation_during_validation(binding_fixture,monkeypatch):
    package,*_=binding_fixture
    def validating(path):
        (package.root/"model.gguf").write_bytes(b"changed during validation")
        return package
    monkeypatch.setattr(confinement,"validate_package",validating)
    with pytest.raises(PreparationError): captured(binding_fixture)
    assert not list(binding_fixture[2].iterdir())


def test_changed_key_cannot_cross_native_creation_fence(binding_fixture):
    plan=captured(binding_fixture)
    package,descriptor,_,reaper,_=binding_fixture
    key=plan.workspace/"engine.key";key.write_bytes(b"synthetic");key.chmod(0o600)
    *_,admit=plan.launch(package,descriptor,reaper,role="engine",port=49001,engine_port=None,
                        keyfile=key,config_fd=5)
    assert admit() is True
    key.write_bytes(b"changed")
    with pytest.raises(PreparationError): admit()


def test_host_launcher_identity_is_required(binding_fixture):
    package,descriptor,workspace,reaper,_=binding_fixture
    with pytest.raises(PreparationError):
        confinement.TextgenProbeConfinementV1.capture(package,descriptor,workspace,reaper,
                                                     expected_launcher_sha256="f"*64)
    assert not list(workspace.iterdir())


async def test_capture_worker_remains_owned_through_repeated_cancellation():
    from meecp.preparation_textgen_verifier import _joined
    entered,release,finished=threading.Event(),threading.Event(),threading.Event()
    def work():
        entered.set();release.wait(3);finished.set()
    task=asyncio.create_task(_joined(work))
    while not entered.is_set(): await asyncio.sleep(.001)
    try:
        for _ in range(3):
            task.cancel();await asyncio.sleep(.01)
            assert not task.done() and not finished.is_set()
    finally: release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert finished.is_set()


async def test_managed_family_supplies_host_factory(monkeypatch):
    from meecp.preparation_textgen import TextGenerationFamily
    from meecp import preparation_textgen_verifier as verifier
    seen={}
    async def verify(*args,**kwargs):seen.update(kwargs);return "report"
    monkeypatch.setattr(verifier,"verify_textgen_package",verify)
    assert await TextGenerationFamily(None).verify(None,None,None,None)=="report"
    assert seen["confinement_factory"]==confinement.TextgenProbeConfinementV1.from_host


@pytest.mark.parametrize("host",["0.0.0.0","::1","localhost","127.0.0.2"])
def test_pinned_bootstrap_rejects_nonexact_host_configuration(host):
    import json
    from meecp_textgen.__main__ import read_config
    from meecp_textgen.service import model_declaration
    read,write=os.pipe()
    try:
        os.write(write,json.dumps({"endpoint":{"host":host,"port":49001},"model":model_declaration()}).encode())
    finally: os.close(write)
    # The production reader owns/closes its private descriptor even on failure.
    with pytest.raises(ValueError): read_config(read)


def test_large_runtime_binding_does_not_use_descriptor_json_node_budget():
    entries=tuple(("/private/fixture/"+str(index),(1,index,0o100600,1,501,12,34,56)) for index in range(5000))
    first=confinement._records_digest(entries)
    assert len(first)==64
    assert confinement._records_digest((*entries[:-1],("/private/fixture/last",entries[-1][1])))!=first


@pytest.mark.parametrize("mutation",["package","unlisted","owner-helper","key"])
async def test_actual_native_task_rechecks_bound_metadata_before_creation(binding_fixture,monkeypatch,mutation):
    plan=captured(binding_fixture)
    package,descriptor,_,reaper,_=binding_fixture
    key=plan.workspace/"engine.key";key.write_bytes(b"synthetic");key.chmod(0o600)
    executable,args,cwd,env,guard=plan.launch(package,descriptor,reaper,role="engine",port=49001,
                                           engine_port=None,keyfile=key,config_fd=5)
    owner=ProcessOwner(reaper)
    original=owner._launch
    creations=[]
    async def forbidden(*args,**kwargs):creations.append(True);raise AssertionError("unexpected creation")
    monkeypatch.setattr(asyncio,"create_subprocess_exec",forbidden)
    async def delayed(*args,**kwargs):
        await asyncio.sleep(0)
        if mutation=="package": (package.root/"model.gguf").write_bytes(b"changed")
        elif mutation=="unlisted": (package.root/"new").write_bytes(b"changed")
        elif mutation=="owner-helper": owner.reaper=reaper.with_name("different")
        else:key.write_bytes(b"changed")
        return await original(*args,**kwargs)
    monkeypatch.setattr(owner,"_launch",delayed)
    from meecp.processes import ProcessError
    try:
        with pytest.raises((PreparationError,ProcessError)):
            await owner.launch(executable,args,cwd,env,admit=guard,expected_reaper=reaper,
                               owned_group=True,io_mode="fds",startup_timeout=2,stop_grace=.1)
        assert not creations and not owner.instances and not owner._launches
    finally:await owner.close()


@pytest.mark.parametrize("mutation",["runtime","missing","executable","hash","malformed"])
def test_managed_factory_rejects_unbound_host_toolchain(binding_fixture,monkeypatch,mutation):
    from meecp import preparation_builder,preparation_inputs,recipes
    package,descriptor,workspace,reaper,identity=binding_fixture
    records={"textgen-runtime/bin/textgen-"+role:{"sha256":identity,"executable":True}
             for role in ("engine","adapter")}
    runtime="c"*64
    if mutation=="missing": records.pop("textgen-runtime/bin/textgen-engine")
    elif mutation=="executable": records["textgen-runtime/bin/textgen-engine"]["executable"]=False
    elif mutation=="hash": records["textgen-runtime/bin/textgen-engine"]["sha256"]="f"*64
    elif mutation=="malformed": records["textgen-runtime/bin/textgen-engine"]={}
    monkeypatch.setattr(preparation_builder,"load_private_toolchain",lambda root:SimpleNamespace(identity=runtime,artifacts=records))
    monkeypatch.setattr(recipes,"_read",lambda *a:b"{}")
    monkeypatch.setattr(preparation_inputs,"parse_preparation_receipt",lambda data:SimpleNamespace(schema_version=2,
        inputs=SimpleNamespace(runtimes=[SimpleNamespace(name="private-python-toolchain",
            digest="d"*64 if mutation=="runtime" else runtime)])))
    with pytest.raises(PreparationError):
        confinement.TextgenProbeConfinementV1.from_host(package,descriptor,workspace,reaper)
    assert not list(workspace.iterdir())


@pytest.mark.parametrize("ending",["cancel","deadline"])
async def test_verifier_joins_factory_before_close_or_terminal_result(tmp_path,monkeypatch,ending):
    from meecp import preparation_textgen_verifier as verifier
    from test_preparation_textgen import descriptor,draft
    entered,release=threading.Event(),threading.Event()
    package=SimpleNamespace(root=tmp_path,digest="a"*64)
    async def validated(path):return package
    monkeypatch.setattr(verifier,"_validate_joined",validated)
    monkeypatch.setattr(verifier,"validate_model_package",lambda *a,**k:None)
    class Owner:
        reaper=tmp_path/"reaper"
        closed=False
        launched=False
        async def launch(self,*a,**k):self.launched=True;raise AssertionError("unexpected creation")
        async def close(self):self.closed=True
    owner=Owner()
    def factory(*args):entered.set();release.wait(3);return None
    task=asyncio.create_task(verifier.verify_textgen_package(tmp_path,descriptor(draft()),owner.reaper,
        tmp_path,owner=owner,confinement_factory=factory,timeout_seconds=.05 if ending=="deadline" else 2))
    while not entered.is_set():await asyncio.sleep(.001)
    try:
        if ending=="cancel":task.cancel()
        await asyncio.sleep(.1)
        assert not task.done() and not owner.closed and not owner.launched
    finally:release.set()
    with pytest.raises(asyncio.CancelledError if ending=="cancel" else PreparationError):await task
    assert owner.closed and not owner.launched
