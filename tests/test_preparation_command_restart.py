"""Actual held-host death and reopened SQLite command preparation recovery."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from meecp.imports import ImportStore
from meecp.preparation_command import CommandFamily, component_catalog
from meecp.preparation_command_owner import recover_command_ownership
from meecp.preparation_runner import PreparationRunner
from meecp.preparations import PreparationStore
from meecp.recipes import RecipeStore
from test_host_command import native, digest, pytestmark
from test_preparation_command_managed import intake
from test_preparation_command_measured import measured_admission


CHILD = '''import asyncio,os,sys,time
from pathlib import Path
from meecp.contracts import validate_package
from meecp.host_command import HostCommandAdmission
from meecp.imports import ImportStore
from meecp.preparation_command import CommandFamily,component_catalog
from meecp.preparation_command_owner import CommandPreparationOwner,CommandPreparationResourceAdmission
from meecp.preparations import PreparationStore
from meecp.recipes import RecipeStore
async def main():
 imports=ImportStore(Path(sys.argv[1]))
 recipes=RecipeStore(imports,component_catalog=component_catalog())
 store=PreparationStore(imports,recipes,families=(CommandFamily(sys.argv[4]),))
 job=store.get(sys.argv[2])
 descriptor,_,_=store.job_inputs(job["job_id"])
 workspace=Path(job["workspace"])
 resource=CommandPreparationResourceAdmission(Path(sys.argv[5]),sys.argv[6],sys.argv[7]) if len(sys.argv)>5 else None
 owner=CommandPreparationOwner(HostCommandAdmission(Path(sys.argv[3]),sys.argv[4]),store,job["job_id"],workspace,validate_package(workspace/"candidate"),descriptor,attempt_id="a"*32,check=lambda:None,deadline=time.monotonic()+10,resource_admission=resource)
 handle=await owner.session(owner.package)
 launched=asyncio.Event()
 original_read=os.read
 def observed_read(fd,size):
  value=original_read(fd,size)
  if value and any(process.stdout.fileno()==fd for process in owner.executor._native.values() if process.stdout and not process.stdout.closed): launched.set()
  return value
 os.read=observed_read
 task=asyncio.create_task(owner.invoke(handle,"run",{"ok":True},1))
 await asyncio.wait_for(launched.wait(),5)
 os.read=original_read
 os.write(1,b"R")
 await task
asyncio.run(main())
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("measured", [False, True])
async def test_actual_host_sigkill_reopened_store_retains_until_exact_native_receipt(tmp_path, native, measured_admission, measured):
    source = tmp_path / "restart-target.c"
    source.write_text('#include <signal.h>\n#include <unistd.h>\nint main(void) { alarm(4); '
        'if (fork()==0) { signal(SIGTERM,SIG_IGN); while(1) pause(); } '
        'write(1,"{\\"ok\\":true}",11); while(1) pause(); }\n')
    executable = tmp_path / "restart-target"
    compiled = subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2", str(source), "-o", str(executable)],
        capture_output=True, timeout=30)
    assert compiled.returncode == 0
    fixture = (native[0], native[1], executable)
    imports, store, factory = intake(tmp_path, fixture, "hang", create_owner=False)
    identifier = factory.args[2]
    state = imports.root
    job = store.get(identifier)
    workspace = Path(job["workspace"])
    imports.close()
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "backend")}
    extra = [str(measured_admission.helper), measured_admission.helper_sha256, measured_admission.platform_policy] if measured else []
    child = subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(state), identifier, str(native[1]), digest(native[1]), *extra],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
    reopened = None
    runner = None
    retained = None
    receipt = None
    try:
        os.set_blocking(child.stdout.fileno(), False)
        ready = b""
        async with asyncio.timeout(5):
            while not ready:
                try:
                    ready = os.read(child.stdout.fileno(), 1)
                except BlockingIOError:
                    pass
                assert child.poll() is None
                await asyncio.sleep(.005)
        assert ready == b"R"
        child.kill()  # Only this exact held host process, never discovered PIDs.
        await asyncio.to_thread(child.wait, 5)
        reopened = ImportStore(state)
        recipes = RecipeStore(reopened, component_catalog=component_catalog())
        store = PreparationStore(reopened, recipes, families=(CommandFamily(digest(native[1])),))
        metadata, = store.command_ownership(identifier)
        async with asyncio.timeout(5):
            while recover_command_ownership(workspace, metadata) is None:
                await asyncio.sleep(.01)
        receipt, = (workspace / ("command-probe-v4" if measured else "command-probe-v3")).glob("*.complete")
        offset = 360 if measured else 296
        assert receipt.read_bytes()[offset:offset+1] == b"C"
        retained = receipt.with_suffix(".held")
        receipt.rename(retained)
        reservations = set()

        async def reserve(job_id, policy):
            reservations.add(job_id)

        async def release(job_id, *, ownership_settled):
            assert ownership_settled
            reservations.discard(job_id)

        runner = PreparationRunner(store, tmp_path / "host", native[0], reserve_resources=reserve,
            release_resources=release)  # Recovery works with execution admission closed.
        await runner.recover()
        pending = store.get(identifier)
        assert pending["state"] == "interrupted" and pending["reservation_retained"]
        assert reservations == {identifier} and store.pending_ownership(identifier)
        assert workspace.exists() and reopened.inspect(job["binding"]["import_id"])["attachment"] is None
        retained.rename(receipt)
        retained = None
        await runner.recover()
        settled = store.get(identifier)
        assert settled["state"] == "interrupted" and settled["ownership_settled"] and not settled["reservation_retained"]
        assert not reservations and not store.pending_ownership(identifier) and not workspace.exists()
        assert reopened.inspect(job["binding"]["import_id"])["attachment"] is None
    finally:
        if child.poll() is None:
            child.kill()
        await asyncio.to_thread(child.wait, 5)
        child.stdout.close()
        if retained is not None and retained.exists():
            retained.rename(receipt)
        if runner is not None:
            await runner.recover()
            await runner.close()
        if reopened is not None:
            reopened.close()
