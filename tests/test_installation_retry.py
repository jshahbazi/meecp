from pathlib import Path

import meecp.installation_retry as installation_retry
import pytest
from meecp.imports import ImportError, ImportStore
from meecp.installation_retry import retry_installation
from test_meecp_imports import prepared_for, source_folder


def terminal(store, draft, status="failed"):
    with store._transaction():
        current = store._get(draft["draft_id"], draft["revision"])
        current["operation_id"] = "22222222-2222-4222-8222-222222222222"
        current["operation_status"] = status
        current["state"] = "installed"
        store._save(current)
    return store.inspect(draft["draft_id"])


def ready(tmp_path, store):
    draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator",
                         start=True, start_policy="host",)
    return store.attach(draft["draft_id"], draft["revision"], prepared_for(tmp_path, draft))


def test_retry_reuses_real_verified_bytes_and_choices(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store))
        retry = retry_installation(store, original["draft_id"], original["revision"])
        assert retry["name"] == original["name"]
        assert retry["intent"] == original["intent"]
        assert retry["start"] is True and retry["start_policy"] == "host"
        assert retry["source"]["kind"] == "prepared_package"
        assert retry["attachment"]["digest"] == original["attachment"]["digest"]
        assert Path(retry["attachment"]["path"]).is_dir()
        assert retry["operation_id"] is None


def test_retry_coalesces_across_reopen(tmp_path):
    state = tmp_path / "state"
    with ImportStore(state) as store:
        original = terminal(store, ready(tmp_path, store), "canceled")
        first = retry_installation(store, original["draft_id"], original["revision"])
    with ImportStore(state) as reopened:
        second = retry_installation(reopened, original["draft_id"], original["revision"])
        assert second["draft_id"] == first["draft_id"]


def test_retry_rejects_recovery_and_stale(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store), "recovery_required")
        with pytest.raises(ImportError, match="installation_recovery_required"):
            retry_installation(store, original["draft_id"], original["revision"])
        with pytest.raises(ImportError, match="stale_import_revision"):
            retry_installation(store, original["draft_id"], original["revision"] + 1)
        with pytest.raises(ImportError, match="stale_import_revision"):
            retry_installation(store, original["draft_id"], True)


def test_retry_rejects_changed_attachment(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store))
        package = Path(original["attachment"]["path"])
        (package / "README.md").write_text("changed")
        with pytest.raises(ImportError):
            retry_installation(store, original["draft_id"], original["revision"])


def test_retry_denies_at_capacity_without_publishing(tmp_path, monkeypatch):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store))
        before = sorted(path.name for path in store.staging.iterdir())
        monkeypatch.setattr(installation_retry, "MAX_DRAFTS", 1)
        with pytest.raises(ImportError, match="import_limit_exceeded"):
            retry_installation(store, original["draft_id"], original["revision"])
        assert len(store.list()) == 1
        assert sorted(path.name for path in store.staging.iterdir()) == before


def test_retry_cleans_partial_snapshot_on_failure(tmp_path, monkeypatch):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store))
        seen = {}

        def fail_snapshot(package, destination, **kwargs):
            seen["destination"] = destination
            destination.mkdir(parents=True)
            (destination / "partial").write_text("discard me")
            raise OSError("simulated snapshot failure")

        monkeypatch.setattr(installation_retry, "snapshot_package", fail_snapshot)
        with pytest.raises(ImportError, match="package_snapshot_failed"):
            retry_installation(store, original["draft_id"], original["revision"])
        assert not seen["destination"].exists()
        assert not seen["destination"].parent.exists()
        assert len(store.list()) == 1


def test_retry_rechecks_terminal_operation_before_commit(tmp_path, monkeypatch):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store))
        real_get = store._get
        calls = 0

        def changing_get(*args):
            nonlocal calls
            calls += 1
            value = real_get(*args)
            if calls == 2:
                value["operation_status"] = "running"
            return value

        monkeypatch.setattr(store, "_get", changing_get)
        before = sorted(path.name for path in store.staging.iterdir())
        with pytest.raises(ImportError, match="stale_installation_retry"):
            retry_installation(store, original["draft_id"], original["revision"])
        assert sorted(path.name for path in store.staging.iterdir()) == before


def test_duplicate_retry_returns_current_harmless_revision(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store))
        first = retry_installation(store, original["draft_id"], original["revision"])
        revised = store.revise(first["draft_id"], first["revision"], carry_attachment=True, name=first["name"])
        duplicate = retry_installation(store, original["draft_id"], original["revision"])
        assert duplicate["draft_id"] == first["draft_id"]
        assert duplicate["revision"] == revised["revision"]


def test_duplicate_retry_cannot_apply_changed_effective_choices(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store))
        first = retry_installation(store, original["draft_id"], original["revision"])
        store.revise(first["draft_id"], first["revision"], carry_attachment=True, start_policy="manual", start=False)
        with pytest.raises(ImportError, match="stale_installation_retry"):
            retry_installation(store, original["draft_id"], original["revision"])


def test_retry_rejects_boolean_revision(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        original = terminal(store, ready(tmp_path, store))
        with pytest.raises(ImportError, match="stale_import_revision"):
            retry_installation(store, original["draft_id"], True)
