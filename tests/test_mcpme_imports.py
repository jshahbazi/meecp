from __future__ import annotations

import concurrent.futures
import contextlib
import multiprocessing
import os
import stat
import threading
import uuid
from pathlib import Path

import pytest

from mcpme.contracts import PackageError, validate_package
from mcpme.imports import ImportError, ImportStore
from mcpme.private import PrivateStateError
from test_mcpme_packages import edit_manifest, make_package


def _attachment_process(state, draft_id, package, connection, mode):
    """A real independent owner so recovery cannot rely on process-local sets."""
    import mcpme.private as private
    original_validate = private.validate_package

    def paused_validate(path):
        if mode == "copy" and Path(path).name.endswith(".tmp"):
            connection.send(("copy", str(path)))
            connection.recv()
        return original_validate(path)

    private.validate_package = paused_validate
    with ImportStore(Path(state)) as store:
        staged = store.stage_attachment(draft_id, 1, Path(package))
        connection.send(("staged", staged))
        action = connection.recv()
        if action == "admit":
            original_save = store._save

            def paused_save(draft):
                if mode == "publish":
                    connection.send(("publishing", draft["attachment"]["path"]))
                    connection.recv()
                original_save(draft)

            store._save = paused_save
            result = store.admit_attachment(staged)
            connection.send(("admitted", result["attachment"]["digest"]))


def _process_message(connection):
    assert connection.poll(10), "attachment process did not reach its barrier"
    return connection.recv()


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


