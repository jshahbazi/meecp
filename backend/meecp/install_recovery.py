"""First-install interruption recovery without replaying client requests."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import secrets

from .contracts import validate_package
from .private import ensure_private_directory, read_private_json, snapshot_package, write_private_json
from .processes import OwnershipLost
from .registry import RegistryError
from .settings import settings_digest
from .upgrades import UpgradeError


async def settled_work(host, function, *args):
    """Retain the install candidate until the underlying file worker has settled."""
    task = asyncio.create_task(host.static_work(function, *args))
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(task)
            if interrupted:
                raise asyncio.CancelledError()
            return result
        except asyncio.CancelledError:
            if task.done():
                if not task.cancelled():
                    task.exception()
                raise
            interrupted = True


async def checkpoint(host, operation_id: str, stage: str):
    operation = host.registry.get_operation(operation_id)
    host.registry.update_operation(operation_id, operation.status,
        details={**operation.details, "stage": stage}, error_code=operation.error_code)
    await host.install_checkpoint(operation_id, stage)
    if host.registry.get_operation(operation_id).details.get("cancel_requested"):
        raise UpgradeError("installation_canceled")


async def install_generation(host, operation_id: str, plan: dict):
    from .supervisor import HostError, bind_listener
    slot_id = plan["slot_id"]
    listener = host.plan_listeners.pop(plan["plan_id"], None)
    operation = host.registry.get_operation(operation_id)
    recovering = operation.status == "recovery_required"
    exposed = bool(operation.details.get("exposed"))
    created = False
    active = bool(plan["start"])
    try:
        try:
            slot = host.registry.get_slot(slot_id)
            created = recovering and bool(operation.details.get("selected", True))
        except RegistryError as error:
            if error.code != "slot_not_found":
                raise
            slot = None
        if operation.status in {"completed", "failed", "canceled"}:
            return
        if slot is not None:
            host.check_mutation(slot, operation_id=operation_id)
            if recovering:
                # The selected generation owns current intent. An explicit Stop
                # after exposure must not be undone by replaying the old plan.
                active = slot.desired_state == "running" and not slot.wake_suppressed
        host.registry.update_operation(operation_id, "running")
        await checkpoint(host, operation_id, "staging")
        if slot is not None and not recovering:
            raise HostError("slot_conflict", 409)
        if slot is None and operation.details.get("selected"):
            raise HostError("installation_recovery_required", 409)
        if slot is None and len(host.registry.list_slots()) >= 16:
            raise HostError("capacity_exceeded")
        if recovering:
            host.check_recovery_ownership(slot_id)
        elif host.ledger(slot_id):
            raise HostError("ownership_lost", 409)
        if recovering and slot is not None:
            await host.quiesce_upgrade(slot_id)
            if any(getattr(slot, key) != plan.get(key) for key in ("package_digest", "settings_digest")):
                raise HostError("installation_recovery_required", 409)
            data = Path(slot.data_path) if slot.data_path else host.slot_directory(slot_id) / "data"
            if not data.exists():
                raise HostError("generation_data_missing", 409)
            activation = host.activation(slot_id)
            exposed = exposed or bool(activation.get("exposure_permitted"))
            current = host.registry.get_operation(operation_id)
            host.registry.update_operation(operation_id, "running", details={**current.details,
                "selected": True, "exposed": exposed})
        if slot_id in host.listeners:
            listener = host.listeners[slot_id]
        elif listener is None:
            listener = bind_listener(plan["public_port"])
        package = await settled_work(host, validate_package, Path(plan["package_path"]))
        if package.digest != plan["package_digest"]:
            raise HostError("package_digest_mismatch")
        settings = host.check_plan_settings(plan, package)
        host.secrets.resolve(settings["secret_references"])
        package = await settled_work(host, snapshot_package, package,
            host.root / "packages" / package.manifest.package_id / package.digest)
        host.package_cache[str(package.root)] = (package, host.package_stamp(package))
        await checkpoint(host, operation_id, "files_ready")
        directory = host.slot_directory(slot_id)
        if slot is None:
            write_private_json(directory / "settings.json", settings)
            data = ensure_private_directory(directory / "data")
            connection_path = directory / "connection.json"
            token = read_private_json(connection_path)["token"] if connection_path.exists() else secrets.token_urlsafe(32)
            write_private_json(connection_path, {"slot_id": slot_id,
                "endpoint": f"http://127.0.0.1:{plan['public_port']}/mcp", "token": token,
                "max_request_bytes": package.manifest.lifecycle.max_request_bytes})
            slot = host.registry.register_install(operation_id, slot_id=slot_id,
                package_digest=package.digest, package_path=package.root, name=plan["name"],
                public_port=plan["public_port"], start_policy=plan["start_policy"],
                credential_ref=connection_path, settings_digest=settings_digest(settings),
                settings_path=directory / "settings.json", data_path=data,
                desired_state="running" if active else "stopped")
            created = True
        else:
            # Existing candidate settings/data are retained; the plan only
            # verifies the immutable choices and artifacts that were selected.
            host.package_settings(slot, package)
        host.listeners[slot_id] = listener
        await checkpoint(host, operation_id, "registered")
        host.registry.update_slot(slot_id, observed_state="starting", error_code=None)
        await host.check_candidate(slot, business_smoke=not exposed)
        host.secrets.resolve(settings["secret_references"])
        await checkpoint(host, operation_id, "candidate_verified")
        if not active:
            await host.stop_residents(slot_id)
        # Even a stopped installation commits permission for this candidate;
        # future explicit Start may admit writes. Cancellation ends here.
        host.registry.expose_upgrade(operation_id, expected_generation=slot.generation)
        exposed = True
        write_private_json(directory / "activation.json", {"generation": slot.generation,
            "verified": True, "exposure_permitted": True, "legacy_jev": plan.get("legacy_jev", False)})
        await checkpoint(host, operation_id, "exposed")
        await host.start_gateway(slot, active=active)
        host.registry.update_slot(slot_id, observed_state="ready" if active else "stopped")
        await checkpoint(host, operation_id, "completed")
        host.registry.update_operation(operation_id, "completed")
    except BaseException as error:
        code = "operation_interrupted" if isinstance(error, asyncio.CancelledError) else getattr(error, "code", "installation_failed")
        task = asyncio.create_task(fail_install(host, operation_id, code, created=created,
            exposed=exposed or host.registry.get_operation(operation_id).details.get("exposed", False), listener=listener))
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    if not task.cancelled():
                        task.exception()
                    break
    finally:
        if "import_id" in plan:
            host.imports.operation_status(plan["import_id"], host.registry.get_operation(operation_id).to_dict())


async def fail_install(host, operation_id: str, code: str, *, created: bool, exposed: bool, listener):
    operation = host.registry.get_operation(operation_id)
    slot_id = operation.slot_id
    lost = False
    try:
        await host.quiesce_upgrade(slot_id)
    except Exception as error:
        lost = (isinstance(error, OwnershipLost) or getattr(error, "code", None) == "ownership_lost"
                or bool(host.ledger(slot_id)) or host.commands.pending(slot_id)
                or any(value.ownership_lost for value in host.owner(slot_id).instances.values()))
        if not lost:
            code = "shutdown_unconfirmed"
    canceled = operation.details.get("cancel_requested") and not exposed and not lost
    interrupted = code == "operation_interrupted" and not canceled
    recovery = exposed or lost or interrupted or code == "shutdown_unconfirmed"
    if created:
        slot = host.registry.get_slot(slot_id)
        lost = lost or slot.observed_state == "ownership_lost"
        recovery = recovery or lost
        host.registry.update_slot(slot_id, observed_state="ownership_lost" if lost else "failed",
            error_code="ownership_lost" if lost else "installation_recovery_required" if recovery else code)
        if not recovery:
            host.registry.update_slot(slot_id, desired_state="stopped")
            host.registry.discard_slot(slot_id)
            host.activities.pop(slot_id, None)
    if listener is not None and not recovery:
        listener.close()
        host.listeners.pop(slot_id, None)
    details = {**host.registry.get_operation(operation_id).details,
               "stage": "recovery_required" if recovery else operation.details.get("stage", "staging")}
    if not recovery:
        details["selected"] = False
    host.registry.update_operation(operation_id, "recovery_required" if recovery else "canceled" if canceled else "failed",
        error_code="ownership_lost" if lost else "installation_recovery_required" if exposed else code, details=details)


async def cancel_install(host, operation) -> dict:
    if operation.details.get("exposed"):
        raise UpgradeError("installation_already_exposed")
    if operation.status in {"completed", "failed", "canceled"}:
        return operation.to_dict()
    operation = host.registry.update_operation(operation.id, operation.status,
        error_code=operation.error_code, details={**operation.details, "cancel_requested": True})
    if operation.status == "running":
        for task in host.tasks:
            if getattr(task, "operation_id", None) == operation.id:
                task.cancel()
    elif operation.status == "recovery_required":
        host.schedule_install(operation, read_private_json(host.root / "plans" / (operation.details["plan_id"] + ".json")), recovery=True)
    return operation.to_dict()


async def recover_install(host, operation) -> dict:
    if operation.status == "completed":
        return operation.to_dict()
    if operation.status != "recovery_required":
        raise UpgradeError("operation_recovery_unavailable")
    host.check_recovery_ownership(operation.slot_id)
    plan = read_private_json(host.root / "plans" / (operation.details["plan_id"] + ".json"))
    host.schedule_install(operation, plan, recovery=True)
    return operation.to_dict()
