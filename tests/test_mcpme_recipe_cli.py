"""Offline recipe authoring and owner-only private preparation management."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator
import pytest

from mcpme import __main__ as cli
from mcpme import recipe_sdk
from mcpme.preparation_contracts import ExecutionPolicy, PreparationError
from mcpme.recipe_sdk import check_recipe, read_contract_file, relock_recipe, scaffold_recipe
from mcpme.recipes import validate_bundle
from test_mcpme_management_cli import private_manager, execute


@pytest.fixture(autouse=True)
def restore_process_umask():
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


def descriptor(recipe, lock_digest):
    return {"schema_version": 1, "recipe": {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]},
        "package": {"id": "test.synthetic", "version": "1.0.0", "name": "Synthetic validator"},
        "interface": {"kind": "python-callable", "module": "validator", "function": "validate_json", "tools": [{
            "name": "validate_json", "description": "Synthetic validator", "input_schema": {"type": "object",
                "properties": {"document": {"type": "string"}}, "required": ["document"], "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"valid": {"type": "boolean"}}, "required": ["valid"]}}]},
        "dependencies": {"project": ".", "lock_sha256": lock_digest, "python_version": "3.12.13", "mcp_version": "1.30.0"},
        "source_license": "LICENSE", "inputs": {"label": "Generated"}, "resources": {"memory_mb": 128},
        "validation": [{"id": "synthetic", "tool": "validate_json", "arguments": {"document": "{}"},
                        "expected": {"valid": True}, "synthetic": True}]}


def policy():
    return ExecutionPolicy(schema_version=1, operations=["build", "probe"], wall_seconds=120,
                           disk_bytes=1024 * 1024 * 1024, memory_mb=128)


@pytest.mark.parametrize("component", ["python-function-v1", "existing-mcp-v1", "resolved-python-v1"])
async def test_scaffold_and_check_are_offline_with_closed_license_and_fixture_closure(tmp_path, monkeypatch, component):
    async def forbidden(*args, **kwargs):
        pytest.fail("offline recipe authoring must not access management or execution")
    def no_process(*args, **kwargs):
        pytest.fail("offline recipe authoring must not spawn a process")
    monkeypatch.setattr(cli, "call", forbidden)
    monkeypatch.setattr("subprocess.Popen", no_process)
    root = tmp_path / "new-recipe"
    created = await execute(tmp_path, "recipes", "scaffold", "--output", root, "--id", "local.offline",
                            "--component", component, "--json")
    checked = await execute(tmp_path, "recipes", "check", "--path", root, "--json")
    assert created["digest"] == checked["digest"] and checked["runnable"] is False
    assert checked["kind"] == "declarative" and checked["artifact_count"] == 4
    assert "files" not in checked and "file_bytes" not in checked and not (tmp_path / "manager-connection.json").exists()
    bundle = validate_bundle(root)
    assert set(bundle["files"]) == {"LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", "fixtures/synthetic.json",
                                    "mcpme.recipe.json", "mcpme.recipe.lock.json"}
    assert bundle["manifest"]["steps"][0]["component"] == component
    assert (root / "LICENSE").read_bytes() == (Path(__file__).resolve().parents[1] / "templates/LICENSE").read_bytes()
    fixture = json.loads((root / "fixtures/synthetic.json").read_bytes())
    assert fixture["synthetic"] is True
    assert Draft202012Validator(bundle["manifest"]["input_schema"]).is_valid(fixture["inputs"])
    assert not Draft202012Validator(bundle["manifest"]["input_schema"]).is_valid({"label": "Synthetic", "precision": 7})
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in root.rglob("*") if path.is_file())


def test_scaffold_identity_is_independent_of_workspace_and_existing_destination_is_preserved(tmp_path):
    first = scaffold_recipe(tmp_path / "first", "local.repeatable")
    second = scaffold_recipe(tmp_path / "second", "local.repeatable")
    assert first["digest"] == second["digest"]
    original = (tmp_path / "first/LICENSE").read_bytes()
    with pytest.raises(PreparationError, match="recipe_destination_exists"):
        scaffold_recipe(tmp_path / "first", "local.replacement")
    assert (tmp_path / "first/LICENSE").read_bytes() == original


async def test_edit_relock_check_and_add_new_immutable_version_without_host_changes(tmp_path, monkeypatch):
    root = tmp_path / "recipe"
    first = scaffold_recipe(root, "local.authored")
    original_lock = json.loads((root / "mcpme.recipe.lock.json").read_bytes())
    async with private_manager(tmp_path) as host:
        await execute(host.root, "recipes", "add", "--path", root)
        await execute(host.root, "recipes", "enable", "--id", "local.authored", "--version", "1.0.0")
        manifest_path = root / "mcpme.recipe.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["version"] = "2.0.0"
        manifest["form"][0]["label"] = "Authored tool label"
        manifest["provenance"]["origin"] = "local-authored-v2"
        manifest_path.write_text(json.dumps(manifest))
        notice = root / "THIRD_PARTY_NOTICES.md"
        notice.write_text(notice.read_text() + "\nSynthetic authoring revision.\n")
        artifacts_before = {path.relative_to(root).as_posix(): path.read_bytes()
                            for path in root.rglob("*") if path.is_file() and path.name != "mcpme.recipe.lock.json"}
        async def forbidden(*args, **kwargs):
            pytest.fail("relocking must not access management or execute code")
        with monkeypatch.context() as offline:
            offline.setattr(cli, "call", forbidden)
            offline.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("relocking must not spawn a process"))
            locked = await execute(tmp_path, "recipes", "lock", "--path", root, "--json")
            checked = await execute(tmp_path, "recipes", "check", "--path", root, "--json")
        assert locked["locked"] is True and locked["digest"] == checked["digest"] != first["digest"]
        assert locked["version"] == "2.0.0" and locked["runnable"] is False and "files" not in locked
        assert artifacts_before == {path.relative_to(root).as_posix(): path.read_bytes()
                                    for path in root.rglob("*") if path.is_file() and path.name != "mcpme.recipe.lock.json"}
        updated_lock = json.loads((root / "mcpme.recipe.lock.json").read_bytes())
        metadata = lambda value: {record["path"]: (record["origin"], record["license"]) for record in value["artifacts"]}
        assert metadata(updated_lock) == metadata(original_lock)
        assert updated_lock["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        second = await execute(host.root, "recipes", "add", "--path", root)
        assert second["version"] == "2.0.0" and second["digest"] == checked["digest"]
        assert host.recipes.resolve("local.authored", "1.0.0", first["digest"], existing_job=True)["digest"] == first["digest"]
        assert host.registry.list_slots() == []
    assert "rendered in generated native forms" in (root / "README.md").read_text()


def test_lock_main_emits_static_metadata_and_retains_production_private_umask(tmp_path, capsys):
    root = tmp_path / "recipe"
    scaffold_recipe(root, "local.main")
    assert cli.main(["recipes", "lock", "--path", str(root), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["locked"] is True and result["runnable"] is False and "files" not in result
    assert os.umask(0o077) == 0o077


@pytest.mark.parametrize("variant", ["unknown-manifest", "credentials", "empty-license", "missing-license",
                                    "unlicensed-record", "unlisted-file", "symlink", "hardlink", "unsafe-mode",
                                    "oversized-metadata"])
def test_relock_rejects_invalid_or_unlicensed_private_tree_without_changing_lock(tmp_path, variant):
    root = tmp_path / "recipe"
    scaffold_recipe(root, "local.invalid")
    manifest_path = root / "mcpme.recipe.json"
    lock_path = root / "mcpme.recipe.lock.json"
    manifest = json.loads(manifest_path.read_bytes())
    if variant == "unknown-manifest":
        manifest["script"] = "unexpected"
        manifest_path.write_text(json.dumps(manifest))
    elif variant == "credentials":
        manifest["provenance"]["origin"] = "sk-" + "a" * 24
        manifest_path.write_text(json.dumps(manifest))
    elif variant == "empty-license":
        (root / "LICENSE").write_bytes(b"")
    elif variant == "missing-license":
        (root / "LICENSE").unlink()
    elif variant == "unlicensed-record":
        lock = json.loads(lock_path.read_bytes())
        lock["artifacts"][0]["license"] = "UNDECLARED-LICENSE"
        lock_path.write_text(json.dumps(lock))
    elif variant == "unlisted-file":
        (root / "undeclared.txt").write_text("Synthetic undeclared artifact")
        (root / "undeclared.txt").chmod(0o600)
    elif variant == "symlink":
        (root / "linked.txt").symlink_to(root / "LICENSE")
    elif variant == "hardlink":
        os.link(root / "LICENSE", root / "linked.txt")
    elif variant == "unsafe-mode":
        (root / "README.md").chmod(0o644)
    else:
        manifest_path.write_bytes(b" " * (512 * 1024 + 1))
    before = lock_path.read_bytes()
    with pytest.raises(PreparationError):
        relock_recipe(root)
    assert lock_path.read_bytes() == before
    assert not list(tmp_path.glob(".mcpme-recipe-lock-*"))


def test_relock_restores_original_lock_if_postpublication_validation_fails(tmp_path, monkeypatch):
    root = tmp_path / "recipe"
    scaffold_recipe(root, "local.rollback")
    lock_path = root / "mcpme.recipe.lock.json"
    before = lock_path.read_bytes()
    notice = root / "THIRD_PARTY_NOTICES.md"
    notice.write_text(notice.read_text() + "\nSynthetic updated notice.\n")
    original_check = recipe_sdk.check_recipe
    def fail_after_publish(path):
        if Path(path) == root:
            raise PreparationError("synthetic_verification_failure")
        return original_check(path)
    monkeypatch.setattr(recipe_sdk, "check_recipe", fail_after_publish)
    with pytest.raises(PreparationError, match="synthetic_verification_failure"):
        relock_recipe(root)
    assert lock_path.read_bytes() == before
    assert notice.read_text().endswith("Synthetic updated notice.\n")
    assert not list(tmp_path.glob(".mcpme-recipe-lock-*"))


def test_relock_failed_atomic_replace_preserves_original_lock_and_artifacts(tmp_path, monkeypatch):
    root = tmp_path / "recipe"
    scaffold_recipe(root, "local.writefail")
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    def fail_replace(*args, **kwargs):
        raise OSError("synthetic replacement failure")
    monkeypatch.setattr(recipe_sdk.os, "replace", fail_replace)
    with pytest.raises(OSError):
        relock_recipe(root)
    assert before == {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert not list(tmp_path.glob(".mcpme-recipe-lock-*"))


@pytest.mark.parametrize("recipe_id,version", [("invalid", "1.0.0"), ("local.test", "01.0.0")])
def test_scaffold_invalid_identity_is_actionable_and_creates_no_output(tmp_path, capsys, recipe_id, version):
    output = tmp_path / "not-created"
    assert cli.main(["recipes", "scaffold", "--output", str(output), "--id", recipe_id,
                     "--version", version, "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "invalid_recipe_schema" and not output.exists()


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'[]'])
async def test_descriptor_loader_rejects_duplicate_nonfinite_and_nonobject_inputs_before_api(tmp_path, monkeypatch, raw):
    path = tmp_path / "descriptor.json"
    path.write_bytes(raw)
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid descriptor must not be sent to management")
    monkeypatch.setattr(cli, "call", forbidden)
    with pytest.raises(PreparationError):
        await execute(tmp_path, "preparations", "plan", "--import", str(uuid.uuid4()), "--revision", "1", "--descriptor", path)


def test_descriptor_reader_rejects_links_and_is_bounded(tmp_path):
    original = tmp_path / "descriptor.json"
    original.write_text("{}")
    link = tmp_path / "linked.json"
    link.symlink_to(original)
    with pytest.raises(PreparationError):
        read_contract_file(link)
    original.write_bytes(b" " * (512 * 1024 + 1))
    with pytest.raises(PreparationError):
        read_contract_file(original)


@pytest.mark.parametrize("action", ["inspect", "enable", "disable", "revoke", "remove", "deprecate"])
async def test_recipe_actions_use_structured_ids_and_preserve_explicit_scoped_credential(tmp_path, monkeypatch, action):
    credential = tmp_path / "synthetic-grant.json"
    async def request(state, method, route, body, *, credential_file):
        assert state == tmp_path and method == "POST" and route == "/v1/recipes/" + action
        assert body == {"recipe_id": "local.synthetic", "version": "1.0.0"}
        assert credential_file == credential
        return {"error": {"code": "grant_scope"}}
    monkeypatch.setattr(cli, "call", request)
    result = await execute(tmp_path, "--credential-file", credential, "recipes", action,
                           "--id", "local.synthetic", "--version", "1.0.0")
    assert result["error"]["code"] == "grant_scope"


async def test_preparation_cli_sends_bounded_descriptor_and_no_execution_options(tmp_path, monkeypatch):
    path = tmp_path / "descriptor.json"
    path.write_text('{"schema_version":1}')
    import_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    calls = []
    async def request(state, method, route, body):
        calls.append((method, route, body))
        return {"execution_available": False}
    monkeypatch.setattr(cli, "call", request)
    await execute(tmp_path, "preparations", "plan", "--import", import_id, "--revision", "2", "--descriptor", path)
    await execute(tmp_path, "preparations", "list")
    await execute(tmp_path, "preparations", "get", job_id)
    await execute(tmp_path, "preparations", "cancel", job_id)
    assert calls == [("POST", "/v1/preparations/plan", {"import_id": import_id, "revision": 2,
                                                        "descriptor": {"schema_version": 1}}),
                     ("GET", "/v1/preparations", None), ("GET", "/v1/preparations/" + job_id, None),
                     ("POST", "/v1/preparations/" + job_id + "/cancel", {})]
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["preparations", "start"])


async def test_owner_add_enable_variant_and_restart_through_private_cli(tmp_path):
    first_path = tmp_path / "first-recipe"
    second_path = tmp_path / "second-recipe"
    first = scaffold_recipe(first_path, "local.first")
    second = scaffold_recipe(second_path, "local.second")
    async with private_manager(tmp_path) as host:
        defaults = {record["recipe_id"] for record in host.recipes.list()}
        assert defaults == {"mcpme.python-callable", "mcpme.resolved-python", "mcpme.onnx-hotdog", "mcpme.text-generation"}
        for path, entry in ((first_path, first), (second_path, second)):
            added = await execute(host.root, "recipes", "add", "--path", path)
            assert added["digest"] == entry["digest"] and added["state"] == "proposed"
            enabled = await execute(host.root, "recipes", "enable", "--id", entry["recipe_id"], "--version", "1.0.0")
            assert enabled["state"] == "enabled" and enabled["runnable"] is False
            assert host.recipes.resolve(entry["recipe_id"], "1.0.0", entry["digest"])["digest"] == entry["digest"]
        listing = await execute(host.root, "recipes", "list")
        assert {record["recipe_id"] for record in listing["recipes"]} == defaults | {"local.first", "local.second"}
        assert host.registry.list_slots() == []
    async with private_manager(tmp_path) as host:
        restored = await execute(host.root, "recipes", "inspect", "--id", "local.first", "--version", "1.0.0")
        assert restored["digest"] == first["digest"] and restored["state"] == "enabled"
        assert host.registry.list_slots() == []


@pytest.mark.parametrize("mode", ["read", "prepare"])
async def test_existing_scoped_grants_cannot_manage_recipes_or_preparation_jobs(tmp_path, mode):
    path = tmp_path / "recipe"
    scaffold_recipe(path, "local.owner")
    async with private_manager(tmp_path) as host:
        baseline = host.recipes.list()
        grant = await execute(host.root, "authorize", "--read-only" if mode == "read" else "--prepare")
        scoped = ("--credential-file", grant["credential_file"])
        for command in (("recipes", "list"), ("recipes", "add", "--path", path),
                        ("recipes", "enable", "--id", "local.owner", "--version", "1.0.0"),
                        ("preparations", "list")):
            denied = await execute(host.root, *scoped, *command)
            assert denied["error"]["code"] == "grant_scope"
        assert host.recipes.list() == baseline and host.preparations.list() == []


async def test_owner_preparation_plan_get_cancel_and_unknown_descriptor_fields(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "validator.py").write_text("raise RuntimeError('planning must not execute source')\n")
    (source / "LICENSE").write_text("Synthetic source notice")
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    locked = b"version = 1\nrevision = 3\n"
    (source / "uv.lock").write_bytes(locked)
    recipe_path = tmp_path / "recipe"
    scaffold_recipe(recipe_path, "local.plan")
    async with private_manager(tmp_path) as host:
        added = await execute(host.root, "recipes", "add", "--path", recipe_path)
        await execute(host.root, "recipes", "enable", "--id", "local.plan", "--version", "1.0.0")
        draft = await execute(host.root, "imports", "create", "--source", source, "--name", "Synthetic", "--intent", "Expose validator")
        value = descriptor(added, hashlib.sha256(locked).hexdigest())
        descriptor_path = tmp_path / "descriptor.json"
        descriptor_path.write_text(json.dumps(value))
        plan = await execute(host.root, "preparations", "plan", "--import", draft["draft_id"],
                             "--revision", "1", "--descriptor", descriptor_path)
        assert plan["execution_available"] is False and plan["recipe"]["digest"] == added["digest"]
        job = host.preparations.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
        assert (await execute(host.root, "preparations", "get", job["job_id"]))["state"] == "queued"
        assert (await execute(host.root, "preparations", "cancel", job["job_id"]))["state"] == "canceled"
        assert (await execute(host.root, "preparations", "list"))["preparations"][0]["job_id"] == job["job_id"]
        value["shell"] = "unrestricted"
        descriptor_path.write_text(json.dumps(value))
        rejected = await execute(host.root, "preparations", "plan", "--import", draft["draft_id"],
                                 "--revision", "1", "--descriptor", descriptor_path)
        assert "error" in rejected and host.registry.list_slots() == []