def test_attachment_stages_outside_imports_and_admits_with_callback(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        staged = store.stage_attachment(draft["draft_id"], 1, package)
        staged_path = Path(staged["path"])
        assert staged_path.parent == store.attachment_staging
        assert staged_path.exists()
        observed = []

        def admission(current, sealed):
            observed.append((current["revision"], sealed.digest, store._connection.in_transaction))

        attached = store.admit_attachment(staged, admission=admission)
        assert observed == [(1, attached["attachment"]["digest"], True)]
        assert not staged_path.exists()
        assert Path(attached["attachment"]["path"]).exists()


def test_staged_attachment_cannot_cross_revision_or_cancel_cleanup(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        staged = store.stage_attachment(draft["draft_id"], 1, package)
        revised = store.revise(draft["draft_id"], 1, intent="Expose only validation")
        with pytest.raises(ImportError, match="^stale_import_revision$"):
            store.admit_attachment(staged)
        assert not Path(staged["path"]).exists()
        assert store.inspect(draft["draft_id"])["revision"] == revised["revision"]

        staged = store.stage_attachment(draft["draft_id"], 2, package)
        store.cancel(draft["draft_id"], 2)
        with pytest.raises(ImportError, match="^draft_inactive$"):
            store.admit_attachment(staged)
        assert not Path(staged["path"]).exists()


def test_startup_cleanup_preserves_live_stage_from_another_store(tmp_path):
    state = tmp_path / "state"
    first = ImportStore(state)
    draft = first.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
    package = prepared_for(tmp_path, draft)
    staged = first.stage_attachment(draft["draft_id"], 1, package)
    second = ImportStore(state)
    assert Path(staged["path"]).exists()
    second.close()
    first.admit_attachment(staged)
    first.close()


def test_slow_staging_does_not_block_other_connection_or_revision(tmp_path, monkeypatch):
    state = tmp_path / "state"
    with ImportStore(state) as first:
        draft = first.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        entered, release = threading.Event(), threading.Event()
        original = __import__("mcpme.imports", fromlist=["snapshot_package"]).snapshot_package

        def paused(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr("mcpme.imports.snapshot_package", paused)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first.stage_attachment, draft["draft_id"], 1, package)
            assert entered.wait(5)
            with ImportStore(state) as second:
                assert second.inspect(draft["draft_id"])["revision"] == 1
                revised = second.revise(draft["draft_id"], 1, intent="Expose only validation")
                assert revised["revision"] == 2
                canceled = second.cancel(draft["draft_id"], 2)
                assert canceled["state"] == "canceled"
            release.set()
            staged = future.result()
        with pytest.raises(ImportError, match="^stale_import_revision$"):
            first.admit_attachment(staged)


def test_concurrent_same_digest_is_idempotent_and_different_digest_loses(tmp_path):
    state = tmp_path / "state"
    with ImportStore(state) as first:
        draft = first.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft, "package-a")
        other = prepared_for(tmp_path, draft, "package-b")
        edit_manifest(other, name="Different package")
        with ImportStore(state) as second:
            stages = [first.stage_attachment(draft["draft_id"], 1, package),
                      second.stage_attachment(draft["draft_id"], 1, package),
                      first.stage_attachment(draft["draft_id"], 1, other)]
            results = []
            for store, stage in ((first, stages[0]), (second, stages[1]), (first, stages[2])):
                try:
                    results.append(store.admit_attachment(stage)["attachment"]["digest"])
                except ImportError as error:
                    results.append(error.code)
            assert results[:2] == [results[0], results[0]]
            assert results[2] == "import_revision_required"


def test_restart_reclaims_publication_when_database_commit_fails(tmp_path, monkeypatch):
    state = tmp_path / "state"
    with ImportStore(state) as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        staged = store.stage_attachment(draft["draft_id"], 1, package)
        monkeypatch.setattr(store, "_save", lambda draft: (_ for _ in ()).throw(ImportError("synthetic_commit_failure")))
        with pytest.raises(ImportError, match="^synthetic_commit_failure$"):
            store.admit_attachment(staged)
        # Cleanup is immediate, not contingent on another host restart.
        assert store.inspect(draft["draft_id"])["attachment"] is None
        assert not list((store.staging / draft["draft_id"]).glob("package-*"))
        assert [item.name for item in store.attachment_staging.iterdir()] == [".lock"]
    with ImportStore(state) as reopened:
        assert not list((reopened.staging / draft["draft_id"]).glob("package-*"))
        assert reopened.attach(draft["draft_id"], 1, package)["state"] == "ready_to_plan"


def test_slow_package_validation_never_blocks_same_store_status(tmp_path, monkeypatch):
    import mcpme.imports as imports
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        entered, release = threading.Event(), threading.Event()
        original = imports.validate_package

        def paused(path):
            if path == package:
                entered.set()
                assert release.wait(5)
            return original(path)

        monkeypatch.setattr(imports, "validate_package", paused)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(store.stage_attachment, draft["draft_id"], 1, package)
            assert entered.wait(5)
            try:
                status = pool.submit(store.inspect, draft["draft_id"]).result(timeout=1)
                assert status["revision"] == 1
                assert store.revise(draft["draft_id"], 1, intent="Revised")["revision"] == 2
            finally:
                release.set()
            staged = future.result(timeout=5)
        with pytest.raises(ImportError, match="stale_import_revision"):
            store.admit_attachment(staged)


def test_stage_mapping_cannot_rebind_a_live_lease_to_another_draft(tmp_path):
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        other = store.create(Path(draft["source"]["locator"]), name="Other", intent="Other")
        stage = store.stage_attachment(draft["draft_id"], 1, prepared_for(tmp_path, draft))
        forged = dict(stage, draft_id=other["draft_id"])
        with pytest.raises(ImportError, match="invalid_attachment_stage"):
            store.admit_attachment(forged)
        assert store.inspect(other["draft_id"])["attachment"] is None
        assert store.admit_attachment(stage)["draft_id"] == draft["draft_id"]


@pytest.mark.parametrize("attached", [False, True])
def test_damaged_existing_or_orphan_package_is_never_trusted(tmp_path, attached):
    from mcpme.private import snapshot_package
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        digest = validate_package(package).digest
        if attached:
            final = Path(store.attach(draft["draft_id"], 1, package)["attachment"]["path"])
        else:
            final = store.staging / draft["draft_id"] / f"package-1-{digest}"
            snapshot_package(package, final)
        # It still exists at the expected digest path but its contents differ.
        artifact = validate_package(final).lock.artifacts[0]
        (final / artifact.path).write_bytes(b"synthetic corruption")
        with pytest.raises(ImportError):
            store.attach(draft["draft_id"], 1, package)
        current = store.inspect(draft["draft_id"])
        assert bool(current["attachment"]) is attached
        assert not [item for item in store.attachment_staging.iterdir() if item.name != ".lock"]


def test_admission_validation_is_outside_write_and_read_locks(tmp_path, monkeypatch):
    import mcpme.imports as imports
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        attached = store.attach(draft["draft_id"], 1, package)
        final = Path(attached["attachment"]["path"])
        stage = store.stage_attachment(draft["draft_id"], 1, package)
        entered, release = threading.Event(), threading.Event()
        original = imports.validate_package

        def paused(path):
            assert not store._connection.in_transaction
            if path == final:
                entered.set()
                assert release.wait(5)
            return original(path)

        monkeypatch.setattr(imports, "validate_package", paused)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(store.admit_attachment, stage)
            assert entered.wait(5)
            try:
                assert pool.submit(store.inspect, draft["draft_id"]).result(timeout=1) == attached
            finally:
                release.set()
            assert future.result(timeout=5) == attached


def test_cross_process_recovery_preserves_live_copy_and_lease(tmp_path):
    state = tmp_path / "state"
    with ImportStore(state) as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
    context = multiprocessing.get_context("spawn")
    connection, child = context.Pipe()
    process = context.Process(target=_attachment_process,
                              args=(str(state), draft["draft_id"], str(package), child, "copy"))
    process.start()
    child.close()
    try:
        event, temporary = _process_message(connection)
        assert event == "copy" and Path(temporary).is_dir()
        with ImportStore(state) as reopened:
            assert Path(temporary).is_dir()
            assert reopened.inspect(draft["draft_id"])["attachment"] is None
        connection.send("continue")
        event, stage = _process_message(connection)
        assert event == "staged"
        with ImportStore(state):
            assert Path(stage["path"]).is_dir()
        connection.send("admit")
        assert _process_message(connection) == ("admitted", stage["digest"])
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        connection.close()


@pytest.mark.parametrize("barrier", ["copy", "staged", "publishing"])
def test_cross_process_crash_reclaims_abandoned_copy_and_publication(tmp_path, barrier):
    state = tmp_path / "state"
    with ImportStore(state) as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
    context = multiprocessing.get_context("spawn")
    connection, child = context.Pipe()
    mode = "copy" if barrier == "copy" else "publish"
    process = context.Process(target=_attachment_process,
                              args=(str(state), draft["draft_id"], str(package), child, mode))
    process.start()
    child.close()
    try:
        event, _ = _process_message(connection)
        if barrier == "publishing":
            assert event == "staged"
            connection.send("admit")
            event, _ = _process_message(connection)
        assert event == barrier
        process.kill()
        process.join(5)
        with ImportStore(state) as reopened:
            assert reopened.inspect(draft["draft_id"])["attachment"] is None
            assert not list((reopened.staging / draft["draft_id"]).glob("package-*"))
            assert [item.name for item in reopened.attachment_staging.iterdir()] == [".lock"]
            assert reopened.attach(draft["draft_id"], 1, package)["state"] == "ready_to_plan"
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        connection.close()


def test_startup_cannot_collect_a_live_uncommitted_publication(tmp_path):
    state = tmp_path / "state"
    with ImportStore(state) as store:
        draft = store.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
    context = multiprocessing.get_context("spawn")
    connection, child = context.Pipe()
    process = context.Process(target=_attachment_process,
                              args=(str(state), draft["draft_id"], str(package), child, "publish"))
    process.start()
    child.close()
    try:
        event, stage = _process_message(connection)
        assert event == "staged"
        connection.send("admit")
        event, final = _process_message(connection)
        assert event == "publishing" and Path(final).is_dir()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ImportStore, state)
            try:
                with pytest.raises(concurrent.futures.TimeoutError):
                    future.result(timeout=0.1)
                assert Path(final).is_dir()
            finally:
                connection.send("continue")
            assert _process_message(connection) == ("admitted", stage["digest"])
            with future.result(timeout=5) as reopened:
                assert reopened.inspect(draft["draft_id"])["attachment"]["digest"] == stage["digest"]
                assert validate_package(Path(final)).digest == stage["digest"]
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        connection.close()


