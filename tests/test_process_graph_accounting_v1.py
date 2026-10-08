"""Exercise graph records through the real native ProcessOwner lifecycle."""
import asyncio
import hashlib
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from meecp.process_graph_accounting_v1 import FixedProcessGraphV1, GraphMemberV1
from meecp.processes import ProcessOwner, OwnershipLost, ProcessError
from test_meecp_processes import reaper

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def graph_inputs(tmp_path):
    target = tmp_path / "engine"
    built = subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", str(ROOT / "tests/fixtures/fixed_graph_target.c"),
        "-o", str(target)], capture_output=True, timeout=20)
    assert built.returncode == 0
    adapter = tmp_path / "adapter"
    shutil.copyfile(target, adapter); adapter.chmod(0o700)
    members = tuple(GraphMemberV1(role, path, hashlib.sha256(path.read_bytes()).hexdigest(), 16 * 1024**2)
                    for role, path in (("engine", target), ("adapter", adapter)))
    return members


async def launch(owner, member):
    return await owner.launch(member.executable, [], member.executable.parent, {}, graph_role=member.role,
                              owned_group=True, stop_grace=.01)


async def test_actual_two_member_foreground_graph_records_separate_terminal_maxima(reaper, graph_inputs):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs)
    owner = ProcessOwner(reaper, graph_accounting=graph, graph_admission_check=lambda: True)
    try:
        for member in graph_inputs:
            process = await launch(owner, member)
            assert await process.stdout.readline() == b"ready\n"
        assert graph.report().incomplete and not graph.report().ownership_settled
        with pytest.raises(ValueError, match="process_graph_contract_invalid"):
            await launch(owner, graph_inputs[0])
    finally:
        await owner.close()
    report = graph.report()
    assert report.ownership_settled and report.foreground_measurements_complete and not report.incomplete
    assert report.conservative_member_rss_max_sum_bytes == sum(m.terminal_foreground_rss_max_bytes for m in report.members)
    assert not report.hard_aggregate_enforced and not report.lifetime_descendant_coverage_complete
    assert not owner.instances


@pytest.mark.parametrize("mutation", ["before_hash", "before_spawn"])
async def test_executable_mutation_is_proven_nonlaunch(reaper, graph_inputs, monkeypatch, mutation):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs[:1])
    owner = ProcessOwner(reaper, graph_accounting=graph, graph_admission_check=lambda: True)
    path = graph_inputs[0].executable
    if mutation == "before_hash":
        path.write_bytes(path.read_bytes() + b"changed")
    else:
        original = owner._launch
        async def changed(*args):
            path.write_bytes(path.read_bytes() + b"changed")
            return await original(*args)
        monkeypatch.setattr(owner, "_launch", changed)
    try:
        with pytest.raises((ValueError, ProcessError)):
            await launch(owner, graph_inputs[0])
    finally:
        await owner.close()
    assert graph.report().members[0].state == "nonlaunch"
    assert graph.report().ownership_settled and not graph.report().foreground_measurements_complete
    assert not owner.instances


async def test_cancel_and_close_join_hash_worker_before_proving_nonlaunch(reaper, graph_inputs, monkeypatch):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs[:1])
    owner = ProcessOwner(reaper, graph_accounting=graph, graph_admission_check=lambda: True)
    entered, release = threading.Event(), threading.Event()
    original = graph.verify_executable
    def held(role):
        entered.set(); release.wait(2); return original(role)
    monkeypatch.setattr(graph, "verify_executable", held)
    task = asyncio.create_task(launch(owner, graph_inputs[0]))
    await asyncio.to_thread(entered.wait, 1)
    task.cancel(); await asyncio.sleep(.01); task.cancel()
    close = asyncio.create_task(owner.close())
    await asyncio.sleep(.02)
    assert not task.done() and not close.done() and not graph.report().ownership_settled
    release.set()
    assert isinstance((await asyncio.gather(task, return_exceptions=True))[0], asyncio.CancelledError)
    await close
    assert graph.report().members[0].state == "nonlaunch" and not owner.instances


