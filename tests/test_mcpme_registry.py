from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest

from mcpme.private import PrivateStateError
from mcpme.registry import Registry, RegistryError


def add_fixture(registry: Registry, port: int = 19386):
    return registry.add_slot(package_digest="a" * 64, package_path=Path("/prepared/package"),
                             name="Fixture", public_port=port)


def test_slot_identity_port_generation_and_persistence(tmp_path):
    state = tmp_path / "state"
    with Registry(state) as registry:
        slot = add_fixture(registry)
        assert slot.start_policy == "manual"
        assert slot.desired_state == slot.observed_state == "stopped"
        updated = registry.update_slot(slot.id, expected_generation=1, name="Renamed",
                                       observed_state="ready", desired_state="running")
        assert updated.id == slot.id and updated.public_port == slot.public_port
        assert registry.list_slots() == [updated]
        upgraded = registry.update_slot(slot.id, expected_generation=1, generation=2,
                                        package_digest="b" * 64, package_path="/prepared/new")
        with pytest.raises(RegistryError, match="^stale_generation$"):
            registry.update_slot(slot.id, expected_generation=1, observed_state="failed")
        with pytest.raises(RegistryError, match="^invalid_slot_update$"):
            registry.update_slot(slot.id, public_port=19387)
        with pytest.raises(RegistryError, match="^generation_required$"):
            registry.update_slot(slot.id, package_digest="c" * 64)
    assert (state.stat().st_mode & 0o777) == 0o700
    assert ((state / "registry.sqlite").stat().st_mode & 0o777) == 0o600
    with Registry(state) as reopened:
        assert reopened.get_slot(slot.id) == upgraded


def test_port_conflicts_and_owner_loss_are_persistent(tmp_path):
    with Registry(tmp_path / "state") as registry:
        slot = add_fixture(registry)
        with pytest.raises(RegistryError, match="^slot_conflict$"):
            add_fixture(registry)
        registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")
        with pytest.raises(RegistryError, match="^ownership_reconciliation_required$"):
            registry.update_slot(slot.id, observed_state="starting")
    with Registry(tmp_path / "state") as registry:
        assert registry.get_slot(slot.id).observed_state == "ownership_lost"


def test_operation_idempotency_and_terminal_results(tmp_path):
    key = str(uuid.uuid4())
    candidate = str(uuid.uuid4())
    with Registry(tmp_path / "state") as registry:
        operation = registry.create_operation(kind="install", slot_id=candidate, generation=1,
                                              idempotency_key=key, details={"plan_id": key})
        assert registry.create_operation(kind="install", slot_id=candidate, generation=1,
                                         idempotency_key=key, details={"plan_id": key}) == operation
        with pytest.raises(RegistryError, match="^operation_conflict$"):
            registry.create_operation(kind="install", idempotency_key=key)
        running = registry.update_operation(operation.id, "running")
        assert running.status == "running"
        result = registry.update_operation(operation.id, "failed", error_code="smoke_probe_failed")
        with pytest.raises(RegistryError, match="^operation_finalized$"):
            registry.update_operation(operation.id, "running")
        assert registry.list_operations() == [result]
        with pytest.raises(RegistryError, match="^invalid_operation_details$"):
            registry.create_operation("install", details={"prompt": "must never persist"})
    with Registry(tmp_path / "state") as registry:
        assert registry.get_operation(operation.id) == result


@pytest.mark.parametrize("kind", ["directory_mode", "database_mode", "database_symlink", "journal_symlink", "hardlink"])
def test_registry_rejects_unsafe_storage(tmp_path, kind):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    db = state / "registry.sqlite"
    if kind == "directory_mode":
        state.chmod(0o755)
    elif kind == "database_mode":
        db.touch(mode=0o644)
    elif kind in ("database_symlink", "journal_symlink"):
        target = tmp_path / "outside"
        target.write_text("outside preserved")
        (db if kind == "database_symlink" else state / "registry.sqlite-journal").symlink_to(target)
    else:
        db.touch(mode=0o600)
        os.link(db, tmp_path / "shared-db")
    with pytest.raises(PrivateStateError):
        Registry(state)


def test_concurrent_registries_generation_compare_and_swap(tmp_path):
    state = tmp_path / "state"
    with Registry(state) as first, Registry(state) as second:
        slot = add_fixture(first)
        first.update_slot(slot.id, expected_generation=1, generation=2)
        with pytest.raises(RegistryError, match="^stale_generation$"):
            second.update_slot(slot.id, expected_generation=1, generation=2)
        assert second.get_slot(slot.id).generation == 2


def test_install_retries_recover_operation_after_slot_creation_and_restart(tmp_path):
    key = str(uuid.uuid4())
    slot_id = str(uuid.uuid4())
    with Registry(tmp_path / "state") as registry:
        operation = registry.create_operation("install", slot_id=slot_id, idempotency_key=key,
                                              details={"plan_id": key})
        slot = registry.add_slot(slot_id=slot_id, package_digest="a" * 64, package_path="/prepared/package",
                                 name="Fixture", public_port=19386)
        registry.update_slot(slot.id, generation=2)
        completed = registry.update_operation(operation.id, "completed")
        assert registry.create_operation("install", slot_id=slot_id, idempotency_key=key,
                                          details={"plan_id": key}) == completed


def test_recovery_inspects_older_unfinished_operations_beyond_display_page(tmp_path):
    with Registry(tmp_path / "state") as registry:
        interrupted = registry.create_operation("install")
        registry.update_operation(interrupted.id, "running")
        for _ in range(105):
            later = registry.create_operation("install")
            registry.update_operation(later.id, "completed")
        assert interrupted.id not in {item.id for item in registry.list_operations()}
        assert [item.id for item in registry.list_unfinished_operations()] == [interrupted.id]
