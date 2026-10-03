from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from mcpme.cli_execution import CliExecutionError, CliExecutor
from mcpme.ownership_receipts import prepare_receipt, verify_receipt
from mcpme.preparation_cli import CliJsonComponent, inspect_cli_directory, parse_cli_spec
from mcpme.preparation_cli_forms import CliJsonFormProvider
from mcpme.preparation_components import ComponentRegistry, parse_component_descriptor
from mcpme.preparation_contracts import PreparationError
from mcpme.private import write_private_json
from mcpme.processes import ProcessOwner

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(platform.system() != "Darwin", reason="macOS native confinement support-matrix case")


def spec(script="command.py", **changes):
    value = {"schema_version": 1, "runtime": "private-python-stdlib-v1", "script": script,
        "source_license": "LICENSE", "tool": {"name": "fixture", "description": "Nonsecret deterministic fixture",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {
            "value": {"type": "string", "maxLength": 4096}}, "required": ["value"]},
        "output_schema": {"type": "object", "additionalProperties": False, "properties": {
            "value": {"type": "string"}, "argument": {"type": "string"}}, "required": ["value", "argument"]}},
        "argv": [{"field": "value"}], "topology": "macos-no-fork-v1", "effects": "package-read-only-no-network-v1"}
    value.update(changes)
    return parse_cli_spec(value)


@pytest.fixture(scope="module")
def native_helpers(tmp_path_factory):
    output = tmp_path_factory.mktemp("cli-native-helpers")
    for source, name in (("scripts/mcpme-reap.c", "mcpme-reap"),
                         ("scripts/mcpme-cli-launch.c", "mcpme-cli-launch"),
                         ("tests/fixtures/cli/foreground.c", "foreground"),
                         ("tests/fixtures/cli/escape.c", "escape")):
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(ROOT / source),
                        "-o", str(output / name)], check=True, capture_output=True)
    return output


