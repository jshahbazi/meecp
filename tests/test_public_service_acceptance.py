"""Public service driver preflight and compiled shipped native guard contracts."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from test_recipe_native_management_acceptance import private_json, file_identity

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_phase3_public_service as driver


def policy():
    refs = [dict(zip(("id", "version", "digest"), item)) for item in driver.REFERENCES]
    return {"schema_version": 1, "policy_id": "phase3-v1", "preparation": {"families": [refs[-1]],
        "recipe_component_sets": [refs], "resolution_candidate_kinds": []}, "command": None,
        "agent": {"components": [], "providers": [], "unlocked_resolution": False}}


@pytest.mark.parametrize("fault", [None, "closed", "missing_head", "missing_leaf", "changed_digest", "changed_version", "extra_leaf", "serialized_triples", "bool_schema"])
def test_exact_shipped_service_policy_required(tmp_path, fault):
    raw = policy()
    if fault == "closed": raw["preparation"] = None
    elif fault == "missing_head": raw["preparation"]["families"] = []
    elif fault == "missing_leaf": raw["preparation"]["recipe_component_sets"][0].pop(0)
    elif fault == "changed_digest": raw["preparation"]["recipe_component_sets"][0][0]["digest"] = "0" * 64
    elif fault == "changed_version": raw["preparation"]["recipe_component_sets"][0][0]["version"] = "1.0.1"
    elif fault == "extra_leaf": raw["preparation"]["recipe_component_sets"][0].append(raw["preparation"]["families"][0])
    elif fault == "serialized_triples": raw["preparation"]["recipe_component_sets"] = [[list(item) for item in driver.REFERENCES]]
    elif fault == "bool_schema": raw["schema_version"] = True
    path = tmp_path / driver.POLICY_PATH; path.parent.mkdir(parents=True)
    private_json(path, raw)
    if fault:
        with pytest.raises(driver.common.AcceptanceError): driver.active_policy(tmp_path)
    else:
        assert driver.active_policy(tmp_path) == driver.common.digest(path)
        from meecp.preparation_service import references
        assert tuple(ref.key for ref in references()) == driver.REFERENCES


def test_native_diagnostic_drops_untrusted_payload():
    raw = {"stage": "service_review_only", "passed": False, "error_code": "service_probe_failed",
        "prompt": "must not retain", "secret": "must not retain", "exception": "must not retain"}
    assert driver.native_diagnostic(raw) == {"stage": "service_review_only", "passed": False,
        "error_code": "service_probe_failed", "native_service_selection": False, "review_created_no_jobs": False}
    raw.update(stage="private-content", error_code="unknown-content")
    assert driver.native_diagnostic(raw)["stage"] == "unknown" and driver.native_diagnostic(raw)["error_code"] is None


async def test_start_always_uses_ordinary_launcher(monkeypatch):
    observed = []
    async def start(self, *args, enabled): observed.append(enabled); return "owned"
    monkeypatch.setattr(driver.common.Harness, "start", start)
    harness = driver.PublicServiceHarness(Path("/unused"), Path("/unused"), Path("/unused"), {}, report={})
    assert await harness.start(None, None, None, enabled=True) == "owned"
    assert observed == [False]


async def test_fixture_validates_actual_authenticated_loopback_protocol():
    import asyncio
    async with driver.protocol_fixture() as (server, port, token, counts):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"model": "synthetic-model", "stream": False, "messages": [{"role": "user", "content": "Hi"}]}).encode()
        writer.write(b"POST /v1/chat/completions HTTP/1.1\r\nAuthorization: Bearer " + token.encode()
            + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain(); response = await reader.read()
        assert response.startswith(b"HTTP/1.1 200") and counts == {"authenticated_requests": 1}
        writer.close(); await writer.wait_closed(); assert server.is_serving()
    assert not server.is_serving()


def context(tmp_path):
    workspace = tmp_path.resolve() / ("meecp-native-onboarding-" + str(uuid.uuid4()))
    root = workspace / "service_guard"; state, home, temporary = (root / name for name in ("state", "home", "tmp"))
    for path in (workspace, root, state, home, temporary): path.mkdir(mode=0o700)
    manager = state / "manager-connection.json"; private_json(manager, {"synthetic": True})
    nonce = str(uuid.uuid4()); marker = state / ("native-service-" + nonce + ".json")
    proof = {"schema_version": 1, "fixture_nonce": nonce, "expires_at": int(time.time()) + 120, "manager_file": file_identity(manager)}
    fixture, output = root / "case.json", root / "receipt.json"
    case = {"schema_version": 1, "fixture_nonce": nonce, "state_dir": str(state), "output_path": str(output),
        "service": {"name": "Synthetic", "model": "synthetic-model", "port": 12345, "secret_reference": str(uuid.uuid4())},
        "answers_by_label": {}}
    return state, home, temporary, manager, marker, proof, fixture, output, case


def run(binary, fixture, home, temporary):
    result = subprocess.run([str(binary), "--service-native-acceptance", str(fixture)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary)}, capture_output=True, timeout=10)
    assert result.returncode == 1 and result.stdout == b"service_native_acceptance_failed\n" and result.stderr == b""


@pytest.mark.parametrize("fault", ["unknown_field", "secret_value", "bad_port", "bad_model", "bad_reference", "expired", "wrong_nonce",
    "manager_replaced", "fixture_mode", "marker_hardlink", "replay", "existing_output", "output_link"])
def test_compiled_service_guard_denies_before_claim(native_recipe_driver, tmp_path, fault):
    state, home, temporary, manager, marker, proof, fixture, output, case = context(tmp_path)
    if fault == "unknown_field": case["source_directory"] = str(home)
    elif fault == "secret_value": case["service"]["credential"] = "synthetic-only"
    elif fault == "bad_port": case["service"]["port"] = 65536
    elif fault == "bad_model": case["service"]["model"] = "invalid model"
    elif fault == "bad_reference": case["service"]["secret_reference"] = "invalid"
    elif fault == "expired": proof["expires_at"] = int(time.time()) - 1
    elif fault == "wrong_nonce": proof["fixture_nonce"] = str(uuid.uuid4())
    private_json(marker, proof); private_json(fixture, case)
    claim = marker.with_suffix(".claimed")
    if fault == "manager_replaced":
        replacement = state / "replacement.json"; private_json(replacement, {"synthetic": True}); replacement.replace(manager)
    elif fault == "fixture_mode": fixture.chmod(0o644)
    elif fault == "marker_hardlink": os.link(marker, state / "duplicate.json")
    elif fault == "replay": private_json(claim, {"consumed": True})
    elif fault == "existing_output": private_json(output, {"existing": True})
    elif fault == "output_link": output.symlink_to(state / "absent")
    before_claim = claim.read_bytes() if claim.exists() else None
    before_output = output.read_bytes() if output.exists() else None
    run(native_recipe_driver, fixture, home, temporary)
    assert (claim.read_bytes() if claim.exists() else None) == before_claim
    assert (output.read_bytes() if output.exists() else None) == before_output


def test_valid_service_context_claims_once_before_invalid_manager(native_recipe_driver, tmp_path):
    state, home, temporary, manager, marker, proof, fixture, output, case = context(tmp_path)
    private_json(marker, proof); private_json(fixture, case)
    run(native_recipe_driver, fixture, home, temporary)
    assert marker.with_suffix(".claimed").read_bytes() == b"claimed\n"
    receipt = json.loads(output.read_bytes())
    assert receipt["passed"] is False and receipt["stage"] == "connection" and receipt["routes"] == []
    before = file_identity(output)
    run(native_recipe_driver, fixture, home, temporary)
    assert file_identity(output) == before


async def test_actual_parent_service_intake_obeys_closed_host_gate(native_recipe_driver, tmp_path):
    """Full production parent callbacks reach real owner routes, never a probe."""
    import asyncio
    import uvicorn
    from meecp.supervisor import Supervisor, management_app, HostServer, bind_listener
    from meecp_local_transport import bind_private_listener
    state, home, temporary, manager, marker, proof, fixture, output, case = context(tmp_path)
    manager.unlink()  # Only the synthetic placeholder created by this fixture.
    host = Supervisor(state, tmp_path / "unused-reaper")
    listener = bind_listener(); private = bind_private_listener(state, "manager")
    port = listener.getsockname()[1]; token = "synthetic-service-owner-token-value"
    server = HostServer(uvicorn.Config(management_app(host, token, port), access_log=False,
        log_config=None, log_level="critical", timeout_graceful_shutdown=2))
    server_task = asyncio.create_task(server.serve(sockets=[listener, private]))
    process = None
    try:
        async with asyncio.timeout(5):
            while not server.started: await asyncio.sleep(0.01)
        secret = host.secrets.create("Synthetic", "synthetic-only-service-value")
        private_json(manager, {"api_version": 1, "endpoint": f"http://127.0.0.1:{port}", "token": token,
            "supervisor_pid": os.getpid()})
        proof["manager_file"] = file_identity(manager)
        case["service"]["secret_reference"] = secret["secret_id"]
        private_json(marker, proof); private_json(fixture, case)
        process = await asyncio.create_subprocess_exec(str(native_recipe_driver), "--service-native-acceptance", str(fixture),
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(35): printed, error = await process.communicate()
        assert process.returncode == 1 and printed == b"service_native_acceptance_failed\n" and error == b""
        receipt = json.loads(output.read_bytes())
        assert receipt["native_service_selection"] is True and receipt["passed"] is False
        assert receipt["stage"] == "preparation_review"
        assert "POST v1/imports/service" in receipt["routes"] and "POST v1/preparations/start" not in receipt["routes"]
        assert len(host.imports.list()) == 1 and host.imports.list()[0]["source"]["kind"] == "service_connection"
        assert not host.preparations.list() and not host.registry.list_operations()
    finally:
        if process is not None and process.returncode is None: process.kill(); await process.wait()
        server.should_exit = True; await server_task
        await host.close(); listener.close(); private.close()
