from __future__ import annotations

import concurrent.futures
import os
import stat
import uuid
from pathlib import Path

import pytest

from mcpme.contracts import PackageError, validate_package
from mcpme.imports import ImportError, ImportStore
from mcpme.private import PrivateStateError
from test_mcpme_packages import edit_manifest, make_package


def source_folder(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "validator.py").write_text("def validate_json(value):\n    return {'valid': True}\n")
    return source


def prepared_for(tmp_path: Path, draft: dict, name: str = "package") -> Path:
    package = make_package(tmp_path / name)
    edit_manifest(package, source_revision=draft["source"]["revision"])
    return package


def test_discovery_summaries_preserve_full_draft_and_bound_metadata(tmp_path, monkeypatch):
    source = source_folder(tmp_path)
    with ImportStore(tmp_path / "state") as store:
        name = "Project " + "x" * 100
        draft = store.create(source, name=name, intent="Selected private requirements")
        summary = store.list_summaries()[0]
        assert summary["summary"] is True and len(summary["name"]) == 64
        assert "intent" not in summary and "files" not in summary["source"]
        full = store.inspect(draft["draft_id"])
        assert full["name"] == name and full["intent"] == "Selected private requirements"
        assert full["source"]["files"]
        monkeypatch.setattr("mcpme.imports.MAX_DRAFT_METADATA_BYTES", 128)
        with pytest.raises(ImportError, match="import_metadata_limit"):
            store.create(source, name="Too much metadata")
        assert len(store.list()) == 1 and (source / "validator.py").exists()


def ready(tmp_path: Path, store: ImportStore) -> tuple[dict, Path]:
    draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validate_json")
    package = prepared_for(tmp_path, draft)
    return store.attach(draft["draft_id"], draft["revision"], package), package


def test_static_intake_excludes_private_and_generated_material(tmp_path, monkeypatch):
    source = source_folder(tmp_path)
    for directory in (".git", ".venv", "node_modules", "build", ".cache", "__pycache__"):
        (source / directory).mkdir()
        (source / directory / "private.py").write_text("Excluded source")
    (source / ".env").write_text("API_TOKEN=synthetic-secret")
    (source / "credentials.json").write_text('{"token":"synthetic-secret"}')
    (source / "private.key").write_text("synthetic key")
    (source / "model.gguf").write_bytes(b"model artifact")
    (source / "README.md").write_text("Documentation")
    def forbid(*args, **kwargs):
        pytest.fail("import intake must not execute selected source")
    monkeypatch.setattr("subprocess.Popen", forbid)
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source, name="Validator", intent="Expose validator")
        assert draft["state"] == "waiting_for_agent"
        assert [item["path"] for item in draft["source"]["files"]] == ["README.md", "validator.py"]
        snapshot = Path(draft["source"]["snapshot_path"])
        assert (snapshot / "validator.py").read_bytes() == (source / "validator.py").read_bytes()
        assert draft["source"]["revision"] == "sha256:" + draft["source"]["digest"]
        (source / "validator.py").write_text("Edited uncommitted source")
        assert (snapshot / "validator.py").read_text().startswith("def validate_json")
        for path in [store.root, store.staging, snapshot]:
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in [store.path, snapshot / "validator.py"]:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        raw = store.path.read_bytes()
        assert b"synthetic-secret" not in raw
        assert b"def validate_json" not in raw
        assert b"agent_instructions" not in raw


def test_explicit_selection_and_hashes_are_durable(tmp_path):
    source = source_folder(tmp_path)
    (source / "unused.py").write_text("Unrelated source")
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source, name="Validator", files=["validator.py"])
        assert draft["state"] == "needs_information"
        assert draft["blockers"] == [{"code": "intent_required", "field": "intent"},
                                     {"code": "prepared_package_required", "field": "attachment"}]
    with ImportStore(tmp_path / "state") as store:
        assert store.inspect(draft["draft_id"]) == draft
        assert store.list() == [draft]
        assert [item["path"] for item in draft["source"]["files"]] == ["validator.py"]


