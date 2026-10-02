"""Durable, explicit JevK5 endpoint transfer with retained recovery artifacts."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
import uuid

import httpx
from mcpme_local_transport import private_transport, socket_path

from .contracts import validate_package
from .private import check_private_file, ensure_private_directory, read_private_json, snapshot_package, write_private_json
from .registry import RegistryError


def failure(code):
    from .supervisor import HostError
    return HostError(code, 409)


def record_path(host, identity):
    if str(uuid.UUID(identity)) != identity:
        raise failure("invalid_transfer")
    return host.root / "transfers" / (identity + ".json")


def inspect_transfer(host, identity):
    record = read_private_json(record_path(host, identity))
    return {key: value for key, value in record.items() if key not in {"credential_digest"}}


def save(host, record):
    write_private_json(record_path(host, record["transfer_id"]), record)


def check_legacy_contract(package, legacy_app):
    # This compatibility exception is pinned to the host's original JevK5
    # contract. Candidate code is never imported or executed during planning.
    contract = json.loads(Path(__file__).with_name("legacy_jevk5_contract.json").read_bytes())
    manifest = package.manifest
    model = manifest.model.model_dump() if manifest.model is not None else {}
    if (manifest.package_id != contract["package_id"] or manifest.profile != "model-service"
            or {key: model.get(key) for key in contract["model"]} != contract["model"]
            or model.get("parameters") != contract["parameters"]
            or [tool.mcp_schema() for tool in manifest.tools] != contract["tools"]):
        raise failure("legacy_package_contract_mismatch")
    try:
        old_model = json.loads((legacy_app / "Contents/Resources/model-manifest.json").read_bytes())
        if {key: old_model.get(key) for key in contract["model"]} != contract["model"]:
            raise failure("legacy_model_identity_mismatch")
    except (OSError, ValueError):
        raise failure("legacy_model_identity_mismatch") from None


def legacy_connection(record):
    value = read_private_json(Path(record["legacy_state"]) / "connection.json")
    if value.get("endpoint") != "http://127.0.0.1:19385" or not isinstance(value.get("token"), str) or len(value["token"]) < 32:
        raise failure("invalid_legacy_connection")
    if record.get("credential_digest") and hashlib.sha256(value["token"].encode()).hexdigest() != record["credential_digest"]:
        raise failure("legacy_connection_changed")
    return value


def acquire_legacy_lock(record):
    path = Path(record["legacy_state"]) / "backend.lock"
    check_private_file(path)
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise failure("legacy_owner_conflict") from None


def retain_legacy_lock(host, record):
    descriptor = acquire_legacy_lock(record)
    try:
        host.retain_legacy_lock(record["slot_id"], descriptor)
    except BaseException:
        os.close(descriptor)
        raise


async def _native(arguments):
    process = await asyncio.create_subprocess_exec(*arguments, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
    try:
        async with asyncio.timeout(35):
            output, _ = await process.communicate()
        if len(output) > 65536 or process.returncode:
            raise failure("legacy_handoff_failed")
        value = json.loads(output)
        if not isinstance(value, dict) or value.get("error"):
            raise failure("legacy_handoff_failed")
        return value
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def native_handoff(action: str, app: Path, *, login_enabled=False, login_disabled_confirmed=False):
    # Only this host's known legacy build may perform ServiceManagement calls.
    # The selected original/recovery app's code must match the build receipt.
    root = Path(__file__).resolve().parents[2]
    candidates = [root / "bin/mcpme-handoff", root / "runtime/mcpme-handoff"]
    helper = next((path for path in candidates if path.is_file()), None)
    if helper is None:
        raise failure("migration_helper_unavailable")
    receipt_path = next((path for path in (root / "legacy-handoff.json", root / "runtime/legacy-handoff.json")
                         if path.is_file()), None)
    if receipt_path is None:
        raise failure("legacy_handoff_build_required")
    receipt = json.loads(receipt_path.read_bytes())
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    name = info.get("CFBundleExecutable")
    if not isinstance(name, str) or Path(name).name != name or info.get("CFBundleIdentifier") != receipt["bundle_identifier"]:
        raise failure("invalid_legacy_app")
    executable = app / "Contents/MacOS" / name
    if executable.resolve() != executable or hashlib.sha256(executable.read_bytes()).hexdigest() != receipt["executable_sha256"]:
        raise failure("legacy_handoff_build_required")
    login = await _native([str(executable), "--mcpme-handoff", "login-status"])
    absent_confirmed = login.get("login_status") == "not_found" and login_disabled_confirmed is True
    if login.get("login_status") not in {"enabled", "not_registered"} and not absent_confirmed:
        raise failure("legacy_login_state_unknown")
    login_source = "owner_confirmed_disabled" if absent_confirmed else "service_management"
    status = await _native([str(helper), "--legacy-status"])
    if len(status["running"]) > 1:
        raise failure("ambiguous_legacy_app")
    if action == "status":
        return {"running": bool(status["running"]), "login_enabled": login["login_status"] == "enabled",
                "login_source": login_source}
    if action == "stop":
        if not absent_confirmed:
            disabled = await _native([str(executable), "--mcpme-handoff", "login-disable"])
            if disabled.get("login_status") != "not_registered":
                raise failure("legacy_login_disable_failed")
        for running in status["running"]:
            await _native([str(helper), "--legacy-stop", "--pid", str(running["pid"]),
                           "--bundle-path", running["bundle_path"]])
        stopped = await _native([str(helper), "--legacy-status"])
        return {"stopped": not stopped["running"], "login_disabled": True}
    if action == "restore":
        if login_enabled:
            enabled = await _native([str(executable), "--mcpme-handoff", "login-enable"])
            if enabled.get("login_status") != "enabled":
                raise failure("legacy_login_restore_failed")
        process = await asyncio.create_subprocess_exec("/usr/bin/open", "-g", str(app),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        async with asyncio.timeout(10):
            code = await process.wait()
        return {"restored": code == 0}
    raise failure("invalid_handoff_action")


async def plan_transfer(host, payload):
    # Package validation/snapshotting suspend for substantial time. Serialize
    # planners so they cannot both miss the durable duplicate check.
    if not hasattr(host, "migration_plan_lock"):
        host.migration_plan_lock = asyncio.Lock()
    async with host.migration_plan_lock:
        return await _plan_transfer(host, payload)


async def _plan_transfer(host, payload):
    required = {"package", "legacy_app", "legacy_state"}
    if (set(payload) - {"legacy_login_disabled"} != required
            or any(not isinstance(payload.get(key), str) for key in required)
            or ("legacy_login_disabled" in payload and payload["legacy_login_disabled"] is not True)):
        raise failure("invalid_migration_plan")
    legacy_app = Path(payload["legacy_app"]).absolute()
    if legacy_app.is_symlink() or legacy_app.resolve() != legacy_app:
        raise failure("invalid_legacy_app")
    try:
        info = plistlib.loads((legacy_app / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != "com.jshahbazi.jevk5":
            raise ValueError()
    except Exception:
        raise failure("invalid_legacy_app") from None
    legacy_state = ensure_private_directory(Path(payload["legacy_state"]).absolute())
    package = await asyncio.to_thread(validate_package, Path(payload["package"]))
    if package.manifest.package_id != "local.jevk5" or package.manifest.profile != "model-service":
        raise failure("invalid_migration_package")
    check_legacy_contract(package, legacy_app)
    # One unfinished transfer per legacy installation, including a committed one.
    transfer_directory = ensure_private_directory(host.root / "transfers")
    for path in transfer_directory.glob("*.json"):
        existing = read_private_json(path)
        if existing.get("legacy_state") == str(legacy_state) and existing.get("stage") != "rolled_back":
            if existing.get("package_digest") == package.digest:
                return inspect_transfer(host, existing["transfer_id"])
            raise failure("migration_already_planned")
    status = await native_handoff("status", legacy_app,
                                  login_disabled_confirmed=payload.get("legacy_login_disabled") is True)
    if type(status.get("login_enabled")) is not bool:
        raise failure("legacy_login_state_unknown")
    identity, slot_id = str(uuid.uuid4()), str(uuid.uuid4())
    snapshot = await asyncio.to_thread(snapshot_package, package, host.root / "staging" / identity / "package")
    record = {"transfer_id": identity, "slot_id": slot_id, "stage": "staged", "status": "planned",
              "legacy_app": str(legacy_app), "legacy_state": str(legacy_state),
              "login_enabled": status["login_enabled"], "login_source": status.get("login_source", "service_management"),
              "legacy_running": bool(status.get("running")),
              "package_path": str(snapshot.root), "package_digest": snapshot.digest,
              "public_port": 19385, "start_policy": "host", "error_code": None}
    connection = legacy_connection(record)
    record["credential_digest"] = hashlib.sha256(connection["token"].encode()).hexdigest()
    record["legacy_paused"] = False
    if record["legacy_running"]:
        try:
            transport = private_transport(socket_path(legacy_state, "jevk5"))
        except (OSError, ValueError):
            # The original bundle has no authenticated server transport. It
            # must be quit before planning, or replaced by the updated recovery
            # bundle. Never send its persistent token to an unverified port.
            raise failure("legacy_transport_upgrade_required") from None
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False, transport=transport) as client:
            try:
                response = await client.get(connection["endpoint"] + "/api/status",
                    headers={"Authorization": "Bearer " + connection["token"]})
                value = response.json()
                if response.status_code != 200 or type(value.get("paused")) is not bool:
                    raise failure("legacy_status_unavailable")
                record["legacy_paused"] = value["paused"]
            except Exception:
                raise failure("legacy_status_unavailable") from None
    save(host, record)
    return {**inspect_transfer(host, identity), "package": package.manifest.model_dump(),
            "effects": ["stop_legacy_app", "retire_legacy_login", "retain_legacy_lock", "preserve_port_and_invocation_token",
                        "start_and_verify_model", "preserve_legacy_bundle_and_state"]}


async def apply_transfer(host, identity):
    record = read_private_json(record_path(host, identity))
    if record["status"] in {"running", "completed", "recovery_required"}:
        return inspect_transfer(host, identity)
    if record["stage"] != "staged":
        raise failure("migration_finalized")
    # No await between durable submission and task registration; duplicate calls
    # observe this intent and recover the same transfer rather than resubmitting.
    record["status"] = "running"
    save(host, record)
    task = asyncio.create_task(execute_transfer(host, record))
    host.track(task)
    return inspect_transfer(host, identity)


async def verify_and_commit(host, record, expected_sha256):
    connection = legacy_connection(record)
    async with httpx.AsyncClient(timeout=150, trust_env=False, follow_redirects=False,
            transport=private_transport(socket_path(host.root, record["slot_id"]))) as client:
        response = await client.post(connection["endpoint"] + "/api/decide",
            headers={"Authorization": "Bearer " + connection["token"]}, json={
                "state": "Synthetic migration fixture: the switch is on.",
                "question": {"type": "noul", "instructions": "Is the switch on?"}})
        if response.status_code != 200 or response.json().get("model", {}).get("sha256") != expected_sha256:
            raise failure("migration_smoke_failed")
    if record.get("legacy_paused"):
        await host.control(record["slot_id"], "stop", migration_internal=True)
    if record["login_enabled"]:
        root = Path(__file__).resolve().parents[2]
        app = next((p for p in root.parents if p.suffix == ".app"), None)
        if app is None:
            raise failure("host_login_transfer_requires_app")
        enabled = await _native([str(app / "Contents/MacOS/mcpme"), "--host-login-enable"])
        if enabled.get("login_status") != "enabled":
            raise failure("host_login_transfer_requires_approval")
    record.update(stage="committed", status="completed", error_code=None)
    host.migration_blocked.discard(record["slot_id"])
    save(host, record)


async def block_transfer(host, record, error):
    record["status"] = "recovery_required"
    record["error_code"] = getattr(error, "code", "migration_interrupted")
    host.migration_blocked.add(record["slot_id"])
    save(host, record)
    with contextlib.suppress(Exception):
        await host.stop_gateway(record["slot_id"])
    with contextlib.suppress(RegistryError):
        slot = host.registry.get_slot(record["slot_id"])
        if slot.error_code != "ownership_lost":
            host.registry.update_slot(slot.id, observed_state="failed", error_code="migration_recovery_required")


async def execute_transfer(host, record):
    descriptor = None
    try:
        package = await asyncio.to_thread(validate_package, Path(record["package_path"]))
        if package.digest != record["package_digest"]:
            raise failure("package_digest_mismatch")
        connection = legacy_connection(record)
        stopped = await native_handoff("stop", Path(record["legacy_app"]),
                                      login_disabled_confirmed=record.get("login_source") == "owner_confirmed_disabled")
        if stopped.get("stopped") is not True or stopped.get("login_disabled") is not True:
            raise failure("legacy_stop_incomplete")
        record["stage"] = "old_stopped"
        save(host, record)
        async with asyncio.timeout(15):
            while descriptor is None:
                try:
                    descriptor = acquire_legacy_lock(record)
                except Exception:
                    await asyncio.sleep(0.2)
        host.retain_legacy_lock(record["slot_id"], descriptor)
        descriptor = None
        plan = await host.plan_install_async({"package": record["package_path"], "name": "JevK5 Judge",
                                  "start_policy": record["start_policy"], "start": True},
                                 slot_id=record["slot_id"], public_port=19385,
                                 invocation_token=connection["token"], legacy_jev=True)
        record["plan_id"] = plan["plan_id"]
        record["stage"] = "endpoint_acquired"
        save(host, record)
        operation = await host.apply_install(plan["plan_id"])
        record["operation_id"] = operation["operation_id"]
        save(host, record)
        async with asyncio.timeout(600):
            while True:
                operation = host.registry.get_operation(record["operation_id"])
                if operation.status == "completed":
                    break
                if operation.status in {"failed", "canceled", "recovery_required"}:
                    raise failure("migration_activation_failed")
                await asyncio.sleep(0.2)
        # The record is committed only after a synthetic decision traverses the
        # preserved public endpoint. No private user evidence enters this probe.
        await verify_and_commit(host, record, package.manifest.model.sha256)
    except BaseException as error:
        # Keep all package/data artifacts; close candidate admission. Uncertain
        # ownership retains the slot reservation and can never relaunch legacy.
        await block_transfer(host, record, error)
        if record["stage"] == "old_stopped" and not host.ledger(record["slot_id"]):
            held = host.legacy_locks.pop(record["slot_id"], None)
            if held is not None:
                os.close(held)
        if isinstance(error, asyncio.CancelledError):
            raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def recover_transfer(host, identity):
    """Explicit forward recovery of the selected generation, preserving its data."""
    record = read_private_json(record_path(host, identity))
    if record["status"] in {"running", "completed"}:
        return inspect_transfer(host, identity)
    if record["status"] != "recovery_required" or record["stage"] not in {"endpoint_acquired", "committed"}:
        raise failure("pre_exposure_use_rollback")
    slot = host.registry.get_slot(record["slot_id"])
    if host.ledger(slot.id) or slot.error_code == "ownership_lost":
        raise failure("ownership_reconciliation_required")
    if not host.activation(slot.id).get("exposure_permitted"):
        raise failure("pre_exposure_use_rollback")
    if any(op.slot_id == slot.id and op.action == "install" for op in host.registry.list_unfinished_operations()):
        raise failure("installation_recovery_required")
    if slot.public_port != 19385 or slot.package_digest != record["package_digest"]:
        raise failure("migration_registry_mismatch")
    record["status"] = "running"
    save(host, record)
    async def resume():
        try:
            legacy_connection(record)
            if slot.id not in host.legacy_locks:
                retain_legacy_lock(host, record)
            package = await asyncio.to_thread(host.package, slot)
            await host.control(slot.id, "start", migration_internal=True)
            await verify_and_commit(host, record, package.manifest.model.sha256)
        except BaseException as error:
            await block_transfer(host, record, error)
            if isinstance(error, asyncio.CancelledError):
                raise
    host.track(asyncio.create_task(resume()))
    return inspect_transfer(host, identity)


async def initialize_transfers(host):
    host.migration_blocked = set()
    directory = host.root / "transfers"
    if not directory.exists():
        return
    ensure_private_directory(directory)
    for path in directory.glob("*.json"):
        record = read_private_json(path)
        if record["stage"] in {"rolled_back", "staged"} and record["status"] != "running":
            continue
        try:
            if record["stage"] != "committed" or record["status"] != "completed":
                raise failure("migration_recovery_required")
            slot = host.registry.get_slot(record["slot_id"])
            if slot.public_port != 19385 or slot.package_digest != record["package_digest"]:
                raise failure("migration_registry_mismatch")
            retain_legacy_lock(host, record)
        except Exception as error:
            host.migration_blocked.add(record["slot_id"])
            record.update(status="recovery_required", error_code=getattr(error, "code", "migration_recovery_required"))
            save(host, record)
            with contextlib.suppress(RegistryError):
                host.registry.update_slot(record["slot_id"], observed_state="failed", error_code=record["error_code"])


async def wait_legacy_restored(record, *, timeout=180):
    """Authenticate the restored backend and confirm its saved pause state."""
    connection = legacy_connection(record)
    headers = {"Authorization": "Bearer " + connection["token"]}
    paused = record.get("legacy_paused") is True
    pause_sent = False
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    # The restored app may not have created its socket yet.
                    transport = private_transport(socket_path(Path(record["legacy_state"]), "jevk5"))
                    async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False, transport=transport) as client:
                        response = await client.get(connection["endpoint"] + "/api/status", headers=headers)
                        if response.status_code == 200:
                            value = response.json()
                            if isinstance(value, dict) and type(value.get("paused")) is bool:
                                if paused and not pause_sent:
                                    response = await client.post(connection["endpoint"] + "/api/control",
                                                                 headers=headers, json={"action": "pause"})
                                    if response.status_code != 200:
                                        raise failure("legacy_pause_restore_failed")
                                    pause_sent = True
                                elif value["paused"] == paused and (paused or value.get("ready") is True):
                                    return
                except (httpx.HTTPError, OSError, ValueError):
                    pass
                await asyncio.sleep(0.2)
    except TimeoutError:
        raise failure("legacy_restore_timeout") from None


async def rollback_transfer(host, identity):
    record = read_private_json(record_path(host, identity))
    if record["stage"] == "committed":
        raise failure("committed_migration_requires_restore_plan")
    if record["status"] == "running":
        raise failure("operation_in_progress")
    if record["stage"] == "rolled_back":
        return inspect_transfer(host, identity)
    slot_id = record["slot_id"]
    activation_path = host.slot_directory(slot_id) / "activation.json"
    if activation_path.exists() and read_private_json(activation_path).get("exposure_permitted"):
        raise failure("exposed_migration_requires_restore_plan")
    # Serialize rollback with apply/recover before the first suspension.
    record["status"] = "running"
    save(host, record)
    try:
        await host.stop_gateway(slot_id)
        if host.ledger(slot_id):
            raise failure("ownership_reconciliation_required")
        with contextlib.suppress(RegistryError):
            host.registry.update_slot(slot_id, desired_state="stopped", observed_state="stopped")
            host.registry.discard_slot(slot_id)
        listener = host.listeners.pop(slot_id, None)
        if listener:
            listener.close()
        plan_listener = host.plan_listeners.pop(record.get("plan_id"), None)
        if plan_listener:
            plan_listener.close()
        descriptor = host.legacy_locks.pop(slot_id, None)
        if descriptor is not None:
            os.close(descriptor)
        # Prove both resources are currently free without evicting an unknown owner.
        descriptor = acquire_legacy_lock(record)
        from .supervisor import bind_listener
        try:
            listener = bind_listener(19385)
            listener.close()
        finally:
            os.close(descriptor)
        restored = await native_handoff("restore", Path(record["legacy_app"]), login_enabled=record["login_enabled"],
                                       login_disabled_confirmed=record.get("login_source") == "owner_confirmed_disabled")
        if restored.get("restored") is not True:
            raise failure("legacy_restore_failed")
        await wait_legacy_restored(record)
        record.update(stage="rolled_back", status="completed", error_code=None)
        save(host, record)
        host.migration_blocked.discard(slot_id)
        return inspect_transfer(host, identity)
    except BaseException as error:
        await block_transfer(host, record, error)
        raise