async def test_repeat_cancel_during_native_ack_settles_existing_owned_process(reaper, graph_inputs, monkeypatch):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs[:1])
    owner = ProcessOwner(reaper, graph_accounting=graph, graph_admission_check=lambda: True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = owner._launch
    async def held(*args):
        process = await original(*args)
        assert await process.stdout.readline() == b"ready\n"
        entered.set(); await release.wait(); return process
    monkeypatch.setattr(owner, "_launch", held)
    task = asyncio.create_task(launch(owner, graph_inputs[0]))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel(); await asyncio.sleep(.01); task.cancel(); release.set()
    assert isinstance((await asyncio.gather(task, return_exceptions=True))[0], asyncio.CancelledError)
    await owner.close()
    assert graph.report().ownership_settled and graph.report().foreground_measurements_complete


async def test_exact_helper_loss_keeps_graph_unknown(reaper, graph_inputs):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs[:1])
    owner = ProcessOwner(reaper, graph_accounting=graph, graph_admission_check=lambda: True)
    process = await launch(owner, graph_inputs[0])
    assert await process.stdout.readline() == b"ready\n"
    process.wrapper.kill()  # Held wrapper only; no target lookup or signals.
    with pytest.raises(OwnershipLost):
        await process.wait()
    with pytest.raises(OwnershipLost):
        await owner.close()
    assert graph.report().ownership_lost and not graph.report().ownership_settled
    await asyncio.sleep(2.1)  # Fixture alarm bounds orphan lifetime, not proof.
    assert graph.report().members[0].state == "unknown"


def test_missing_member_and_identity_change_never_become_complete(graph_inputs):
    first = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs)
    second = FixedProcessGraphV1("a" * 64, "c" * 64, graph_inputs)
    assert first.contract_digest != second.contract_digest
    assert first.report().incomplete and not first.report().ownership_settled


async def test_legacy_nonmeasuring_wire_stays_explicitly_incomplete(reaper, graph_inputs):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs[:1])
    owner = ProcessOwner(reaper, graph_accounting=graph, graph_admission_check=lambda: True)
    member = graph_inputs[0]
    process = await owner.launch(member.executable, [], member.executable.parent, {}, graph_role=member.role)
    assert await process.stdout.readline() == b"ready\n"
    await owner.close()
    report = graph.report()
    assert report.ownership_settled and report.incomplete and not report.foreground_measurements_complete
    assert report.conservative_member_rss_max_sum_bytes is None


def test_oversized_member_total_is_not_published_as_a_valid_measurement(graph_inputs):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs)
    for index, member in enumerate(graph_inputs):
        identifier = str(index) * 32
        graph.reserve(member.role, identifier, member.executable)
        graph.record(identifier, "settled", 2**63 - 1)
    assert graph.report().ownership_settled and graph.report().incomplete
    assert graph.report().conservative_member_rss_max_sum_bytes is None


async def test_graph_hash_wait_preserves_expected_native_reaper_pin(reaper, graph_inputs, monkeypatch):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs[:1])
    owner = ProcessOwner(reaper, graph_accounting=graph, graph_admission_check=lambda: True)
    original = graph.verify_executable
    def change(role):
        original(role)
        owner.reaper = Path("/missing/changed-reaper")
    monkeypatch.setattr(graph, "verify_executable", change)
    member = graph_inputs[0]
    with pytest.raises(ProcessError, match="native_reaper_changed"):
        await owner.launch(member.executable, [], member.executable.parent, {}, graph_role=member.role,
                           expected_reaper=reaper)
    await owner.close()
    assert graph.report().members[0].state == "nonlaunch"