@pytest.fixture(scope="module")
def command_package(tmp_path_factory):
    output = tmp_path_factory.mktemp("cli-private-runtime")
    runtime = Path(sys.executable).resolve().parent.parent
    assert runtime.name.startswith("cpython-3.12.13-")
    shutil.copytree(runtime, output / "python", symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (output / "source").mkdir(mode=0o700)
    for path in output.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
    return output


def make_executor(tmp_path, command_package, native_helpers, code, **changes):
    script = tmp_path.name + ".py"
    (command_package / "source" / script).write_text(code)
    state = tmp_path / "state"
    executor = CliExecutor(command_package, state, native_helpers / "mcpme-reap", native_helpers / "mcpme-cli-launch")
    return executor, spec(script, **changes)


async def wait_spawn(executor):
    async with asyncio.timeout(5):
        while not executor.owner.instances or next(iter(executor.owner.instances.values())).pid is None:
            await asyncio.sleep(0.01)
    return next(iter(executor.owner.instances.values()))


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_actual_json_stdin_fixed_argv_no_shell_and_durable_settlement(tmp_path, command_package, native_helpers):
    code = "import json,sys; value=json.load(sys.stdin); print(json.dumps({'value':value['value'],'argument':sys.argv[1]}))\n"
    executor, contract = make_executor(tmp_path, command_package, native_helpers, code)
    literal = "$(touch forbidden); `false`; *; $HOME"
    result = await executor.execute(contract, {"value": literal})
    assert result.value == {"value": literal, "argument": literal}
    assert result.returncode == 0 and result.peak_memory_bytes > 0 and result.stdout_bytes > 0
    assert executor.reconcile() == {"pending": 0, "reserved_memory_mb": 0}
    assert not executor.owner.instances
    journal = executor.journal_path.read_text()
    assert literal not in journal and "value" not in journal and "argv" not in journal
    assert all(verify_receipt(executor.state, item["receipt"]) for item in executor.entries["entries"])
    await executor.close()
    restarted = CliExecutor(command_package, executor.state, native_helpers / "mcpme-reap", native_helpers / "mcpme-cli-launch")
    assert (await restarted.execute(contract, {"value": "restart"})).value["value"] == "restart"
    await restarted.close()


@pytest.mark.parametrize("mode,expected", [("foreground", {"input_bytes": 3, "foreground": True}),
    ("fork", {"fork_denied": True, "requested_depth": 1}),
    ("descendant", {"fork_denied": True, "requested_depth": 2}), ("setsid", {"setsid_denied": True})])
async def test_real_native_command_topology_requests_denied(tmp_path, native_helpers, mode, expected):
    # This native fixture is separate from the Python JSON adapter fixture.
    package = tmp_path / "package"
    package.mkdir(mode=0o700)
    shutil.copyfile(native_helpers / "foreground", package / "foreground")
    (package / "foreground").chmod(0o700)
    owner = ProcessOwner(native_helpers / "mcpme-reap")
    process = await owner.launch(native_helpers / "mcpme-cli-launch", [str(package), str(package / "foreground"), mode],
        package, {}, owned_group=True, stop_grace=0.1)
    process.stdin.write(b"{}\n")
    await process.stdin.drain()
    process.stdin.close()
    assert json.loads(await process.stdout.read()) == expected
    assert await process.wait() == 0 and process.peak_memory_bytes > 0
    process.close_stdio()
    assert not owner.instances
    await owner.close()


async def test_unconfined_group_receipt_does_not_prove_escaped_descendant_settlement(tmp_path, native_helpers):
    state = tmp_path / "state"
    descriptor, receipt = prepare_receipt(state, "c" * 32)
    owner = ProcessOwner(native_helpers / "mcpme-reap")
    process = await owner.launch(native_helpers / "escape", [], tmp_path, {}, owned_group=True,
        instance_id=receipt["instance_id"], completion_fd=descriptor, completion_nonce=receipt["nonce"], stop_grace=0.1)
    os.close(descriptor)
    escaped = json.loads(await process.stdout.readline())["escaped_child"]
    try:
        assert await process.wait() == 0
        assert verify_receipt(state, receipt) and alive(escaped)
        # The witness establishes the A3 gap. A complete C frame is proof for
        # the held leader, so the production no-fork case denies this topology.
    finally:
        process.stdin.write(b"X")
        await process.stdin.drain()
        process.stdin.close()
        async with asyncio.timeout(5):
            while alive(escaped):
                await asyncio.sleep(0.02)
        process.close_stdio()
        await owner.close()


@pytest.mark.parametrize("code,changes,error", [
    ("import sys; sys.stdout.write('x'*1000)", {"stdout_limit": 32}, "cli_output_limit"),
    ("import sys; print('nonsecret'); print('discarded',file=sys.stderr); sys.exit(7)",
        {"exit_errors": {"7": "cli_exit_rejected"}}, "cli_exit_rejected"),
    ("import sys; sys.exit(9)", {}, "cli_command_failed"),
    ("import os,signal; os.kill(os.getpid(),signal.SIGKILL)", {}, "cli_command_signaled"),
    ("print('{\"duplicate\":1,\"duplicate\":2}')", {}, "invalid_tool_result"),
    ("print('{\"value\":NaN}')", {}, "invalid_tool_result"),
    ("print('{}')", {}, "invalid_tool_result"),
    ("import time; time.sleep(60)", {"deadline_seconds": 1}, "cli_timeout"),
])
async def test_output_exit_deadline_and_crash_failures_settle(tmp_path, command_package, native_helpers, code, changes, error, capfd):
    executor, contract = make_executor(tmp_path, command_package, native_helpers, code, **changes)
    with pytest.raises(CliExecutionError, match=error):
        await executor.execute(contract, {"value": "fixture"})
    assert not executor.owner.instances
    assert executor.reconcile()["reserved_memory_mb"] == 0
    assert "discarded" not in "".join(capfd.readouterr())
    await executor.close()


async def test_command_cannot_read_original_source_write_or_network(tmp_path, command_package, native_helpers):
    forbidden = tmp_path / "original-source"
    forbidden.write_text("nonsecret-fixture")
    code = "\n".join(["import json,os,socket,sys", "checks=[]", "try:",
        "    open(" + repr(str(forbidden)) + ").read()", "except PermissionError:", "    checks.append('read')",
        "try:", "    open('forbidden-write','w').write('fixture')", "except PermissionError:", "    checks.append('write')",
        "try:", "    socket.create_connection(('127.0.0.1',9),timeout=0.1)", "except PermissionError:",
        "    checks.append('network')", "try:", "    os.fork()", "except PermissionError:", "    checks.append('fork')",
        "print(json.dumps({'value':','.join(checks),'argument':sys.argv[1]}))"])
    executor, contract = make_executor(tmp_path, command_package, native_helpers, code)
    result = await executor.execute(contract, {"value": "fixture"})
    assert result.value == {"value": "read,write,network,fork", "argument": "fixture"}
    assert not (command_package / "source/forbidden-write").exists()
    await executor.close()


async def test_posix_spawn_of_allowed_interpreter_is_also_denied(tmp_path, command_package, native_helpers):
    code = "\n".join(["import json,os,sys", "denied=False", "try:",
        "    child=os.posix_spawn(sys.executable,[sys.executable,'-I','-S','-B','-c','pass'],{})",
        "    os.waitpid(child,0)", "except PermissionError:", "    denied=True",
        "print(json.dumps({'value':'denied' if denied else 'spawned','argument':sys.argv[1]}))"])
    executor, contract = make_executor(tmp_path, command_package, native_helpers, code)
    result = await executor.execute(contract, {"value": "fixture"})
    assert result.value["value"] == "denied"
    await executor.close()


async def test_repeat_cancel_retains_reservation_until_receipt(tmp_path, command_package, native_helpers):
    executor, contract = make_executor(tmp_path, command_package, native_helpers,
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)")
    task = asyncio.create_task(executor.execute(contract, {"value": "fixture"}))
    process = await wait_spawn(executor)
    assert executor.entries["entries"][0]["reserved_memory_mb"] == 128
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(process.pid) and not executor.owner.instances
    assert executor.reconcile()["reserved_memory_mb"] == 0
    await executor.close()


async def test_wrapper_sigkill_retains_unsettled_resources_after_natural_target_exit(tmp_path, command_package, native_helpers):
    executor, contract = make_executor(tmp_path, command_package, native_helpers, "import time; time.sleep(1.5)")
    task = asyncio.create_task(executor.execute(contract, {"value": "fixture"}))
    process = await wait_spawn(executor)
    process.wrapper.kill()
    with pytest.raises(CliExecutionError, match="cli_ownership_unsettled"):
        await task
    assert process.ownership_lost and executor.reconcile() == {"pending": 1, "reserved_memory_mb": 128}
    async with asyncio.timeout(5):
        while alive(process.pid):
            await asyncio.sleep(0.02)
    # Observed disappearance cannot replace native reaping evidence.
    with pytest.raises(Exception, match="ownership_lost"):
        await executor.close()
    restarted = CliExecutor(command_package, executor.state, native_helpers / "mcpme-reap", native_helpers / "mcpme-cli-launch")
    assert restarted.reconcile() == {"pending": 1, "reserved_memory_mb": 128}
    with pytest.raises(CliExecutionError, match="cli_ownership_unsettled"):
        await restarted.execute(contract, {"value": "fixture"})
    await restarted.close()


async def test_measured_over_budget_result_is_rejected_after_settlement(tmp_path, command_package, native_helpers):
    code = "import json,sys; allocated=bytearray(8*1024*1024); print(json.dumps({'value':'fixture','argument':sys.argv[1]}))"
    executor, contract = make_executor(tmp_path, command_package, native_helpers, code)
    executor.memory_mb = 1
    with pytest.raises(CliExecutionError, match="cli_memory_budget_exceeded"):
        await executor.execute(contract, {"value": "fixture"})
    assert executor.reconcile()["reserved_memory_mb"] == 0
    assert executor.entries["entries"][0]["measurements"]["peak_memory_bytes"] > 1024 * 1024
    await executor.close()


async def test_missing_receipt_restart_never_adopts_or_replays(tmp_path, command_package, native_helpers):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    descriptor, receipt = prepare_receipt(state, "a" * 32)
    os.close(descriptor)
    write_private_json(state / "invocations.json", {"schema_version": 1, "entries": [{"receipt": receipt,
        "contract_digest": "b" * 64, "declared_memory_mb": 128, "reserved_memory_mb": 128,
        "state": "pending", "outcome": "pending", "measurements": None}]})
    executor = CliExecutor(command_package, state, native_helpers / "mcpme-reap", native_helpers / "mcpme-cli-launch")
    assert executor.reconcile() == {"pending": 1, "reserved_memory_mb": 128}
    with pytest.raises(CliExecutionError, match="cli_ownership_unsettled"):
        await executor.execute(spec(), {"value": "fixture"})
    assert not executor.owner.instances
    await executor.close()


async def test_input_and_argument_bounds_reject_before_launch(tmp_path, command_package, native_helpers):
    executor, contract = make_executor(tmp_path, command_package, native_helpers, "raise SystemExit(71)", stdin_limit=8)
    with pytest.raises(CliExecutionError, match="cli_input_limit"):
        await executor.execute(contract, {"value": "fixture"})
    contract = spec(contract.script, argument_limit=2)
    with pytest.raises(CliExecutionError, match="cli_argument_limit"):
        await executor.execute(contract, {"value": "fixture"})
    with pytest.raises(CliExecutionError, match="invalid_tool_arguments"):
        await executor.execute(contract, {"unknown": "fixture"})
    assert not executor.entries["entries"] and not executor.owner.instances
    await executor.close()


async def test_tampered_terminal_receipt_reinstates_reservation_on_restart(tmp_path, command_package, native_helpers):
    code = "import json,sys; print(json.dumps({'value':'fixture','argument':sys.argv[1]}))"
    executor, contract = make_executor(tmp_path, command_package, native_helpers, code)
    await executor.execute(contract, {"value": "fixture"})
    item = executor.entries["entries"][0]
    receipt_path = executor.state / "ownership-receipts" / (item["receipt"]["instance_id"] + ".receipt")
    await executor.close()
    with receipt_path.open("r+b") as stream:
        stream.seek(104)
        stream.write(b"P")
    restarted = CliExecutor(command_package, executor.state, native_helpers / "mcpme-reap", native_helpers / "mcpme-cli-launch")
    assert restarted.reconcile() == {"pending": 1, "reserved_memory_mb": 128}
    with pytest.raises(CliExecutionError, match="cli_ownership_unsettled"):
        await restarted.execute(contract, {"value": "fixture"})
    await restarted.close()


def source_draft(directory):
    records = [{"path": path.relative_to(directory).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size} for path in sorted(directory.rglob("*")) if path.is_file()]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"source": {"kind": "local_folder", "snapshot_path": str(directory), "digest": digest,
            "revision": "sha256:" + digest, "files": records}}