def test_concurrent_admission_revalidates_the_winning_tree(tmp_path, monkeypatch):
    import mcpme.imports as imports
    state = tmp_path / "state"
    with ImportStore(state) as first, ImportStore(state) as second:
        draft = first.create(source_folder(tmp_path), name="Validator", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        stages = [store.stage_attachment(draft["draft_id"], 1, package) for store in (first, second)]
        barrier = threading.Barrier(2)
        validations = []
        original_validate = imports.validate_package

        def observed_validate(path):
            validations.append(Path(path))
            return original_validate(path)

        monkeypatch.setattr(imports, "validate_package", observed_validate)
        for store in (first, second):
            original_transaction = store._transaction

            def synchronised_transaction(original=original_transaction):
                first_attempt = True

                @contextlib.contextmanager
                def transaction():
                    nonlocal first_attempt
                    if first_attempt:
                        first_attempt = False
                        # Both readers saw no attachment before either commits.
                        barrier.wait(timeout=5)
                    with original():
                        yield
                return transaction

            monkeypatch.setattr(store, "_transaction", synchronised_transaction())
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.admit_attachment, stage) for store, stage in zip((first, second), stages)]
            results = [future.result(timeout=5) for future in futures]
        assert results[0] == results[1]
        final = Path(results[0]["attachment"]["path"])
        assert final in validations
        assert validate_package(final).digest == stages[0]["digest"]


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