@pytest.mark.parametrize("change", ["revoked", "revision", "deadline"])
async def test_paused_hash_rechecks_current_authority_without_native_creation(reaper, graph_inputs, monkeypatch, change):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs[:1])
    state = {"authorized": True, "revision": 1}
    owner = ProcessOwner(reaper, graph_accounting=graph,
        graph_admission_check=lambda: state["authorized"] and state["revision"] == 1)
    entered, release = threading.Event(), threading.Event()
    original = graph.verify_executable
    def held(role):
        entered.set(); release.wait(2); original(role)
    monkeypatch.setattr(graph, "verify_executable", held)
    creations = []
    async def forbidden(*args, **kwargs):
        creations.append(True)
        raise AssertionError("unexpected_native_creation")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    member = graph_inputs[0]
    task = asyncio.create_task(owner.launch(member.executable, [], member.executable.parent, {},
        graph_role=member.role, startup_timeout=.03 if change == "deadline" else 1))
    await asyncio.to_thread(entered.wait, 1)
    if change == "revoked": state["authorized"] = False
    if change == "revision": state["revision"] = 2
    if change == "deadline": await asyncio.sleep(.05)
    release.set()
    with pytest.raises(ProcessError, match="process_graph_not_admitted"):
        await task
    await owner.close()
    assert not creations and graph.report().members[0].state == "nonlaunch"


@pytest.mark.parametrize("answer", [False, 1, "awaitable", "second-check", "closed"])
async def test_native_boundary_requires_exact_true_current_admission(reaper, graph_inputs, monkeypatch, answer):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs[:1])
    calls = 0
    async def accepted(): return True
    def admit():
        nonlocal calls
        calls += 1
        if answer == "second-check": return calls == 1
        if answer == "closed":
            owner._closed = True
            return True
        return accepted() if answer == "awaitable" else answer
    owner = ProcessOwner(reaper, graph_accounting=graph, graph_admission_check=admit)
    creations = []
    async def forbidden(*args, **kwargs):
        creations.append(True)
        raise AssertionError("unexpected_native_creation")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(ProcessError):
        await launch(owner, graph_inputs[0])
    await owner.close()
    assert not creations and graph.report().members[0].state == "nonlaunch"
    if answer == "second-check": assert calls == 2


def test_graph_constructor_requires_live_trusted_callback(reaper, graph_inputs):
    graph = FixedProcessGraphV1("a" * 64, "b" * 64, graph_inputs)
    with pytest.raises(ProcessError, match="process_graph_admission_required"):
        ProcessOwner(reaper, graph_accounting=graph)


@pytest.mark.parametrize("change", ["revoked", "revision"])
async def test_plain_owner_scheduled_native_task_rechecks_real_admit(reaper, graph_inputs, monkeypatch, change):
    owner = ProcessOwner(reaper)
    state = {"authorized": True, "revision": 1}
    entered, release = asyncio.Event(), asyncio.Event()
    original = owner._launch
    async def held(*args):
        entered.set(); await release.wait(); return await original(*args)
    monkeypatch.setattr(owner, "_launch", held)
    creations, nonlaunch = [], []
    async def forbidden(*args, **kwargs):
        creations.append(True)
        raise AssertionError("unexpected_native_creation")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    member = graph_inputs[0]
    task = asyncio.create_task(owner.launch(member.executable, [], member.executable.parent, {},
        admit=lambda: state["authorized"] and state["revision"] == 1,
        nonlaunch_callback=lambda: nonlaunch.append(True)))
    await entered.wait()
    if change == "revoked": state["authorized"] = False
    else: state["revision"] = 2
    release.set()
    with pytest.raises(ProcessError): await task
    await owner.close()
    assert not creations and nonlaunch == [True] and not owner.instances


@pytest.mark.parametrize("answer", [False, 1, "awaitable", "exception"])
async def test_plain_owner_admit_requires_exact_sync_true(reaper, graph_inputs, answer):
    owner = ProcessOwner(reaper)
    async def yes(): return True
    def admit():
        if answer == "exception": raise ValueError("fixed_test_error")
        return yes() if answer == "awaitable" else answer
    proof = []
    member = graph_inputs[0]
    with pytest.raises(ProcessError):
        await owner.launch(member.executable, [], member.executable.parent, {}, admit=admit,
                           nonlaunch_callback=lambda: proof.append(True))
    await owner.close()
    assert proof == [True] and not owner.instances


async def test_plain_owner_true_admit_uses_existing_actual_native_path(reaper, graph_inputs):
    owner = ProcessOwner(reaper)
    member = graph_inputs[0]
    process = await owner.launch(member.executable, [], member.executable.parent, {}, admit=lambda: True)
    assert await process.stdout.readline() == b"ready\n"
    await owner.close()
    assert not owner.instances