@pytest.mark.parametrize("selection,code", [
    (["../outside.py"], "unsafe_source_path"), ([".env"], "source_file_excluded"),
    (["validator.py", "validator.py"], "invalid_source_selection"),
    ([".venv/private.py"], "source_file_excluded"),
])
def test_bad_source_selection_is_rejected_without_staging(tmp_path, selection, code):
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match=f"^{code}$"):
            store.create(source_folder(tmp_path), name="Validator", files=selection)
        assert store.list() == []
        assert list(store.staging.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "fifo", "private_key", "binary"])
def test_unsafe_source_files_are_rejected(tmp_path, kind):
    source = source_folder(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("Private source")
    if kind == "symlink":
        (source / "extra.py").symlink_to(outside)
    elif kind == "parent_symlink":
        (source / "linked").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, source / "extra.py")
    elif kind == "fifo":
        os.mkfifo(source / "extra.py")
    elif kind == "private_key":
        (source / "validator.py").write_text("-----BEGIN PRIVATE KEY-----\nSynthetic\n")
    else:
        (source / "validator.py").write_bytes(b"binary\0source")
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError):
            store.create(source, name="Validator")
        assert list(store.staging.iterdir()) == []
        assert outside.read_text() == "Private source"


def test_source_and_selection_limits_are_enforced(tmp_path, monkeypatch):
    import mcpme.imports as imports
    source = source_folder(tmp_path)
    monkeypatch.setattr(imports, "MAX_SOURCE_FILE_BYTES", 1)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="^source_size_exceeded$"):
            store.create(source, name="Validator")
    monkeypatch.setattr(imports, "MAX_SOURCE_FILE_BYTES", 1024)
    monkeypatch.setattr(imports, "MAX_SOURCE_FILES", 1)
    (source / "extra.py").write_text("Extra")
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="^source_size_exceeded$"):
            store.create(source, name="Validator")


def test_replaced_source_ancestor_is_never_followed(tmp_path, monkeypatch):
    source = source_folder(tmp_path)
    selected = source / "nested"
    selected.mkdir()
    (selected / "module.py").write_text("Selected source")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "module.py").write_text("Private contents outside source")
    original_open = os.open
    replaced = False
    def replace_before_directory_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "nested" and not replaced and flags & os.O_DIRECTORY:
            replaced = True
            selected.rename(source / "original-nested")
            selected.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", replace_before_directory_open)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="^unsafe_source_file$"):
            store.create(source, name="Validator", files=["nested/module.py"])
        assert replaced
        assert store.list() == []
        assert list(store.staging.iterdir()) == []
        assert (outside / "module.py").read_text() == "Private contents outside source"


@pytest.mark.parametrize("field", ["name", "intent"])
def test_recognized_credentials_in_draft_choices_are_not_persisted(tmp_path, field):
    token = "ghp_" + "a" * 30
    values = {"name": "Validator", "intent": "Expose validator", field: token}
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="^import_credentials_detected$"):
            store.create(source_folder(tmp_path), **values)
        assert store.list() == []
        assert token.encode() not in store.path.read_bytes()


def test_documents_require_intent_and_an_explicit_role(tmp_path):
    brief = tmp_path / "setup.md"
    brief.write_text("Expose a synthetic JSON validator.")
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(brief, name="Validator", intent="Expose validator")
        assert draft["source"]["kind"] == "document"
        assert {item["code"] for item in draft["blockers"]} == {"document_role_required", "prepared_package_required"}
        draft = store.revise(draft["draft_id"], 1, document_role="reference")
        assert "document_template_required" in {item["code"] for item in draft["blockers"]}
        draft = store.revise(draft["draft_id"], 2, document_role="instructions")
        assert draft["state"] == "waiting_for_agent"
        assert draft["revision"] == 3