def test_directory_default_recipe_and_missing_questions_are_static(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    marker = tmp_path / "executed"
    (source / "cli.py").write_text("from pathlib import Path; Path(" + repr(str(marker)) + ").touch()")
    candidates = inspect_cli_directory(source_draft(source))
    assert len(candidates) == 1 and candidates[0]["default_recipe"] == "mcpme.cli-json"
    assert "cli_json_contract_required" in candidates[0]["unresolved"]
    assert not marker.exists() and not candidates[0]["execution_available"]
    (source / "LICENSE").write_text("Synthetic fixture license")
    (source / "mcpme.cli.json").write_text(json.dumps(spec("cli.py").model_dump()))
    candidates = inspect_cli_directory(source_draft(source))
    assert candidates[0]["questions"] == ["synthetic_arguments", "synthetic_expected"]
    assert candidates[0]["tools"][0]["name"] == "fixture"
    assert not marker.exists() and not candidates[0]["execution_available"]


def test_unknown_daemon_topology_and_unbounded_argv_rejected():
    for updates in ({"topology": "daemon"}, {"runtime": "system-python"}, {"shell": True},
                    {"argv": [{"field": "missing"}]}, {"argv": ["\0"]}, {"exit_errors": {"0": "cli_exit_ok"}}):
        with pytest.raises(PreparationError):
            spec(**updates)


def test_generated_directory_questions_resolve_inline_contract_and_bind_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "cli.py").write_text("raise SystemExit(71)\n")
    (source / "LICENSE").write_text("Synthetic source license")
    draft = {**source_draft(source), "name": "Directory CLI", "intent": "Echo bounded synthetic input"}
    candidate = inspect_cli_directory(draft)[0]
    component = CliJsonComponent()
    recipe_reference = {"id": "mcpme.cli-json", "version": "1.0.0", "digest": "d" * 64}
    recipe = {"digest": recipe_reference["digest"], "manifest": {"id": recipe_reference["id"],
        "version": "1.0.0", "steps": [{"component": component.reference.id, "version": "1.0.0", "digest": component.reference.digest}]}}
    provider = CliJsonFormProvider()
    assert provider.accepts(candidate, recipe)
    fields, bindings, blockers = provider.fields(draft, candidate, recipe)
    assert blockers == ["cli_topology_acceptance_required"]
    values = {field.id: field.value for field in fields}
    for identity, binding in bindings.items():
        if binding[0] in {"arguments", "expected"}:
            values[identity] = {"value": "synthetic"}
    context = SimpleNamespace(bindings=bindings, candidate=candidate,
        form=SimpleNamespace(recipe=SimpleNamespace(model_dump=lambda: recipe_reference),
                             import_id="00000000-0000-4000-8000-000000000001"))
    descriptor = parse_component_descriptor(provider.resolve(context, values))
    registry = ComponentRegistry((component,))
    binding = registry.validate_source(descriptor, draft, recipe)[0]
    assert binding.interface_digest == parse_cli_spec(descriptor.components[0].input["spec"]).digest
    assert {item.path for item in binding.files} == {"LICENSE", "cli.py"}
    assert not (source / "mcpme.cli.json").exists()