@pytest.mark.parametrize("action", ["revise", "cancel", "expire"])
def test_startup_recovery_can_win_against_post_commit_cleanup(tmp_path, monkeypatch, action):
    import mcpme.imports as module
    state = tmp_path / "state"
    with ImportStore(state) as first:
        draft, package = ready(tmp_path, first)
        target = Path(draft["attachment"]["path"]) if action == "revise" else first.staging / draft["draft_id"]
        entered, release = threading.Event(), threading.Event()
        original = module.shutil.rmtree

        def paused(path, *args, **kwargs):
            if Path(path) == target:
                entered.set()
                assert release.wait(5), "post-commit cleanup barrier timed out"
            return original(path, *args, **kwargs)

        monkeypatch.setattr(module.shutil, "rmtree", paused)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            operation = (lambda: first.revise(draft["draft_id"], 1, intent="Revised synthetic intent")) if action == "revise" else (
                lambda: getattr(first, action)(draft["draft_id"], 1))
            pending = pool.submit(operation)
            try:
                assert entered.wait(5), "mutation did not commit before cleanup"
                with ImportStore(state) as reopened:
                    observed = reopened.inspect(draft["draft_id"])
                    assert observed["revision"] == (2 if action == "revise" else 1)
                    assert not target.exists()
            finally:
                release.set()
            assert pending.result(timeout=5) == first.inspect(draft["draft_id"])
        assert package.exists() and Path(draft["source"]["locator"]).exists()
        if action == "revise":
            assert Path(draft["source"]["snapshot_path"]).exists()
        assert [entry.name for entry in first.attachment_staging.iterdir()] == [".lock"]


@pytest.mark.parametrize("action", ["revise", "cancel", "expire"])
def test_post_commit_cleanup_can_win_before_recovery_quarantine(tmp_path, monkeypatch, action):
    import mcpme.imports as module
    state = tmp_path / "state"
    with ImportStore(state) as first:
        draft, package = ready(tmp_path, first)
        target = Path(draft["attachment"]["path"]) if action == "revise" else first.staging / draft["draft_id"]
        cleanup_entered, release_cleanup = threading.Event(), threading.Event()
        rename_entered, release_rename = threading.Event(), threading.Event()
        original_remove, original_rename = module._remove_attachment_tree, module.os.rename

        def paused_remove(path):
            if Path(path) == target:
                cleanup_entered.set()
                assert release_cleanup.wait(5), "cleanup ownership barrier timed out"
            return original_remove(path)

        def paused_rename(source, destination, *args, **kwargs):
            if Path(source) == target:
                rename_entered.set()
                assert release_rename.wait(5), "recovery quarantine barrier timed out"
            return original_rename(source, destination, *args, **kwargs)

        monkeypatch.setattr(module, "_remove_attachment_tree", paused_remove)
        monkeypatch.setattr(module.os, "rename", paused_rename)
        reopened = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            operation = (lambda: first.revise(draft["draft_id"], 1, intent="Revised synthetic intent")) if action == "revise" else (
                lambda: getattr(first, action)(draft["draft_id"], 1))
            mutation = pool.submit(operation)
            try:
                assert cleanup_entered.wait(5)
                recovery = pool.submit(ImportStore, state)
                assert rename_entered.wait(5), "recovery did not discover the old tree"
                release_cleanup.set()
                result = mutation.result(timeout=5)
                assert not target.exists()
                release_rename.set()
                reopened = recovery.result(timeout=5)
                assert reopened.inspect(draft["draft_id"]) == result
            finally:
                release_cleanup.set()
                release_rename.set()
                if reopened is not None:
                    reopened.close()
        assert package.exists() and Path(draft["source"]["locator"]).exists()
        assert [entry.name for entry in first.attachment_staging.iterdir()] == [".lock"]


@pytest.mark.parametrize("action", ["revise", "cancel", "expire"])
def test_real_cleanup_permission_failures_are_not_suppressed(tmp_path, monkeypatch, action):
    import mcpme.imports as module
    with ImportStore(tmp_path / "state") as store:
        draft, package = ready(tmp_path, store)
        def denied(*args, **kwargs):
            raise PermissionError("Synthetic cleanup permission failure")
        monkeypatch.setattr(module.shutil, "rmtree", denied)
        if action == "revise":
            with pytest.raises(PermissionError):
                store.revise(draft["draft_id"], 1, intent="Revised synthetic intent")
        else:
            with pytest.raises(ImportError, match="^import_cleanup_failed$"):
                getattr(store, action)(draft["draft_id"], 1)
        assert package.exists() and Path(draft["attachment"]["path"]).exists()


def test_shared_collector_missing_child_does_not_abort_remaining_cleanup(tmp_path, monkeypatch):
    import mcpme.imports as module
    root = tmp_path / "garbage"
    root.mkdir(mode=0o700)
    (root / "first").write_text("Synthetic first artifact")
    (root / "second").write_text("Synthetic second artifact")
    original = module.os.unlink
    removed = []
    def concurrently_removed(path, *args, **kwargs):
        original(path, *args, **kwargs)
        removed.append(path)
        if len(removed) == 1:
            raise FileNotFoundError("A concurrent collector removed this child")
    monkeypatch.setattr(module.os, "unlink", concurrently_removed)
    module._remove_attachment_tree(root)
    assert len(removed) == 2 and not root.exists()


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
