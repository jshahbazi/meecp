"""Owner-authorized generation upgrades with a durable admission boundary."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import os
from pathlib import Path
import threading
import uuid

from .contracts import validate_package
from .grants import plan_digest
from .private import ensure_private_directory, read_private_json, snapshot_package, write_private_json
from .processes import OwnershipLost
from .registry import Slot, TERMINAL_OPERATIONS
from .settings import normalize_settings, settings_digest
from .upgrades import UpgradeError, snapshot_data


class UpgradeHost:
    """Methods share the supervisor lock, listeners, ownership, and registry."""

    def operation_snapshot(self, operation) -> dict:
        result = operation.to_dict()
        if operation.action == "upgrade":
            try:
                record = self.upgrades.get(operation.id)
                result["details"]["stage"] = record["status"]
            except UpgradeError:
                result["details"]["stage"] = "recovery_required" if operation.details.get("selected") else "staging"
        return result

    def check_slot_reference(self, slot_id: str, path: Path) -> None:
        root = self.slot_directory(slot_id)
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            raise UpgradeError("invalid_generation_reference") from None
        if not parts or any(part in {".", ".."} for part in parts):
            raise UpgradeError("invalid_generation_reference")
        # Never follow a mutable generation pointer through a symlink ancestor.
        parent = root
        for part in parts[:-1]:
            parent = ensure_private_directory(parent / part)

    def data_directory(self, slot) -> Path:
        path = Path(slot.data_path) if slot.data_path else self.slot_directory(slot.id) / "data"
        self.check_slot_reference(slot.id, path)
        if slot.data_path and not path.exists():
            raise UpgradeError("generation_data_missing")
        return ensure_private_directory(path)

    @staticmethod
    def _upgrade_identity(value: str) -> str:
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise UpgradeError("invalid_plan") from None
        return value

    async def plan_upgrade(self, payload: dict) -> dict:
        if (not isinstance(payload, dict) or set(payload) - {"slot_id", "package", "settings"}
                or not {"slot_id", "package"} <= set(payload) or not isinstance(payload["package"], str)
                or not Path(payload["package"]).is_absolute()):
            raise UpgradeError("invalid_upgrade_request")
        slot = self.registry.get_slot(payload["slot_id"])
        self.check_mutation(slot)
        package = await self.static_work(validate_package, Path(payload["package"]))
        previous = await self.static_work(self.package, slot)
        if (package.manifest.package_id != previous.manifest.package_id
                or package.manifest.profile != previous.manifest.profile):
            raise UpgradeError("incompatible_upgrade")
        settings = normalize_settings(package.manifest,
                                      payload["settings"] if "settings" in payload else self.package_settings(slot))
        self.secrets.resolve(settings["secret_references"])
        snapshot = await self.static_work(snapshot_package, package,
            self.root / "packages" / package.manifest.package_id / package.digest)
        # Static work yields. Recheck the reviewed generation after it settles.
        current = self.registry.get_slot(slot.id)
        self.check_mutation(current)
        if current != slot:
            raise UpgradeError("stale_generation")
        plan_id = str(uuid.uuid4())
        plan = {"plan_id": plan_id, "kind": "upgrade", "slot_id": slot.id,
                "expected_generation": slot.generation, "old": slot.to_dict(),
                "package_digest": snapshot.digest, "package_path": str(snapshot.root),
                "settings": settings, "settings_digest": settings_digest(settings),
                "start": slot.desired_state == "running", "name": slot.name,
                "start_policy": slot.start_policy, "public_port": slot.public_port,
                "legacy_jev": bool(self.activation(slot.id).get("legacy_jev", False)),
                "data_policy": "clone_before_probe", "rollback_policy": "before_exposure_only"}
        directory = ensure_private_directory(self.root / "upgrade-plans")
        write_private_json(directory / (plan_id + ".json"), plan)
        return self.inspect_upgrade_plan(plan_id)

    def read_upgrade_plan(self, plan_id: str) -> dict:
        plan_id = self._upgrade_identity(plan_id)
        plan = read_private_json(self.root / "upgrade-plans" / (plan_id + ".json"), max_bytes=65536)
        fields = {"plan_id", "kind", "slot_id", "expected_generation", "old", "package_digest", "package_path",
                  "settings", "settings_digest", "start", "name", "start_policy", "public_port", "legacy_jev",
                  "data_policy", "rollback_policy"}
        if (set(plan) != fields or plan["plan_id"] != plan_id or plan["kind"] != "upgrade"
                or type(plan["expected_generation"]) is not int or type(plan["start"]) is not bool
                or type(plan["legacy_jev"]) is not bool or plan["data_policy"] != "clone_before_probe"
                or plan["rollback_policy"] != "before_exposure_only"):
            raise UpgradeError("invalid_plan")
        try:
            old = Slot(**plan["old"])
        except (ValueError, TypeError):
            raise UpgradeError("invalid_plan") from None
        if (old.id != plan["slot_id"] or old.generation != plan["expected_generation"]
                or any(plan[key] != getattr(old, key) for key in ("name", "start_policy", "public_port"))):
            raise UpgradeError("invalid_plan")
        return plan

    def inspect_upgrade_plan(self, plan_id: str) -> dict:
        plan = self.read_upgrade_plan(plan_id)
        package = validate_package(Path(plan["package_path"]))
        if package.digest != plan["package_digest"]:
            raise UpgradeError("package_digest_mismatch")
        self.check_plan_settings(plan, package)
        return {**plan, "plan_digest": plan_digest(plan), "package": package.manifest.model_dump(),
                "installed_bytes": sum(artifact.size for artifact in package.lock.artifacts),
                "publisher_trust": "local_owner", "access_enforcement": "declarations_only"}

    async def apply_upgrade(self, plan_id: str) -> dict:
        async with self.install_lock:
            plan = self.read_upgrade_plan(plan_id)
            existing = self.registry.get_operation_by_key(plan_id)
            if existing is not None:
                if existing.action != "upgrade" or existing.details.get("plan_id") != plan_id:
                    raise UpgradeError("operation_conflict")
                if existing.status == "pending":
                    self.schedule_upgrade(existing, plan)
                return existing.to_dict()
            slot = self.registry.get_slot(plan["slot_id"])
            self.check_mutation(slot)
            if slot.to_dict() != plan["old"]:
                raise UpgradeError("stale_generation")
            operation = self.registry.create_operation("upgrade", slot.id, generation=slot.generation,
                idempotency_key=plan_id, details={"plan_id": plan_id, "package_digest": plan["package_digest"],
                    "package_path": plan["package_path"], "settings_digest": plan["settings_digest"],
                    "selected": False, "exposed": False, "cancel_requested": False})
            self.schedule_upgrade(operation, plan)
            return operation.to_dict()

    def schedule_upgrade(self, operation, plan: dict, *, recovery: bool = False):
        if not any(getattr(task, "operation_id", None) == operation.id for task in self.tasks):
            task = asyncio.create_task(self.execute_upgrade(operation.id, plan, recovery=recovery))
            task.operation_id = operation.id
            self.track(task)

    async def upgrade_checkpoint(self, operation_id: str, stage: str):
        """An overridable fault-test seam after each durable stage."""
        await asyncio.sleep(0)
        operation = self.registry.get_operation(operation_id)
        if operation.details.get("cancel_requested") and not operation.details.get("exposed"):
            raise UpgradeError("upgrade_canceled")

    async def upgrade_snapshot(self, source: Path, destination: Path, operation_id: str) -> dict:
        cancel = threading.Event()
        self.upgrade_cancels[operation_id] = cancel
        task = asyncio.create_task(asyncio.to_thread(snapshot_data, source, destination, cancel=cancel))
        interrupted = False
        try:
            while True:
                try:
                    result = await asyncio.shield(task)
                    if interrupted:
                        raise asyncio.CancelledError()
                    return result
                except asyncio.CancelledError:
                    if task.done():
                        # Consume a worker failure before reporting interruption.
                        if not task.cancelled():
                            task.exception()
                        raise
                    interrupted = True
                    cancel.set()
        finally:
            self.upgrade_cancels.pop(operation_id, None)

    async def quiesce_upgrade(self, slot_id: str):
        await self.stop_gateway(slot_id)
        await self.stop_residents(slot_id)
        for process in list(self.owner(slot_id).instances.values()):
            await self.reap(slot_id, process)
        if self.ledger(slot_id) or self.owner(slot_id).instances or self.owner(slot_id).persistence_failed:
            raise UpgradeError("ownership_lost")

    async def execute_upgrade(self, operation_id: str, plan: dict, *, recovery: bool = False):
        slot_id = plan["slot_id"]
        async with self.locks.setdefault(slot_id, asyncio.Lock()):
            try:
                operation = self.registry.get_operation(operation_id)
                self.check_mutation(self.registry.get_slot(slot_id), operation_id=operation_id)
                self.registry.update_operation(operation_id, "running")
                if recovery:
                    await self.reconcile_upgrade(operation_id)
                    return
                old = self.registry.get_slot(slot_id)
                if old.to_dict() != plan["old"]:
                    raise UpgradeError("stale_generation")
                directory = ensure_private_directory(self.slot_directory(slot_id) / "generations")
                directory = ensure_private_directory(directory / operation_id)
                candidate = replace(old, package_path=plan["package_path"], package_digest=plan["package_digest"],
                    settings_digest=plan["settings_digest"], settings_path=str(directory / "settings.json"),
                    data_path=str(directory / "data"), generation=old.generation + 1,
                    observed_state="updating", error_code=None)
                old_activation = {"generation": old.generation,
                    "verified": bool(self.activation(slot_id).get("verified", False)),
                    "exposure_permitted": bool(self.activation(slot_id).get("exposure_permitted", False)),
                    "legacy_jev": plan["legacy_jev"]}
                self.upgrades.create(operation_id, plan["plan_id"], old.to_dict(), candidate.to_dict(), old_activation)
                await self.upgrade_checkpoint(operation_id, "staging")
                # Return a durable operation before hashing large artifacts.
                # All validation still precedes quiescing the old writers.
                package = await self.static_work(validate_package, Path(plan["package_path"]))
                if package.digest != plan["package_digest"]:
                    raise UpgradeError("package_digest_mismatch")
                settings = self.check_plan_settings(plan, package)
                self.secrets.resolve(settings["secret_references"])
                self.registry.update_slot(slot_id, observed_state="updating")
                write_private_json(self.slot_directory(slot_id) / "activation.json", {**old_activation, "exposure_permitted": False})
                await self.quiesce_upgrade(slot_id)
                self.upgrades.update(operation_id, "quiesced")
                await self.upgrade_checkpoint(operation_id, "quiesced")
                if slot_id not in self.listeners:
                    from .supervisor import bind_listener
                    self.listeners[slot_id] = bind_listener(old.public_port)
                await self.start_gateway(old, active=False, inactive_code="updating")
                snapshot = await self.upgrade_snapshot(self.data_directory(old), Path(candidate.data_path), operation_id)
                settings = self.check_plan_settings(plan, self.package(candidate))
                self.secrets.resolve(settings["secret_references"])
                write_private_json(Path(candidate.settings_path), settings)
                self.upgrades.update(operation_id, "snapshot_ready", snapshot=snapshot)
                await self.upgrade_checkpoint(operation_id, "snapshot_ready")
                self.candidate_slots[slot_id] = candidate
                await self.check_candidate(candidate)
                self.secrets.resolve(settings["secret_references"])
                self.upgrades.update(operation_id, "candidate_verified")
                await self.upgrade_checkpoint(operation_id, "candidate_verified")
                candidate = self.registry.select_upgrade(operation_id, candidate, expected_generation=old.generation)
                self.upgrades.update(operation_id, "selected")
                await self.upgrade_checkpoint(operation_id, "selected")
                self.registry.expose_upgrade(operation_id, expected_generation=candidate.generation)
                self.upgrades.update(operation_id, "exposed")
                await self.upgrade_checkpoint(operation_id, "exposed")
                write_private_json(self.slot_directory(slot_id) / "activation.json", {
                    "generation": candidate.generation, "verified": True,
                    "exposure_permitted": True, "legacy_jev": plan["legacy_jev"]})
                await self.stop_gateway(slot_id, stop_runtime=False)
                if not plan["start"]:
                    await self.stop_residents(slot_id)
                await self.start_gateway(candidate, active=plan["start"])
                self.registry.update_slot(slot_id, observed_state="ready" if plan["start"] else "stopped")
                self.upgrades.update(operation_id, "completed")
                self.registry.update_operation(operation_id, "completed")
            except BaseException as error:
                # Compensation is owned independently of caller cancellation.
                code = "upgrade_canceled" if isinstance(error, asyncio.CancelledError) else getattr(error, "code", "upgrade_failed")
                task = asyncio.create_task(self.fail_upgrade(operation_id, code))
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
                self.candidate_slots.pop(slot_id, None)

    async def upgrade_record(self, operation_id: str) -> dict:
        """Reconcile the atomic registry marker when its secondary journal lagged."""
        record = self.upgrades.get(operation_id)
        operation = self.registry.get_operation(operation_id)
        if operation.details.get("exposed") and not record["exposed"]:
            self.upgrades.update(operation_id, "recovery_required", selected=True, exposed=True)
            record = self.upgrades.get(operation_id)
        return record

    async def restore_upgrade(self, operation_id: str, record: dict):
        slot_id = record["slot_id"]
        await self.quiesce_upgrade(slot_id)
        self.candidate_slots.pop(slot_id, None)
        current = self.registry.get_slot(slot_id)
        old = Slot(**record["old"])
        restored = replace(old, generation=current.generation + 1, observed_state="starting", error_code=None)
        restored = self.registry.select_upgrade(operation_id, restored, expected_generation=current.generation, selected=False)
        write_private_json(self.slot_directory(slot_id) / "activation.json", {
            **record["old_activation"], "generation": restored.generation, "exposure_permitted": False})
        if not self.closing:
            if slot_id not in self.listeners:
                from .supervisor import bind_listener
                self.listeners[slot_id] = bind_listener(restored.public_port)
            if restored.desired_state == "running":
                await self.check_candidate(restored, business_smoke=False)
                self.secrets.resolve(self.package_settings(restored)["secret_references"])
            await self.start_gateway(restored, active=restored.desired_state == "running")
            write_private_json(self.slot_directory(slot_id) / "activation.json", {
                **record["old_activation"], "generation": restored.generation,
                "exposure_permitted": restored.desired_state == "running"})
        self.registry.update_slot(slot_id, observed_state="ready" if restored.desired_state == "running" and not self.closing else "stopped")

    async def retain_upgrade_recovery(self, operation, code: str):
        """Close held admission when metadata cannot authorize any rollback."""
        try:
            await self.quiesce_upgrade(operation.slot_id)
        except Exception:
            pass
        slot = self.registry.get_slot(operation.slot_id)
        lost = (slot.observed_state == "ownership_lost" or bool(self.ledger(slot.id))
                or bool(self.owner(slot.id).instances) or self.owner(slot.id).persistence_failed)
        self.registry.update_slot(slot.id, observed_state="ownership_lost" if lost else "failed",
            error_code="ownership_lost" if lost else "upgrade_recovery_required")
        self.registry.update_operation(operation.id, "recovery_required", error_code="ownership_lost" if lost else code)

    async def fail_upgrade(self, operation_id: str, code: str):
        operation = self.registry.get_operation(operation_id)
        if operation.status in TERMINAL_OPERATIONS:
            return
        try:
            record = await self.upgrade_record(operation_id)
        except UpgradeError as error:
            if error.code != "upgrade_not_found":
                # Unknown journal contents cannot authorize continued admission
                # or an old-generation rollback. Drain held owners and retain
                # the selected generation for explicit recovery.
                await self.retain_upgrade_recovery(operation, "upgrades_unavailable")
                return
            if operation.details.get("exposed") or operation.details.get("selected"):
                await self.retain_upgrade_recovery(operation, "upgrade_recovery_required")
                return
            # Intent precedes the first journal and every runtime mutation. A
            # crash in that gap can reconstruct only the unchanged old baseline.
            try:
                plan = self.read_upgrade_plan(operation.details["plan_id"])
                old = self.registry.get_slot(operation.slot_id)
                if old.to_dict() != plan["old"]:
                    raise UpgradeError("upgrade_generation_mismatch")
                directory = self.slot_directory(old.id) / "generations" / operation_id
                candidate = replace(old, package_path=plan["package_path"], package_digest=plan["package_digest"],
                    settings_path=str(directory / "settings.json"), settings_digest=plan["settings_digest"],
                    data_path=str(directory / "data"), generation=old.generation + 1,
                    observed_state="updating", error_code=None)
                activation = self.activation(old.id)
                record = self.upgrades.create(operation_id, plan["plan_id"], old.to_dict(), candidate.to_dict(),
                    {"generation": old.generation, "verified": bool(activation.get("verified")),
                     "exposure_permitted": bool(activation.get("exposure_permitted")), "legacy_jev": plan["legacy_jev"]})
            except Exception:
                await self.retain_upgrade_recovery(operation, "upgrade_recovery_required")
                return
        try:
            if record["exposed"]:
                await self.quiesce_upgrade(operation.slot_id)
                if record["status"] != "completed":
                    self.upgrades.update(operation_id, "recovery_required", error_code="upgrade_recovery_required")
                self.registry.update_slot(operation.slot_id, observed_state="failed", error_code="upgrade_recovery_required")
                self.registry.update_operation(operation_id, "recovery_required", error_code="upgrade_recovery_required")
                return
            await self.restore_upgrade(operation_id, record)
            canceled = code == "upgrade_canceled" or record["cancel_requested"]
            self.upgrades.update(operation_id, "canceled" if canceled else "rolled_back", error_code=code)
            self.registry.update_operation(operation_id, "canceled" if canceled else "failed", error_code=code)
        except Exception as error:
            current = self.registry.get_slot(operation.slot_id)
            lost = (isinstance(error, OwnershipLost) or getattr(error, "code", None) == "ownership_lost"
                    or current.observed_state == "ownership_lost"
                    or any(process.ownership_lost for process in self.owner(operation.slot_id).instances.values())
                    or any(value.get("state") == "ownership_lost" for value in self.ledger(operation.slot_id).values()))
            failure = "ownership_lost" if lost else "upgrade_recovery_required"
            with contextlib.suppress(Exception):
                await self.quiesce_upgrade(operation.slot_id)
            if record["status"] != "completed":
                self.upgrades.update(operation_id, "recovery_required", error_code=failure)
            self.registry.update_slot(operation.slot_id, observed_state="ownership_lost" if failure == "ownership_lost" else "failed", error_code=failure)
            self.registry.update_operation(operation_id, "recovery_required", error_code=failure)

    async def reconcile_upgrade(self, operation_id: str):
        record = await self.upgrade_record(operation_id)
        if not record["exposed"]:
            await self.fail_upgrade(operation_id, "operation_interrupted")
            return
        slot = self.registry.get_slot(record["slot_id"])
        candidate = Slot(**record["candidate"])
        if any(getattr(slot, key) != getattr(candidate, key) for key in
               ("package_path", "package_digest", "settings_digest", "data_path", "settings_path")):
            raise UpgradeError("upgrade_generation_mismatch")
        self.data_directory(slot)
        await self.quiesce_upgrade(slot.id)
        if slot.id not in self.listeners:
            from .supervisor import bind_listener
            self.listeners[slot.id] = bind_listener(slot.public_port)
        self.registry.update_slot(slot.id, observed_state="starting", error_code=None)
        if slot.desired_state == "running":
            await self.check_candidate(slot, business_smoke=False)
            self.secrets.resolve(self.package_settings(slot)["secret_references"])
        write_private_json(self.slot_directory(slot.id) / "activation.json", {
            "generation": slot.generation, "verified": True, "exposure_permitted": True,
            "legacy_jev": record["old_activation"]["legacy_jev"]})
        await self.start_gateway(slot, active=slot.desired_state == "running")
        self.registry.update_slot(slot.id, observed_state="ready" if slot.desired_state == "running" else "stopped", error_code=None)
        self.upgrades.update(operation_id, "completed")
        self.registry.update_operation(operation_id, "completed")

    async def cancel_operation(self, operation_id: str) -> dict:
        operation = self.registry.get_operation(operation_id)
        if operation.action == "install":
            from .install_recovery import cancel_install
            return await cancel_install(self, operation)
        if operation.action != "upgrade":
            raise UpgradeError("operation_cancel_unavailable")
        if operation.details.get("exposed"):
            raise UpgradeError("upgrade_already_exposed")
        if operation.status in TERMINAL_OPERATIONS:
            return operation.to_dict()
        details = {**operation.details, "cancel_requested": True}
        operation = self.registry.update_operation(operation_id, operation.status, error_code=operation.error_code, details=details)
        with contextlib.suppress(UpgradeError):
            self.upgrades.request_cancel(operation_id)
        cancel = self.upgrade_cancels.get(operation_id)
        if cancel is not None:
            cancel.set()
        return operation.to_dict()

    async def recover_operation(self, operation_id: str) -> dict:
        operation = self.registry.get_operation(operation_id)
        if operation.action == "install":
            from .install_recovery import recover_install
            return await recover_install(self, operation)
        if operation.status == "completed":
            return operation.to_dict()
        if operation.action != "upgrade" or operation.status != "recovery_required":
            raise UpgradeError("operation_recovery_unavailable")
        self.check_recovery_ownership(operation.slot_id)
        self.schedule_upgrade(operation, self.read_upgrade_plan(operation.details["plan_id"]), recovery=True)
        return operation.to_dict()
