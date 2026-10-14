"""Installation retry through a real isolated host and owned native MCP app."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import uuid
from pathlib import Path

import pytest
from meecp.contracts import validate_package
from meecp.installation_retry import retry_installation
from meecp.supervisor import Supervisor
from test_meecp_host import finish, tool_call
from test_meecp_packages import edit_manifest, make_package
from test_pi_setup import native_probe_server as _probe_fixture
from test_preparation_processes import group_reaper


@pytest.fixture(scope="module")
def retry_probe(tmp_path_factory):
    return _probe_fixture.__wrapped__(tmp_path_factory)


def _failed_attempt(store, draft):
    # An immutable package plus a terminal failure is the retry boundary. The
    # retry itself, planning, probes, installation and gateway are all real.
    with store._transaction():
        current = store._get(draft["draft_id"], draft["revision"])
        current["operation_id"] = str(uuid.uuid4())
        current["operation_status"] = "failed"
        current["state"] = "installed"
        store._save(current)
    return store.inspect(draft["draft_id"])


async def test_retry_installs_same_native_package_once_without_pi_and_reaps(
    tmp_path, retry_probe, group_reaper, monkeypatch, capfd, caplog
):
    # Exercise the current ownership protocol instead of whichever older app
    # happens to be installed. An explicit installed-build acceptance override
    # still uses its selected binary without weakening receipt validation.
    reaper = Path(
        os.environ.get("MEECP_TEST_REAPER", str(group_reaper))
    ).resolve()
    assert reaper.is_file(), "installation_retry_reaper_unavailable"

    package = make_package(tmp_path / "package")
    shutil.copyfile(retry_probe, package / "bin/service")
    (package / "bin/service").chmod(0o700)
    edit_manifest(
        package,
        resources={"memory_mb": 64, "cpu_only": True, "gpu": False},
        configuration={
            "schema_version": 1,
            "fields": {"enabled": {"type": "boolean", "default": False}},
        },
    )
    sealed = validate_package(package)
    assert sealed.manifest.resources.gpu is False
    host_root = tmp_path.resolve() / "state"
    host = Supervisor(host_root, reaper)
    agent_calls = 0
    processes = []
    endpoints = []
    checks = {}

    async def forbid_agent(*args, **kwargs):
        nonlocal agent_calls
        agent_calls += 1
        raise AssertionError("installation retry must not invoke a setup model")

    monkeypatch.setattr(host.pi_setup, "_run_agent", forbid_agent)
    try:
        await host.initialize()
        draft = host.imports.create(
            package, name="Native retry fixture", intent="Public synthetic validator", start=True, start_policy="host"
        )
        draft = host.imports.revise(
            draft["draft_id"],
            draft["revision"],
            carry_attachment=True,
            requirements={"configuration": {"enabled": True}, "secret_references": {}},
        )
        original = _failed_attempt(host.imports, draft)
        first, duplicate = await asyncio.gather(
            *(
                asyncio.to_thread(retry_installation, host.imports, original["draft_id"], original["revision"])
                for _ in range(2)
            )
        )
        assert first["draft_id"] != original["draft_id"]
        assert first["draft_id"] == duplicate["draft_id"]
        assert first["operation_id"] is None
        assert first["attachment"]["digest"] == original["attachment"]["digest"] == sealed.digest
        assert all(
            first[field] == original[field] for field in ("name", "intent", "start", "start_policy", "requirements")
        )
        assert validate_package(first["attachment"]["path"]).digest == sealed.digest
        checks["fresh_draft_same_bytes_and_choices"] = True
        plans = [
            await host.plan_install_async({"import_id": first["draft_id"], "revision": first["revision"]})
            for _ in range(2)
        ]
        assert all(
            plan["package_digest"] == sealed.digest
            and plan["settings"]["configuration"] == {"enabled": True}
            and plan["start"] is True
            and plan["start_policy"] == "host"
            for plan in plans
        )
        applied, repeated = await asyncio.gather(*(host.apply_install(plan["plan_id"]) for plan in plans))
        assert applied["operation_id"] == repeated["operation_id"]
        operation = await finish(host, applied)
        assert operation.status == "completed", operation.error_code
        assert len(host.registry.list_slots()) == len(host.registry.list_operations()) == 1
        slot = host.registry.get_slot(operation.slot_id)
        assert slot.start_policy == "host" and slot.desired_state == "running"
        assert slot.name == original["name"] and slot.package_digest == sealed.digest
        assert host.package(slot).digest == sealed.digest
        installed = host.imports.inspect(first["draft_id"])
        assert installed["state"] == "installed" and installed["result_slot_id"] == slot.id
        assert installed["attachment"]["digest"] == sealed.digest
        assert host.imports.inspect(original["draft_id"])["operation_status"] == "failed"
        assert await tool_call(host, slot.id) == {"valid": True}
        last = retry_installation(host.imports, original["draft_id"], original["revision"])
        assert last["draft_id"] == first["draft_id"] and last["result_slot_id"] == slot.id
        assert len(host.registry.list_slots()) == 1 and len(host.imports.list()) == 2
        assert agent_calls == 0 and host.pi_setup.list()["jobs"] == [] and host.pi_setup.tasks == {}
        checks.update(
            {
                "duplicate_retry_and_apply_coalesced": True,
                "native_authenticated_mcp_passed": True,
                "zero_pi_jobs_and_model_calls": True,
                "installed_bytes_and_start_policy_preserved": True,
            }
        )
        endpoints = [plan["public_port"] for plan in plans]
        processes = [process for owner in host.owners.values() for process in owner.instances.values()]
        assert processes and slot.id in host.workers
    finally:
        await host.close()

    assert all(process.wrapper.returncode is not None and not process.ownership_lost for process in processes)
    assert all(not owner.instances for owner in host.owners.values())
    assert not host.workers and not host.residents and not host.reservations
    for port in endpoints:
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(("127.0.0.1", port)) != 0
    reopened = Supervisor(host_root, reaper)
    try:
        assert len(reopened.registry.list_slots()) == 1
        assert not reopened.ledger(operation.slot_id)
    finally:
        await reopened.close()
    checks["owned_processes_endpoints_and_host_lock_released"] = True
    assert caplog.text == "" and capfd.readouterr() == ("", "")
    report = os.environ.get("MEECP_RETRY_ACCEPTANCE_REPORT")
    if report:
        destination = Path(report)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "passed": True,
                    "checks": checks,
                    "package_digest": sealed.digest,
                    "reaper_sha256": hashlib.sha256(reaper.read_bytes()).hexdigest(),
                    "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "installed_slots": 1,
                    "pi_jobs": 0,
                    "model_calls": 0,
                },
                indent=2,
            )
            + "\n"
        )
        destination.chmod(0o600)