def test_export_is_generated_without_persisting_instructions(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        exported = store.export(draft["draft_id"], 1)
        assert exported["schema_version"] == 1
        assert exported["template_ids"] == ["python-function-v1", "existing-mcp-v1", "jevk5-llamacpp-v1"]
        assert draft["source"]["revision"] in exported["agent_instructions"]
        assert f"imports attach {draft['draft_id']} --revision 1 --package" in exported["agent_instructions"]
        assert "agent_instructions" not in store.inspect(draft["draft_id"])
        assert b"agent_instructions" not in store.path.read_bytes()
        assert exported["retention_seconds"] == 30 * 24 * 60 * 60


def test_attachment_is_static_bound_and_independent(tmp_path, monkeypatch):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        def forbid(*args, **kwargs):
            pytest.fail("attachment must not execute prepared package")
        monkeypatch.setattr("subprocess.Popen", forbid)
        attached = store.attach(draft["draft_id"], 1, package)
        assert attached["state"] == "ready_to_plan"
        assert store.attach(draft["draft_id"], 1, package) == attached
        assert store.validate_binding(draft["draft_id"], 1, attached["attachment"]["digest"]) == attached
        assert Path(attached["attachment"]["path"]) != package
        edit_manifest(package, name="Changed external workspace")
        assert validate_package(Path(attached["attachment"]["path"])).digest == attached["attachment"]["digest"]
        with pytest.raises(ImportError, match="^import_revision_required$"):
            store.attach(draft["draft_id"], 1, package)


def test_wrong_source_revision_and_stale_package_returns_are_rejected(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        unrelated = make_package(tmp_path / "unrelated")
        with pytest.raises(ImportError, match="^source_identity_mismatch$"):
            store.attach(draft["draft_id"], 1, unrelated)
        package = prepared_for(tmp_path, draft)
        revised = store.revise(draft["draft_id"], 1, intent="Expose validation only")
        with pytest.raises(ImportError, match="^stale_import_revision$"):
            store.attach(draft["draft_id"], 1, package)
        assert store.attach(draft["draft_id"], revised["revision"], package)["state"] == "ready_to_plan"


def test_edit_invalidates_binding_and_reclaims_only_inactive_result(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft, package = ready(tmp_path, store)
        original_snapshot = Path(draft["source"]["snapshot_path"])
        old_attachment = Path(draft["attachment"]["path"])
        old_digest = draft["attachment"]["digest"]
        revised = store.revise(draft["draft_id"], 1, start=True, start_policy="host")
        assert revised["revision"] == 2
        assert revised["attachment"] is None
        assert revised["state"] == "waiting_for_agent"
        assert original_snapshot.exists()
        assert not old_attachment.exists()
        with pytest.raises(ImportError, match="^stale_import_revision$"):
            store.validate_binding(draft["draft_id"], 1, old_digest)
        assert package.exists()
        edit_manifest(package, name="New package revision")
        assert store.attach(draft["draft_id"], 2, package)["state"] == "ready_to_plan"


def test_attached_artifacts_are_reverified_before_plan(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft, _ = ready(tmp_path, store)
        (Path(draft["attachment"]["path"]) / "LICENSE").write_text("Changed artifact")
        with pytest.raises(PackageError):
            store.validate_binding(draft["draft_id"], 1, draft["attachment"]["digest"])


@pytest.mark.parametrize("requirements,blocker", [
    ({"tools": ["unexpected"]}, "tool_contract_mismatch"),
    ({"profile": "model-service"}, "unsupported_process_topology"),
    ({"memory_mb": 32}, "resource_requirement_mismatch"),
    ({"secret_references": ["keychain-reference"]}, "secret_reference_unavailable"),
])
def test_nonsecret_requirements_block_incompatible_return(tmp_path, requirements, blocker):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        draft = store.revise(draft["draft_id"], 1, requirements=requirements)
        draft = store.attach(draft["draft_id"], 2, prepared_for(tmp_path, draft))
        assert draft["state"] == "package_received"
        assert blocker in {item["code"] for item in draft["blockers"]}
        with pytest.raises(ImportError, match="^import_not_ready$"):
            store.validate_binding(draft["draft_id"], 2, draft["attachment"]["digest"])


def test_arbitrary_configuration_and_secret_values_are_rejected(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator")
        for requirements in ({"configuration": {"api_key": "synthetic"}}, {"secret_references": [{"value": "synthetic"}]}):
            with pytest.raises(ImportError, match="^invalid_import_requirements$"):
                store.revise(draft["draft_id"], 1, requirements=requirements)
        assert store.inspect(draft["draft_id"])["revision"] == 1
        assert b"synthetic" not in store.path.read_bytes()


def test_prepared_package_intake_is_ready_without_intent(tmp_path):
    package = make_package(tmp_path / "package")
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(package, name="Prepared")
        assert draft["state"] == "ready_to_plan"
        assert draft["source"]["kind"] == "prepared_package"
        assert draft["source"]["digest"] == draft["attachment"]["digest"]
        revised = store.revise(draft["draft_id"], 1, name="Renamed")
        assert Path(revised["source"]["snapshot_path"]).exists()
        assert store.attach(draft["draft_id"], 2, package)["state"] == "ready_to_plan"


@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_cancel_and_expire_preserve_sources_and_invalidate_binding(tmp_path, action):
    with ImportStore(tmp_path / "state") as store:
        draft, package = ready(tmp_path, store)
        result = getattr(store, action)(draft["draft_id"], 1)
        assert result["state"] == {"cancel": "canceled", "expire": "expired"}[action]
        assert getattr(store, action)(draft["draft_id"], 1) == result
        assert Path(draft["source"]["locator"]).exists()
        assert package.exists()
        assert not Path(draft["source"]["snapshot_path"]).exists()
        assert not Path(draft["attachment"]["path"]).exists()
        for mutation in (lambda: store.attach(draft["draft_id"], 1, package),
                         lambda: store.revise(draft["draft_id"], 1, name="Revised"),
                         lambda: store.validate_binding(draft["draft_id"], 1, draft["attachment"]["digest"]),
                         lambda: store.submit(draft["draft_id"], 1, draft["attachment"]["digest"], str(uuid.uuid4()))):
            with pytest.raises(ImportError, match="^draft_inactive$"):
                mutation()


@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_inactive_cleanup_can_resume_after_interruption(tmp_path, monkeypatch, action):
    import mcpme.imports as imports
    with ImportStore(tmp_path / "state") as store:
        draft, package = ready(tmp_path, store)
        original = imports.shutil.rmtree
        def interrupted(*args, **kwargs):
            raise OSError("Synthetic interrupted cleanup")
        monkeypatch.setattr(imports.shutil, "rmtree", interrupted)
        with pytest.raises(ImportError, match="^import_cleanup_failed$"):
            getattr(store, action)(draft["draft_id"], 1)
        canceled = store.inspect(draft["draft_id"])
        assert canceled["state"] == {"cancel": "canceled", "expire": "expired"}[action]
        assert Path(draft["attachment"]["path"]).exists()
        monkeypatch.setattr(imports.shutil, "rmtree", original)
    with ImportStore(tmp_path / "state") as reopened:
        assert reopened.inspect(draft["draft_id"])["state"] == {"cancel": "canceled", "expire": "expired"}[action]
        assert not Path(draft["source"]["snapshot_path"]).exists()
        assert not Path(draft["attachment"]["path"]).exists()
    with ImportStore(tmp_path / "state") as store:
        assert getattr(store, action)(draft["draft_id"], 1) == canceled
        assert not Path(draft["attachment"]["path"]).exists()
        assert package.exists()
        assert Path(draft["source"]["locator"]).exists()


def test_inactive_startup_cleanup_failure_is_sanitized(tmp_path, monkeypatch):
    import mcpme.imports as imports
    with ImportStore(tmp_path / "state") as store:
        draft, _ = ready(tmp_path, store)
        store.cancel(draft["draft_id"], 1)
    staging = tmp_path / "state" / "imports" / draft["draft_id"]
    staging.mkdir(mode=0o700)
    original = imports.shutil.rmtree
    attempted = []
    def interrupted(*args, **kwargs):
        attempted.append(True)
        raise OSError("private path details")
    monkeypatch.setattr(imports.shutil, "rmtree", interrupted)
    with pytest.raises(ImportError, match="^imports_unavailable$"):
        ImportStore(tmp_path / "state")
    assert attempted == [True]
    monkeypatch.setattr(imports.shutil, "rmtree", original)


def test_inactive_startup_cleanup_rejects_symlink_and_preserves_original(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft, package = ready(tmp_path, store)
        store.cancel(draft["draft_id"], 1)
    staging = tmp_path / "state" / "imports" / draft["draft_id"]
    original = Path(draft["source"]["locator"])
    staging.symlink_to(original, target_is_directory=True)
    with pytest.raises(ImportError, match="^imports_unavailable$"):
        ImportStore(tmp_path / "state")
    assert staging.is_symlink() and original.exists() and package.exists()
    assert (original / "validator.py").exists()


@pytest.mark.parametrize("status", ["pending", "running", "recovery_required", "failed", "canceled", "completed"])
def test_submission_is_idempotent_and_freezes_artifacts_until_operation_terminal(tmp_path, status):
    with ImportStore(tmp_path / "state") as store:
        draft, package = ready(tmp_path, store)
        digest = draft["attachment"]["digest"]
        operation_id, slot_id = str(uuid.uuid4()), str(uuid.uuid4())
        submitted = store.submit(draft["draft_id"], 1, digest, operation_id)
        operation = {"operation_id": operation_id, "status": status, "slot_id": slot_id, "details": {}}
        result = store.operation_status(draft["draft_id"], operation)
        assert result["operation_status"] == status
        assert result["state"] == ("installed" if status == "completed" else "submitted")
        assert result["result_slot_id"] == (slot_id if status == "completed" else None)
        assert store.submit(draft["draft_id"], 1, digest, operation_id) == result
        code = "draft_finalized" if status in {"completed", "failed", "canceled"} else "operation_in_progress"
        for mutation in (lambda: store.revise(draft["draft_id"], 1, name="Revised"),
                         lambda: store.attach(draft["draft_id"], 1, package),
                         lambda: store.cancel(draft["draft_id"], 1),
                         lambda: store.expire(draft["draft_id"], 1),
                         lambda: store.validate_binding(draft["draft_id"], 1, digest)):
            with pytest.raises(ImportError, match=f"^{code}$"):
                mutation()
        assert Path(submitted["attachment"]["path"]).exists()


def test_durable_operation_reconciliation_does_not_rehash_damaged_attachment(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft, _ = ready(tmp_path, store)
        digest = draft["attachment"]["digest"]
        store.validate_binding(draft["draft_id"], 1, digest)
        (Path(draft["attachment"]["path"]) / "LICENSE").write_text("Damaged after durable operation intent")
        operation_id = str(uuid.uuid4())
        submitted = store.submit(draft["draft_id"], 1, digest, operation_id)
        assert submitted["operation_id"] == operation_id
        assert store.submit(draft["draft_id"], 1, digest, operation_id) == submitted
        with pytest.raises(ImportError, match="^import_operation_mismatch$"):
            store.submit(draft["draft_id"], 1, digest, str(uuid.uuid4()))


def test_cancel_apply_race_across_connections_has_one_winner(tmp_path):
    state = tmp_path / "state"
    with ImportStore(state) as first, ImportStore(state) as second:
        draft, _ = ready(tmp_path, first)
        digest = draft["attachment"]["digest"]
        operation_id = str(uuid.uuid4())
        def call(operation):
            try:
                return operation()
            except ImportError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, lambda: first.cancel(draft["draft_id"], 1)),
                       pool.submit(call, lambda: second.submit(draft["draft_id"], 1, digest, operation_id))]
            results = [future.result() for future in futures]
        persisted = first.inspect(draft["draft_id"])
        if persisted["state"] == "canceled":
            assert results[1] == "draft_inactive"
            assert persisted["operation_id"] is None
            assert not Path(draft["attachment"]["path"]).exists()
        else:
            assert persisted["state"] == "submitted"
            assert results[0] == "operation_in_progress"
            assert persisted["operation_id"] == operation_id
            assert Path(draft["attachment"]["path"]).exists()


def test_owner_only_database_is_required_on_resume(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        store.create(source_folder(tmp_path), name="Validator")
        database = store.path
    database.chmod(0o644)
    with pytest.raises(PrivateStateError):
        ImportStore(tmp_path / "state")
