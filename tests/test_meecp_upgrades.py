from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import uuid

import pytest

import meecp.upgrades as upgrades
from meecp.private import ensure_private_directory, write_private_json
from meecp.upgrades import UpgradeError, UpgradeStore, recovery_action, snapshot_data


def private_tree(path: Path) -> Path:
    return ensure_private_directory(path)


def private_file(path: Path, data: bytes = b"synthetic data", mode: int = 0o600) -> Path:
    path.write_bytes(data)
    path.chmod(mode)
    return path


def slots(root: Path) -> tuple[dict, dict, dict]:
    identity = str(uuid.uuid4())
    old = {"slot_id": identity, "package_digest": "a" * 64, "package_path": str(root / "packages" / "old"),
           "name": "Synthetic App", "public_port": 19386, "start_policy": "manual", "desired_state": "running",
           "observed_state": "ready", "generation": 7, "wake_suppressed": False, "error_code": None,
           "credential_ref": str(root / "slots" / identity / "connection.json"),
           "settings_digest": "b" * 64, "data_path": str(root / "slots" / identity / "data"),
           "settings_path": str(root / "slots" / identity / "settings.json")}
    candidate = {**old, "package_digest": "c" * 64, "package_path": str(root / "packages" / "new"),
                 "generation": 8, "observed_state": "updating",
                 "data_path": str(root / "slots" / identity / "generations" / "next" / "data"),
                 "settings_path": str(root / "slots" / identity / "generations" / "next" / "settings.json")}
    activation = {"generation": 7, "verified": True, "exposure_permitted": True, "legacy_jev": False}
    return old, candidate, activation


def create(store: UpgradeStore, root: Path, *, operation_id: str | None = None) -> dict:
    return store.create(operation_id or str(uuid.uuid4()), str(uuid.uuid4()), *slots(root))


def select(store: UpgradeStore, record: dict) -> dict:
    identity = record["operation_id"]
    store.update(identity, "quiesced")
    store.update(identity, "snapshot_ready", snapshot={"bytes": 1, "files": 1, "digest": "d" * 64})
    store.update(identity, "candidate_verified")
    return store.update(identity, "selected")


def test_snapshot_is_private_durable_content_free_and_nonexecuting(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_file(source / "state.bin", b"synthetic persistence")
    private_tree(source / "nested")
    private_file(source / "nested" / "executable", b"synthetic executable", 0o700)
    private_tree(source / "empty")
    destination = tmp_path / "private" / "candidate"
    def forbid(*args, **kwargs):
        pytest.fail("data cloning cannot execute source code")
    monkeypatch.setattr("subprocess.Popen", forbid)
    metadata = snapshot_data(source, destination)
    assert set(metadata) == {"bytes", "files", "digest"}
    assert metadata["bytes"] == len(b"synthetic persistence") + len(b"synthetic executable")
    assert metadata["files"] == 2
    assert len(metadata["digest"]) == 64
    assert destination.joinpath("state.bin").read_bytes() == b"synthetic persistence"
    assert destination.joinpath("nested", "executable").read_bytes() == b"synthetic executable"
    for path in (destination, destination / "nested", destination / "empty"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "state.bin").stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "nested" / "executable").stat().st_mode) == 0o700
    assert not list(destination.parent.glob("*.tmp"))
    metadata_again = snapshot_data(source, tmp_path / "private" / "second")
    assert metadata_again == metadata


def test_snapshot_empty_tree_is_supported(tmp_path):
    metadata = snapshot_data(private_tree(tmp_path / "source"), tmp_path / "private" / "candidate")
    assert metadata == {"bytes": 0, "files": 0, "digest": hashlib.sha256(b"").hexdigest()}