def static_runtime(destination, dependency_project):
    # Contract-only builder fixture. Actual execution uses the separate SDK
    # acceptance harness and the real copied interpreter.
    (destination / "python/bin").mkdir(parents=True)
    (destination / "python/bin/python3.12").write_text("never executed static fixture\n")
    (destination / "python/bin/python3.12").chmod(0o700)
    (destination / "licenses/python-LICENSE.txt").write_text("Static fixture license\n")
    (destination / "licenses/DEPENDENCIES.md").write_text("Static fixture notices\n")
    (destination / "uv.lock").write_text("Static fixture lock\n")
    return "static contract fixture"


def cli_generator():
    module_spec = importlib.util.spec_from_file_location("cli_static_generator", ROOT / "templates/cli-json-v1/prepare.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_inline_generator_seals_source_identity_without_executing_source(tmp_path, native_helpers):
    source = tmp_path / "source"
    source.mkdir()
    marker = tmp_path / "executed"
    (source / "cli.py").write_text("from pathlib import Path; Path(" + repr(str(marker)) + ").touch()")
    (source / "LICENSE").write_text("Synthetic source license")
    draft = {**source_draft(source), "draft_id": "synthetic-cli-draft", "revision": 1, "requirements": {"profile": "mcp-stdio"}}
    export_path = tmp_path / "export.json"
    export_path.write_text(json.dumps(draft))
    package = cli_generator().prepare(export_path, tmp_path / "output", reaper=native_helpers / "mcpme-reap",
        launcher=native_helpers / "mcpme-cli-launch", package_id="test.cli-static", name="Static CLI fixture",
        runtime_builder=static_runtime, cli_spec=spec("cli.py").model_dump())
    assert package.manifest.source_revision == draft["source"]["revision"] and not marker.exists()
    assert package.manifest.lifecycle.max_sessions == 1 and package.manifest.access.writes is False
    assert not (package.root / "source/mcpme.cli.json").exists()
    assert (package.root / "cli.json").exists() and len(package.lock.artifacts) > 10


def test_generator_preserves_preexisting_output(tmp_path, native_helpers):
    source = tmp_path / "source"
    source.mkdir()
    (source / "cli.py").write_text("raise SystemExit(71)")
    (source / "LICENSE").write_text("Synthetic license")
    draft = {**source_draft(source), "draft_id": "synthetic-cli-draft", "revision": 1, "requirements": {}}
    export_path = tmp_path / "export.json"
    export_path.write_text(json.dumps(draft))
    output = tmp_path / "existing"
    output.mkdir()
    (output / "CLI_SUPPORT.md").write_text("existing support boundary")
    (output / "sentinel").write_text("preserve")
    with pytest.raises(PreparationError, match="destination_exists"):
        cli_generator().prepare(export_path, output, reaper=native_helpers / "mcpme-reap",
            launcher=native_helpers / "mcpme-cli-launch", package_id="test.cli-static", name="Existing fixture",
            runtime_builder=static_runtime, cli_spec=spec("cli.py").model_dump())
    assert (output / "sentinel").read_text() == "preserve"


def test_owner_sigkill_produces_durable_restart_receipt(tmp_path, command_package, native_helpers):
    script = command_package / "source/parent-death.py"
    script.write_text("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)")
    state = tmp_path / "state"
    helper = tmp_path / "owner.py"
    helper.write_text("\n".join(["import asyncio,sys", "from pathlib import Path",
        "sys.path.insert(0," + repr(str(ROOT / "backend")) + ")",
        "from mcpme.cli_execution import CliExecutor", "from mcpme.preparation_cli import parse_cli_spec",
        "async def main():", "    executor=CliExecutor(Path(" + repr(str(command_package)) + "),Path(" + repr(str(state)) +
        "),Path(" + repr(str(native_helpers / "mcpme-reap")) + "),Path(" + repr(str(native_helpers / "mcpme-cli-launch")) + "))",
        "    await executor.execute(parse_cli_spec(" + repr(spec("parent-death.py").model_dump()) + "),{'value':'fixture'})",
        "asyncio.run(main())"]) + "\n")
    parent = subprocess.Popen([sys.executable, str(helper)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not (state / "invocations.json").exists():
            assert time.monotonic() < deadline and parent.poll() is None
            time.sleep(0.01)
        journal = json.loads((state / "invocations.json").read_bytes())
        receipt = journal["entries"][0]["receipt"]
        time.sleep(0.3)
        parent.kill()
        parent.wait(timeout=5)
        while not verify_receipt(state, receipt):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        recovered = CliExecutor(command_package, state, native_helpers / "mcpme-reap", native_helpers / "mcpme-cli-launch")
        assert recovered.reconcile() == {"pending": 0, "reserved_memory_mb": 0}
        asyncio.run(recovered.close())
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