@pytest.mark.parametrize("mode", [0o644, 0o666, 0o755, 0o1600, 0o4600, 0o2600])
def test_snapshot_rejects_nonprivate_or_special_modes(tmp_path, mode):
    source = private_tree(tmp_path / "source")
    private_file(source / "state", mode=mode)
    with pytest.raises(UpgradeError, match="^upgrade_data_unsafe$"):
        snapshot_data(source, tmp_path / "private" / "candidate")
    assert not (tmp_path / "private" / "candidate").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_snapshot_rejects_links_special_files_and_unsafe_directories(tmp_path, kind):
    source = private_tree(tmp_path / "source")
    if kind == "symlink":
        (source / "state").symlink_to(tmp_path / "missing")
    elif kind == "hardlink":
        original = private_file(tmp_path / "outside")
        os.link(original, source / "state")
    elif kind == "fifo":
        os.mkfifo(source / "state", mode=0o600)
    else:
        private_tree(source / "nested").chmod(0o755)
    with pytest.raises(UpgradeError, match="^upgrade_data_unsafe$"):
        snapshot_data(source, tmp_path / "private" / "candidate")


def test_snapshot_rejects_foreign_owner(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    monkeypatch.setattr(upgrades.os, "getuid", lambda: source.stat().st_uid + 1)
    with pytest.raises(UpgradeError, match="^upgrade_data_unsafe$"):
        snapshot_data(source, tmp_path / "private" / "candidate")


def test_snapshot_rejects_symlink_ancestors_and_aliases(tmp_path):
    source = private_tree(tmp_path / "source")
    private_file(source / "state")
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    for selected, destination in ((alias, tmp_path / "private" / "candidate"),
                                  (source, alias / "candidate"), (source, source),
                                  (source, source / "candidate"), (source, tmp_path)):
        with pytest.raises(UpgradeError, match="^upgrade_data_unsafe$"):
            snapshot_data(selected, destination)
    assert (source / "state").read_bytes() == b"synthetic data"


@pytest.mark.parametrize("name", ["back\\slash", "colon:name", "control\nname", "format\u200bname"])
def test_snapshot_rejects_ambiguous_names(tmp_path, name):
    source = private_tree(tmp_path / "source")
    private_file(source / name)
    with pytest.raises(UpgradeError, match="^upgrade_data_unsafe$"):
        snapshot_data(source, tmp_path / "private" / "candidate")


def test_snapshot_rejects_casefold_collisions_even_on_case_sensitive_volume(tmp_path):
    source = private_tree(tmp_path / "source")
    private_file(source / "Upper")
    private_file(source / "upper")
    if len(list(source.iterdir())) == 1:
        pytest.skip("filesystem already folds these names")
    with pytest.raises(UpgradeError, match="^upgrade_data_unsafe$"):
        snapshot_data(source, tmp_path / "private" / "candidate")


@pytest.mark.parametrize("limit", ["MAX_DATA_FILES", "MAX_DATA_BYTES", "MAX_DATA_DIRECTORIES", "MAX_TREE_METADATA_BYTES"])
def test_snapshot_enforces_all_storage_bounds(tmp_path, monkeypatch, limit):
    source = private_tree(tmp_path / "source")
    private_file(source / "state", b"abc")
    private_tree(source / "nested")
    monkeypatch.setattr(upgrades, limit, 0)
    with pytest.raises(UpgradeError, match="^upgrade_data_limit$"):
        snapshot_data(source, tmp_path / "private" / "candidate")


def test_snapshot_requires_disk_headroom(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_file(source / "state", b"abc")
    monkeypatch.setattr(upgrades.shutil, "disk_usage", lambda _: type("Usage", (), {"free": upgrades.DISK_HEADROOM_BYTES + 2})())
    with pytest.raises(UpgradeError, match="^upgrade_disk_unavailable$"):
        snapshot_data(source, tmp_path / "private" / "candidate")
    assert not list((tmp_path / "private").iterdir())


def test_snapshot_rechecks_entire_source_identity(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_file(source / "state")
    original = upgrades._tree
    scans = 0
    def changing(path, cancel):
        nonlocal scans
        scans += 1
        if scans == 2:
            private_file(source / "new-file")
        return original(path, cancel)
    monkeypatch.setattr(upgrades, "_tree", changing)
    destination = tmp_path / "private" / "candidate"
    with pytest.raises(UpgradeError, match="^upgrade_data_changed$"):
        snapshot_data(source, destination)
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


def test_snapshot_detects_file_change_after_copy_before_publication(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_file(source / "state", b"original")
    original = upgrades._sync_directory
    modified = False
    def changing(path):
        nonlocal modified
        if not modified:
            private_file(source / "state", b"changed!")
            modified = True
        return original(path)
    monkeypatch.setattr(upgrades, "_sync_directory", changing)
    destination = tmp_path / "private" / "candidate"
    with pytest.raises(UpgradeError, match="^upgrade_data_changed$"):
        snapshot_data(source, destination)
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


def test_snapshot_cancel_cleans_only_its_own_staging(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_file(source / "state")
    destination = tmp_path / "private" / "candidate"
    unrelated = private_tree(destination.parent / "unrelated")
    private_file(unrelated / "retained")
    cancel = threading.Event()
    original = upgrades._sync_directory
    def canceled(path):
        if path.name.endswith(".tmp"):
            cancel.set()
        return original(path)
    monkeypatch.setattr(upgrades, "_sync_directory", canceled)
    with pytest.raises(UpgradeError, match="^upgrade_canceled$"):
        snapshot_data(source, destination, cancel)
    assert (unrelated / "retained").exists()
    assert not destination.exists()
    assert sorted(path.name for path in destination.parent.iterdir()) == ["unrelated"]


def test_snapshot_already_canceled_never_creates_destination(tmp_path):
    source = private_tree(tmp_path / "source")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(UpgradeError, match="^upgrade_canceled$"):
        snapshot_data(source, tmp_path / "private" / "candidate", cancel)
    assert not (tmp_path / "private").exists()


def test_snapshot_never_overwrites_even_a_racing_empty_target(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_file(source / "state")
    destination = tmp_path / "private" / "candidate"
    original = upgrades._publish
    def racing(staged, target):
        private_tree(target)
        return original(staged, target)
    monkeypatch.setattr(upgrades, "_publish", racing)
    with pytest.raises(UpgradeError, match="^upgrade_snapshot_exists$"):
        snapshot_data(source, destination)
    assert list(destination.iterdir()) == []
    assert sorted(path.name for path in destination.parent.iterdir()) == ["candidate"]


def test_snapshot_rejects_existing_target_without_reading_or_deleting_it(tmp_path):
    source = private_tree(tmp_path / "source")
    destination = private_tree(tmp_path / "private" / "candidate")
    private_file(destination / "retained")
    with pytest.raises(UpgradeError, match="^upgrade_snapshot_exists$"):
        snapshot_data(source, destination)
    assert (destination / "retained").read_bytes() == b"synthetic data"


def test_snapshot_cleanup_does_not_delete_replaced_staging_tree(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_file(source / "state")
    replaced = None
    def replacement(staged, target):
        nonlocal replaced
        os.rename(staged, staged.with_name("retained-owned-staging"))
        replaced = private_tree(staged)
        private_file(replaced / "unrelated")
        raise UpgradeError("upgrade_snapshot_unavailable")
    monkeypatch.setattr(upgrades, "_publish", replacement)
    with pytest.raises(UpgradeError, match="^upgrade_snapshot_unavailable$"):
        snapshot_data(source, tmp_path / "private" / "candidate")
    assert replaced is not None and (replaced / "unrelated").exists()


def test_snapshot_flushes_mode_before_file_and_nested_directories_before_parent(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_tree(source / "nested")
    private_file(source / "nested" / "executable", b"x", 0o700)
    calls = []
    fsync, directory = os.fsync, upgrades._sync_directory
    def sync(fd):
        details = os.fstat(fd)
        if stat.S_ISREG(details.st_mode):
            calls.append(("file", stat.S_IMODE(details.st_mode)))
        return fsync(fd)
    def sync_directory(path):
        calls.append(("directory", path.name))
        return directory(path)
    monkeypatch.setattr(upgrades.os, "fsync", sync)
    monkeypatch.setattr(upgrades, "_sync_directory", sync_directory)
    snapshot_data(source, tmp_path / "private" / "candidate")
    assert calls[0] == ("file", 0o700)
    assert calls[1] == ("directory", "nested")
    assert calls[2][0] == "directory" and calls[2][1].endswith(".tmp")
    assert calls[3] == ("directory", "private")


def test_snapshot_published_data_is_retained_if_parent_sync_fails(tmp_path, monkeypatch):
    source = private_tree(tmp_path / "source")
    private_file(source / "state")
    destination = tmp_path / "private" / "candidate"
    original = upgrades._sync_directory
    def fault(path):
        if path == destination.parent:
            raise OSError("synthetic content that must not escape")
        return original(path)
    monkeypatch.setattr(upgrades, "_sync_directory", fault)
    with pytest.raises(UpgradeError, match="^upgrade_snapshot_unavailable$") as captured:
        snapshot_data(source, destination)
    assert captured.value.__cause__ is None
    assert (destination / "state").read_bytes() == b"synthetic data"


def test_upgrade_journal_closed_private_durable_and_idempotent(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    old, candidate, activation = slots(store.state_root)
    identity, plan = str(uuid.uuid4()), str(uuid.uuid4())
    record = store.create(identity, plan, old, candidate, activation)
    old["name"] = "mutated caller"
    record["candidate"]["name"] = "mutated returned record"
    loaded = UpgradeStore(store.state_root).get(identity)
    assert loaded["old"]["name"] == "Synthetic App"
    assert loaded["candidate"]["name"] == "Synthetic App"
    assert stat.S_IMODE((store.records / (identity + ".json")).stat().st_mode) == 0o600
    assert store.create(identity, plan, loaded["old"], loaded["candidate"], loaded["old_activation"]) == loaded
    assert recovery_action(loaded) == "restore_old"
    with pytest.raises(UpgradeError, match="^upgrade_identity_conflict$"):
        store.create(identity, str(uuid.uuid4()), loaded["old"], loaded["candidate"], loaded["old_activation"])


def test_exposure_is_durable_and_can_never_be_rolled_back(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    record = select(store, create(store, store.state_root))
    identity = record["operation_id"]
    assert record["selected"] and not record["exposed"]
    assert recovery_action(record) == "restore_old"
    exposed = store.update(identity, "exposed")
    assert exposed["selected"] and exposed["exposed"]
    restarted = UpgradeStore(store.state_root)
    assert recovery_action(restarted.get(identity)) == "recover_candidate"
    with pytest.raises(UpgradeError, match="^upgrade_already_exposed$"):
        restarted.request_cancel(identity)
    restarted.update(identity, "recovery_required", error_code="upgrade_recovery_required")
    for stage, changes in (("rolled_back", {}), ("canceled", {}), ("recovery_required", {"exposed": False}),
                           ("recovery_required", {"selected": False})):
        with pytest.raises(UpgradeError, match="^upgrade_already_exposed$"):
            restarted.update(identity, stage, **changes)
    completed = restarted.update(identity, "completed", error_code=None)
    assert completed["exposed"]
    assert restarted.list_unfinished() == []


def test_preexposure_cancel_is_idempotent_sticky_and_blocks_exposure(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    selected = select(store, create(store, store.state_root))
    identity = selected["operation_id"]
    canceled = store.request_cancel(identity)
    assert canceled["status"] == "selected" and canceled["cancel_requested"]
    assert store.request_cancel(identity) == canceled
    with pytest.raises(UpgradeError, match="^invalid_upgrade_update$"):
        store.update(identity, "selected", cancel_requested=False)
    with pytest.raises(UpgradeError, match="^upgrade_canceled$"):
        store.update(identity, "exposed")
    rolled_back = store.update(identity, "rolled_back")
    assert not rolled_back["selected"] and not rolled_back["exposed"]
    assert store.request_cancel(identity) == rolled_back
    assert store.list_unfinished() == []


def test_monotonic_stages_reject_skips_or_rewriting_generation_metadata(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    identity = create(store, store.state_root)["operation_id"]
    with pytest.raises(UpgradeError, match="^invalid_upgrade_transition$"):
        store.update(identity, "selected")
    for changes in ({"old": {}}, {"candidate": {}}, {"plan_id": str(uuid.uuid4())}, {"notes": "input"}):
        with pytest.raises(UpgradeError, match="^invalid_upgrade_update$"):
            store.update(identity, "staging", **changes)
    store.update(identity, "quiesced")
    with pytest.raises(UpgradeError, match="^invalid_upgrade_transition$"):
        store.update(identity, "staging")
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        store.update(identity, "snapshot_ready")
    store.update(identity, "canceled")
    with pytest.raises(UpgradeError, match="^invalid_upgrade_transition$"):
        store.update(identity, "quiesced")


def test_interrupted_preexposure_candidate_cannot_be_exposed_by_recovery(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    selected = select(store, create(store, store.state_root))
    identity = selected["operation_id"]
    store.update(identity, "recovery_required", error_code="upgrade_recovery_required")
    for stage in ("exposed", "completed"):
        with pytest.raises(UpgradeError, match="^invalid_upgrade_transition$"):
            store.update(identity, stage)
    assert recovery_action(store.get(identity)) == "restore_old"
    store.update(identity, "rolled_back")


def test_initial_data_snapshot_identity_cannot_be_rewritten(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    selected = select(store, create(store, store.state_root))
    identity = selected["operation_id"]
    for value in (None, {"bytes": 1, "files": 1, "digest": "e" * 64}):
        with pytest.raises(UpgradeError, match="^invalid_upgrade_update$"):
            store.update(identity, "selected", snapshot=value)
    assert store.update(identity, "selected", snapshot=selected["snapshot"])["snapshot"] == selected["snapshot"]


@pytest.mark.parametrize("field,value", [("public_port", 19387), ("name", "changed"), ("start_policy", "host"),
                                          ("credential_ref", "/changed"), ("generation", 9), ("slot_id", str(uuid.uuid4()))])
def test_journal_rejects_changed_immutable_slot_fields(tmp_path, field, value):
    store = UpgradeStore(tmp_path / "state")
    old, candidate, activation = slots(store.state_root)
    candidate[field] = value
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        store.create(str(uuid.uuid4()), str(uuid.uuid4()), old, candidate, activation)
    assert not list(store.records.iterdir())


def test_old_generation_can_have_no_settings_or_data_binding(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    old, candidate, activation = slots(store.state_root)
    old.update(settings_digest=None, settings_path=None, data_path=None)
    record = store.create(str(uuid.uuid4()), str(uuid.uuid4()), old, candidate, activation)
    assert record["old"]["settings_digest"] is None


@pytest.mark.parametrize("field,value", [("schema_version", True), ("exposed", "false"), ("selected", 1),
                                          ("cancel_requested", None), ("created_at", True), ("updated_at", -1),
                                          ("updated_at", float("nan")), ("status", "unknown"),
                                          ("error_code", "raw exception material"), ("slot_id", "../../other"),
                                          ("snapshot", {"bytes": 0, "files": 0, "digest": "x" * 64}),
                                          ("snapshot", {"bytes": 0, "files": 0, "digest": "d" * 64, "content": "input"})])
def test_corrupt_journal_never_infers_preexposure_rollback(tmp_path, field, value):
    store = UpgradeStore(tmp_path / "state")
    record = create(store, store.state_root)
    corrupted = {**record, field: value}
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        recovery_action(corrupted)
    if isinstance(value, float) and not (value == value):
        path = store.records / (record["operation_id"] + ".json")
        path.write_text(json.dumps(corrupted))
    else:
        write_private_json(store.records / (record["operation_id"] + ".json"), corrupted)
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        UpgradeStore(store.state_root)


def test_unfinished_recovery_reads_every_record_independent_of_ui_pagination(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    records = [create(store, store.state_root) for _ in range(105)]
    store.update(records[0]["operation_id"], "canceled")
    store.update(records[1]["operation_id"], "recovery_required", error_code="upgrade_recovery_required")
    remaining = UpgradeStore(store.state_root).list_unfinished()
    assert len(remaining) == 104
    assert records[1]["operation_id"] in {record["operation_id"] for record in remaining}


def test_journal_capacity_does_not_block_idempotent_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrades, "MAX_UPGRADES", 1)
    store = UpgradeStore(tmp_path / "state")
    record = create(store, store.state_root)
    with pytest.raises(UpgradeError, match="^upgrade_limit$"):
        create(store, store.state_root)
    assert store.create(record["operation_id"], record["plan_id"], record["old"], record["candidate"], record["old_activation"]) == record


def test_journal_clock_rollback_keeps_timestamp_monotonic(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrades.time, "time", lambda: 100.0)
    store = UpgradeStore(tmp_path / "state")
    record = create(store, store.state_root)
    monkeypatch.setattr(upgrades.time, "time", lambda: 1.0)
    changed = store.update(record["operation_id"], "quiesced")
    assert changed["created_at"] == changed["updated_at"] == 100.0
    assert UpgradeStore(store.state_root).get(record["operation_id"]) == changed


def test_journal_write_failure_does_not_publish_an_in_memory_exposure(tmp_path, monkeypatch):
    store = UpgradeStore(tmp_path / "state")
    record = select(store, create(store, store.state_root))
    def fault(*args, **kwargs):
        raise OSError("synthetic secret material")
    monkeypatch.setattr(upgrades, "write_private_json", fault)
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        store.update(record["operation_id"], "exposed")
    assert store.get(record["operation_id"]) == record
    assert recovery_action(store.get(record["operation_id"])) == "restore_old"


def test_journal_cleans_only_recognized_private_write_temporaries(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    record = create(store, store.state_root)
    temporary = store.records / (str(uuid.uuid4()) + ".json." + "a" * 24 + ".tmp")
    private_file(temporary, b"interrupted atomic write")
    restarted = UpgradeStore(store.state_root)
    assert not temporary.exists()
    assert restarted.get(record["operation_id"]) == record
    unknown = private_file(store.records / "unknown.tmp")
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        UpgradeStore(store.state_root)
    assert unknown.exists()


def test_journal_does_not_follow_or_delete_linked_write_temporary(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    outside = private_file(tmp_path / "retained")
    temporary = store.records / (str(uuid.uuid4()) + ".json." + "a" * 24 + ".tmp")
    temporary.symlink_to(outside)
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        UpgradeStore(store.state_root)
    assert outside.exists() and temporary.is_symlink()


def test_journal_rejects_symlink_or_writable_records_after_startup(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    record = create(store, store.state_root)
    path = store.records / (record["operation_id"] + ".json")
    path.chmod(0o644)
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        store.get(record["operation_id"])
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        store.list_unfinished()


def test_journal_missing_and_invalid_identities_have_stable_codes(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    with pytest.raises(UpgradeError, match="^upgrade_not_found$"):
        store.get(str(uuid.uuid4()))
    for value in ("../outside", None, 1, str(uuid.uuid4()).upper()):
        with pytest.raises(UpgradeError, match="^invalid_upgrade_identity$"):
            store.get(value)


def test_journal_duplicate_fields_and_unknown_keys_fail_closed(tmp_path):
    store = UpgradeStore(tmp_path / "state")
    record = create(store, store.state_root)
    path = store.records / (record["operation_id"] + ".json")
    original = path.read_text()
    path.write_text(original[:-1] + ',"exposed":false}')
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        UpgradeStore(store.state_root)
    path.write_text(original)
    write_private_json(path, {**record, "content": "synthetic data"})
    with pytest.raises(UpgradeError, match="^upgrades_unavailable$"):
        UpgradeStore(store.state_root)
