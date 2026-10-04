"""Verify native form-to-install onboarding against a relocated private host.

This development-only harness explicitly enables trusted serve factories in an
isolated child. The production CLI remains disabled. Source fixtures contain no
preparation descriptors, dependency locks or separately supplied tool schemas.
All SDK imports and child tools come from the selected packaged host.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from verify_phase3_primitives import _check_module_origins, _host_imports, _host_paths

MAX_JSON_BYTES = 1024 * 1024
CASES = ("callable", "dynamic_mcp", "independent_recipe")
STAGES = frozenset({"bootstrap", "production_defaults", "source_intake", "recipe_authoring", "native",
    "attachment", "installation", "http_sdk", "stdio_sdk", "source_removal", "shutdown", "restart",
    "recipe_lifecycle", "cleanup", "complete"})
NATIVE_STAGES = frozenset({"connection", "initial_draft", "initial_form", "resolution_review", "resolution_start",
    "resolution_poll", "resolution_recovery", "generated_answers", "preparation_review", "preparation_start",
    "preparation_poll", "attached"})
JOB_STATES = frozenset({"queued", "snapshotting", "preparing", "verifying", "ready_to_attach", "attaching",
    "succeeded", "cancel_requested", "canceled", "failed", "interrupted"})
DIAGNOSTIC = {"stage": "bootstrap", "case": "none"}

CALLABLE_SOURCE = "def double(n: int) -> dict[str, int]:\n    return {'value': n * 2}\n"
MCP_PROJECT = ('[project]\nname="native-dynamic-fixture"\nversion="1.0.0"\n'
    'dependencies=["mcp==1.30.0"]\n[tool.mcpme]\nmcp_module="worker:main"\n')
MCP_SOURCE = (
    "import asyncio\nfrom mcp.server import Server\nfrom mcp.server.models import InitializationOptions\n"
    "from mcp.server.stdio import stdio_server\n"
    "from mcp.types import Tool, ServerCapabilities, ToolsCapability, CallToolResult, TextContent\n"
    "server = Server('Synthetic dynamic native acceptance')\n"
    "@server.list_tools()\nasync def listing():\n"
    "    return [Tool(name='dynamic_' + suffix, description='Double a synthetic integer', "
    "inputSchema={'type':'object','properties':{'n':{'type':'integer'}},'required':['n'],'additionalProperties':False}, "
    "outputSchema={'type':'object','properties':{'value':{'type':'integer'}},'required':['value'],'additionalProperties':False}, "
    "annotations={'readOnlyHint':True,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False}) "
    "for suffix in ('one', 'two')]\n"
    "@server.call_tool(validate_input=False)\nasync def call(name, arguments):\n"
    "    return CallToolResult(content=[TextContent(type='text', text='synthetic')], "
    "structuredContent={'value': arguments['n'] * 2})\n"
    "async def serve():\n    async with stdio_server() as (read, write):\n"
    "        await server.run(read, write, InitializationOptions(server_name='synthetic', server_version='1.0.0', "
    "capabilities=ServerCapabilities(tools=ToolsCapability())))\n"
    "def main():\n    asyncio.run(serve())\n"
)

# No payload, environment switch or ordinary CLI path selects these factories.
ENABLED_HOST = (
    "import asyncio,logging,sys\nfrom pathlib import Path\n"
    "sys.path[:0]=[sys.argv[1],sys.argv[2]]\nlogging.disable(logging.CRITICAL)\n"
    "from mcpme.supervisor import serve\nfrom mcpme.preparation_runner import PreparationRunner\n"
    "from mcpme.preparation_resolution_runner import OwnedResolutionRunner\n"
    "from mcpme.preparation_resolved_python import ResolvedPythonComponent\n"
    "def preparation(*args,**kwargs):\n kwargs['enabled']=True\n"
    " kwargs['enabled_families']=frozenset({ResolvedPythonComponent().reference.key})\n"
    " return PreparationRunner(*args,**kwargs)\n"
    "def resolution(*args,**kwargs):\n kwargs['enabled']=True\n return OwnedResolutionRunner(*args,**kwargs)\n"
    "asyncio.run(serve(Path(sys.argv[3]),Path(sys.argv[4]),int(sys.argv[5]),"
    "preparation_runner_factory=preparation,resolution_runner_factory=resolution))\n"
)


class AcceptanceError(ValueError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "acceptance_failed"
        self.context = dict(DIAGNOSTIC)
        super().__init__(self.code)


def bounded_failure(error):
    """Recover our fixed assertion from SDK task groups without exception text."""
    pending, types, checked, http_failure = [(error, 0)], set(), 0, None
    while pending and checked < 64:
        current, depth = pending.pop()
        checked += 1
        if isinstance(current, AcceptanceError):
            return current, []
        name = type(current).__name__
        if name == "HTTPStatusError":
            status = getattr(getattr(current, "response", None), "status_code", None)
            if type(status) is int and 100 <= status <= 599:
                method = getattr(getattr(current, "request", None), "method", "")
                suffix = "_" + method.lower() if method in {"GET", "POST", "DELETE"} else ""
                http_failure = AcceptanceError("sdk_http_status_" + str(status) + suffix)
                http_failure.context = getattr(error, "acceptance_context", dict(DIAGNOSTIC))
        if name in {"ConnectError", "ReadError", "WriteError", "ReadTimeout", "ConnectTimeout",
                "HTTPStatusError", "McpError", "ValidationError", "ExceptionGroup", "TimeoutError"}:
            types.add(name)
        if isinstance(current, BaseExceptionGroup) and depth < 8:
            pending.extend((item, depth + 1) for item in current.exceptions[:16])
    return http_failure or error, sorted(types)[:16]


def stage(value, case=None):
    DIAGNOSTIC["stage"] = value if value in STAGES else "bootstrap"
    if case is not None:
        DIAGNOSTIC["case"] = case if case in CASES else "none"


def require(condition, code):
    if not condition:
        raise AcceptanceError(code)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def private_json(path, limit=MAX_JSON_BYTES):
    details = path.lstat()
    require(stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
        and stat.S_IMODE(details.st_mode) == 0o600 and details.st_nlink == 1
        and details.st_size <= limit and path.resolve() == path, "unsafe_acceptance_json")
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), "invalid_acceptance_json")
    return value


def write_private_json(path, value):
    data = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    require(len(data) <= MAX_JSON_BYTES, "acceptance_report_limit")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def preparation_ownership_count(state, job_id=None):
    # Preparation receipts live in a separate private ledger, not the job JSON.
    path = state / "imports.sqlite"
    details = path.lstat()
    require(stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
        and stat.S_IMODE(details.st_mode) == 0o600 and path.resolve() == path,
        "unsafe_preparation_ledger")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        if job_id is None:
            return connection.execute("SELECT count(*) FROM preparation_ownership").fetchone()[0]
        return connection.execute("SELECT count(*) FROM preparation_ownership WHERE job_id=?", (job_id,)).fetchone()[0]
    finally:
        connection.close()


@contextmanager
def isolated_workspace(report):
    # An unproved shutdown must not erase ownership/recovery evidence.
    temporary = tempfile.TemporaryDirectory(prefix="mcpme-native-onboarding-", delete=False)
    root, lease = Path(temporary.name).resolve(), {"safe_to_remove": True}
    try:
        yield root, lease
    finally:
        if lease["safe_to_remove"]:
            temporary.cleanup()
        else:
            report["isolated_state_retained"] = True
            report["retained_state_directory"] = str(root)


class Harness:
    def __init__(self, root, host, native, lease):
        self.root, self.host, self.native = root, host, native
        self.lease = lease
        self.launcher, self.reaper = host / "bin/mcpme", host / "bin/mcpme-reap"
        self.children = set()
        self.states = []

    def spawn(self, argv, environment, cwd, stdout=subprocess.DEVNULL):
        child = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=subprocess.DEVNULL)
        self.children.add(child)
        return child

    async def stop(self, child):
        # Retain the Popen identity until wait; never signal a discovered PID.
        if child.poll() is None:
            child.terminate()
        try:
            await asyncio.to_thread(child.wait, 30)
        except subprocess.TimeoutExpired:
            if child.poll() is None:
                child.kill()
            await asyncio.to_thread(child.wait, 10)
        self.children.discard(child)

    async def run(self, argv, environment, cwd, *, timeout=330, capture=False):
        with tempfile.TemporaryFile() as output:
            child = self.spawn(argv, environment, cwd, stdout=output if capture else subprocess.DEVNULL)
            try:
                await asyncio.to_thread(child.wait, timeout)
                require(output.tell() <= MAX_JSON_BYTES, "acceptance_command_output_limit")
                output.seek(0)
                return child.returncode, output.read(MAX_JSON_BYTES + 1) if capture else b""
            except subprocess.TimeoutExpired:
                raise AcceptanceError("acceptance_child_timeout") from None
            finally:
                await self.stop(child)

    async def cli(self, state, environment, home, *arguments, expected_error=None):
        code, data = await self.run([str(self.launcher), "--state-dir", str(state), *map(str, arguments)],
            environment, home, capture=True)
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise AcceptanceError("acceptance_cli_invalid_json") from None
        require(isinstance(value, dict), "acceptance_cli_invalid_json")
        if expected_error is not None:
            error = value.get("error")
            require(code != 0 and isinstance(error, dict) and error.get("code") == expected_error,
                "production_cli_rejection_mismatch")
            return value
        if code or "error" in value:
            error = value.get("error")
            raise AcceptanceError(error.get("code") if isinstance(error, dict) else "acceptance_cli_failed")
        require(value.get("api_version") == 1, "acceptance_cli_version")
        return value

    async def manager(self, state, route, body):
        import httpx
        from mcpme_local_transport import private_transport, socket_path
        connection = private_json(state / "manager-connection.json")
        async with httpx.AsyncClient(base_url=connection["endpoint"], trust_env=False, timeout=30,
                follow_redirects=False, transport=private_transport(socket_path(state, "manager")),
                headers={"Authorization": "Bearer " + connection["token"]}) as client:
            response = await client.post(route, json=body)
            require(len(response.content) <= MAX_JSON_BYTES, "manager_response_limit")
            value = response.json()
            require(response.status_code == 200 and isinstance(value, dict) and "error" not in value,
                "manager_request_failed")
            return value

    def new_case(self, label):
        base = self.root / label
        base.mkdir(mode=0o700)
        state, home, temporary = base / "state", base / "home", base / "tmp"
        for path in (state, home, temporary):
            path.mkdir(mode=0o700)
        self.states.append(state)
        environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary),
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHOME": "/nonexistent", "PYTHONUSERBASE": "/nonexistent"}
        # The launcher must not import an ambient module from its working directory.
        (home / "mcpme.py").write_text("raise RuntimeError('ambient module imported')\n")
        return base, state, home, environment

    async def start(self, state, environment, home, *, enabled):
        if enabled:
            argv = [str(self.host / "python/bin/python3.12"), "-I", "-S", "-B", "-c", ENABLED_HOST,
                str(self.host / "backend"), str(self.host / "python/site-packages"), str(state),
                str(self.reaper), str(os.getpid())]
        else:
            argv = [str(self.launcher), "--state-dir", str(state), "serve", "--reaper", str(self.reaper),
                "--parent-pid", str(os.getpid())]
        child = self.spawn(argv, environment, home)
        try:
            async with asyncio.timeout(30):
                while child.poll() is None:
                    if (state / "manager-connection.json").is_file():
                        try:
                            await self.cli(state, environment, home, "capabilities", "--json")
                            return child
                        except (AcceptanceError, OSError):
                            pass
                    await asyncio.sleep(0.1)
            raise AcceptanceError("acceptance_host_exited")
        except BaseException:
            await self.shutdown(child, state)
            raise

    async def shutdown(self, child, state):
        stage("shutdown")
        try:
            await self.stop(child)
            require(not (state / "manager-connection.json").exists(), "manager_connection_retained")
            for path in (state / "slots").glob("*/ownership.json"):
                require(private_json(path) == {}, "installed_ownership_retained")
            for name in ("preparation-reservations", "resolution-reservations"):
                require(not list((state / name).glob("*.json")), "preparation_reservation_retained")
            if (state / "imports.sqlite").exists():
                require(preparation_ownership_count(state) == 0, "preparation_ownership_retained")
        except BaseException:
            self.lease["safe_to_remove"] = False
            raise

    async def finish_operation(self, state, environment, home, operation):
        async with asyncio.timeout(60):
            while operation["status"] in {"pending", "running"}:
                await asyncio.sleep(0.1)
                operation = await self.cli(state, environment, home, "operations", "get", operation["operation_id"], "--json")
        require(operation["status"] == "completed", "installation_operation_failed")
        return operation

    async def sdk(self, state, environment, home, slot_id, expected_tools, fixtures, transport):
        import httpx
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from mcpme_local_transport import private_transport, socket_path

        async def check(read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                require(initialized.protocolVersion == "2025-11-25"
                    and set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"}, "installed_protocol_mismatch")
                listed = await session.list_tools()
                require(listed.nextCursor is None and [tool.model_dump(by_alias=True, exclude_none=True)
                    for tool in listed.tools] == expected_tools, "installed_full_tool_contract_mismatch")
                for name, arguments, expected in fixtures:
                    result = await session.call_tool(name, arguments)
                    require(not result.isError and result.structuredContent == expected, "installed_fixture_mismatch")
                    from jsonschema import Draft202012Validator
                    tool = next(item for item in expected_tools if item["name"] == name)
                    Draft202012Validator(tool["inputSchema"]).validate(arguments)
                    if "outputSchema" in tool:
                        Draft202012Validator(tool["outputSchema"]).validate(result.structuredContent)
            return len(fixtures)

        stage(transport + "_sdk")
        async with asyncio.timeout(30):
            if transport == "http":
                connection = private_json(state / "slots" / slot_id / "connection.json")
                async with httpx.AsyncClient(trust_env=False, timeout=10,
                        transport=private_transport(socket_path(state, slot_id)),
                        headers={"Authorization": "Bearer " + connection["token"]}) as client:
                    async with streamable_http_client(connection["endpoint"], http_client=client) as (read, write, _):
                        return await check(read, write)
            parameters = StdioServerParameters(command=str(self.launcher),
                args=["--state-dir", str(state), "connect", "--slot", slot_id], env=environment, cwd=str(home))
            with open(os.devnull, "w") as quiet:
                async with stdio_client(parameters, errlog=quiet) as (read, write):
                    return await check(read, write)

    async def defaults(self):
        stage("production_defaults", "none")
        base, state, home, environment = self.new_case("production-defaults")
        child = await self.start(state, environment, home, enabled=False)
        try:
            caps = await self.cli(state, environment, home, "capabilities", "--json")
            require(caps["automatic_preparation"] is False and caps["recipe_execution"] is False
                and caps["preparation_resolution"]["execution"] is False, "production_execution_enabled")
            source = base / "source"
            source.mkdir(mode=0o700)
            (source / "worker.py").write_text(CALLABLE_SOURCE)
            (source / "LICENSE").write_text("Synthetic fixture permission notice.\n")
            draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                "--name", "Production defaults fixture", "--json")
            form = await self.manager(state, "/v1/preparations/form", {"import_id": draft["draft_id"], "revision": draft["revision"]})
            require(form["execution_available"] is False and form["resolution_available"] is False
                and form["resolution_required"] is True, "production_form_execution_enabled")
            policy = base / "host-reviewed-resolution-policy.json"
            write_private_json(policy, form["resolution_policy"])
            await self.cli(state, environment, home, "resolutions", "plan", "--import", draft["draft_id"],
                "--revision", draft["revision"], "--candidate", form["candidate_id"], "--policy", policy,
                "--json", expected_error="resolution_runner_unavailable")
            await self.cli(state, environment, home, "preparations", "start", "--plan", str(uuid.uuid4()),
                "--plan-digest", "a" * 64, "--idempotency-key", str(uuid.uuid4()), "--json",
                expected_error="preparation_runner_unavailable")
            require((await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"] == []
                and (await self.cli(state, environment, home, "resolutions", "list", "--json"))["resolutions"] == [],
                "production_job_created")
            return True
        finally:
            await self.shutdown(child, state)

    def source_fixture(self, source, label):
        (source / "worker.py").write_text(MCP_SOURCE if label == "dynamic_mcp" else CALLABLE_SOURCE)
        (source / "LICENSE").write_text("Synthetic fixture dedicated to the public domain under CC0.\n")
        if label == "dynamic_mcp":
            (source / "pyproject.toml").write_text(MCP_PROJECT)
        return {path.name: digest(path) for path in source.iterdir()}

    def source_check(self, source, original_files, *, remove=False):
        require(original_files == {path.name: digest(path) for path in source.iterdir()}, "original_source_mutated")
        if remove:
            shutil.rmtree(source)

    async def case(self, label):
        from mcpme.contracts import Tool, validate_package
        from mcpme.preparation_inputs import parse_preparation_receipt
        from mcpme.recipes import validate_bundle
        stage("source_intake", label)
        started = time.monotonic()
        base, state, home, environment = self.new_case(label)
        source = base / "ordinary source"
        source.mkdir(mode=0o700)
        original_files = self.source_fixture(source, label)
        require("uv.lock" not in original_files and "mcpme.prepare.json" not in original_files
            and (label == "dynamic_mcp" or "pyproject.toml" not in original_files), "fixture_prepared_in_advance")
        child = await self.start(state, environment, home, enabled=True)
        try:
            selected_recipe, bundle = None, None
            if label == "independent_recipe":
                stage("recipe_authoring")
                bundle = base / "independently authored recipe"
                scaffold = await self.cli(state, environment, home, "recipes", "scaffold", "--output", bundle,
                    "--id", "local.native-acceptance", "--component", "resolved-python-v1", "--json")
                checked = await self.cli(state, environment, home, "recipes", "check", "--path", bundle, "--json")
                require(scaffold["digest"] == checked["digest"] and checked["valid"] is True, "independent_recipe_check_failed")
                added = await self.cli(state, environment, home, "recipes", "add", "--path", bundle, "--json")
                selected_recipe = await self.cli(state, environment, home, "recipes", "enable", "--id", "local.native-acceptance",
                    "--version", "1.0.0", "--json")
                require(added["state"] == "proposed" and selected_recipe["state"] == "enabled"
                    and selected_recipe["digest"] == checked["digest"], "independent_recipe_admission_failed")
            stage("source_intake")
            draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                "--name", "Native acceptance " + label, "--intent", "Expose the selected synthetic integer tools.",
                "--start", "--start-policy", "host", "--json")
            observed = await self.cli(state, environment, home, "preparations", "inspect-source", "--import", draft["draft_id"],
                "--revision", draft["revision"], "--json")
            candidates = [item for item in observed["candidates"] if item["kind"] ==
                ("python-mcp" if label == "dynamic_mcp" else "python-callable")]
            require(len(candidates) == 1, "fixture_candidate_ambiguous")
            names = ["dynamic_one", "dynamic_two"] if label == "dynamic_mcp" else ["double"]
            answers = {}
            for name in names:
                suffix = " · " + name if label == "dynamic_mcp" else ""
                answers["Test input" + suffix] = {"n": 7}
                answers["Expected test result" + suffix] = {"value": 14}
            if selected_recipe:
                answers.update({"Tool label": "Independent native fixture", "Decimal places": 3})
            fixture, native_receipt = base / "native-case.json", base / "native-receipt.json"
            case = {"state_dir": str(state), "draft_id": draft["draft_id"], "output_path": str(native_receipt),
                "candidate_id": candidates[0]["candidate_id"], "answers_by_label": answers}
            if selected_recipe:
                case["recipe_id"] = selected_recipe["recipe_id"]
            write_private_json(fixture, case)
            stage("native")
            code, _ = await self.run([str(self.native), "--preparation-native-acceptance", str(fixture)], environment, home, timeout=1200)
            require(native_receipt.is_file(), "native_receipt_missing")
            receipt = private_json(native_receipt, 256 * 1024)
            if code or receipt.get("passed") is not True:
                failure = AcceptanceError(receipt.get("error_code", "native_acceptance_failed"))
                failure.native_stage = receipt.get("stage") if receipt.get("stage") in NATIVE_STAGES else "connection"
                try:
                    listing = await self.cli(state, environment, home, "preparations", "list", "--json")
                    jobs = [job for job in listing["preparations"] if job["import_id"] == draft["draft_id"]]
                    current = await self.cli(state, environment, home, "imports", "inspect", draft["draft_id"], "--json")
                    observation = {"preparation_jobs": len(jobs), "native_exit_code": code,
                        "receipt_sha256": digest(native_receipt), "current_revision": current["revision"],
                        "attachment_present": current.get("attachment") is not None,
                        "same_source_revision": current["source"]["revision"] == draft["source"]["revision"],
                        "resolution_completed": "resolution_digest" in receipt,
                        "reviewed_revision": receipt.get("reviewed_revision")}
                    if len(jobs) == 1:
                        job = jobs[0]
                        observation["preparation_state"] = job["state"] if job["state"] in JOB_STATES else "unknown"
                        error_code = job.get("error_code")
                        if isinstance(error_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
                            observation["preparation_error_code"] = error_code
                        progress = job.get("progress") or {}
                        observation["fixtures_checked"] = progress.get("fixtures_checked", 0)
                    failure.native_observation = observation
                except Exception:
                    pass
                raise failure
            require(receipt["stage"] == "attached" and receipt["draft_id"] == draft["draft_id"]
                and receipt["candidate_id"] == candidates[0]["candidate_id"]
                and receipt["source_revision"] == draft["source"]["revision"], "native_binding_mismatch")
            required_routes = {"POST v1/preparations/form", "POST v1/resolutions/plan", "POST v1/resolutions/start",
                "POST v1/preparations/plan-form", "POST v1/preparations/start"}
            require(required_routes <= set(receipt["routes"]), "native_routes_missing")
            stage("attachment")
            resolved = await self.cli(state, environment, home, "resolutions", "get", receipt["resolution_job_id"], "--json")
            prepared = await self.cli(state, environment, home, "preparations", "get", receipt["preparation_job_id"], "--json")
            for job in (resolved, prepared):
                require(job["state"] == "succeeded" and job["ownership_settled"] is True
                    and job["reservation_retained"] is False, "native_job_unsettled")
            require(not resolved["ownership"] and preparation_ownership_count(state, prepared["job_id"]) == 0,
                "native_job_ownership_retained")
            require(resolved["result_digest"] == receipt["resolution_digest"]
                and resolved["progress"] == receipt["resolution_progress"]
                and {key: value for key, value in prepared["progress"].items() if key != "stage"}
                    == receipt["preparation_progress"]
                and prepared["binding"]["revision"] == receipt["reviewed_revision"], "native_job_receipt_mismatch")
            attached = await self.cli(state, environment, home, "imports", "inspect", draft["draft_id"], "--json")
            require(attached["revision"] == receipt["attached_revision"] == receipt["reviewed_revision"] > draft["revision"]
                and attached["source"]["revision"] == draft["source"]["revision"] and attached["state"] == "ready_to_plan",
                "native_attachment_revision_mismatch")
            require(attached["start"] is True and attached["start_policy"] == "host", "native_start_choice_changed")
            package_root = Path(attached["attachment"]["path"])
            package = validate_package(package_root)
            require(package.digest == attached["attachment"]["digest"] == prepared["expected_package_digest"]
                == receipt["attached_package_digest"], "native_attachment_identity_mismatch")
            proof = parse_preparation_receipt((package_root / "mcpme.preparation.json").read_bytes())
            require(proof.schema_version == 2 and proof.binding.revision == receipt["reviewed_revision"]
                and proof.binding.source_revision == draft["source"]["revision"]
                and not proof.inputs.original_dependency_locks and proof.inputs.derived_inputs
                and not (package_root / "source/uv.lock").exists(), "derived_source_identity_mismatch")
            self.source_check(source, original_files)
            require(prepared["verification"]["tools_checked"] == len(names)
                and prepared["verification"]["fixtures_checked"] == len(names)
                and prepared["verification"]["package_digest"] == package.digest, "independent_probe_count_mismatch")
            if selected_recipe:
                require(receipt["recipe_id"] == selected_recipe["recipe_id"] and receipt["recipe_digest"] == selected_recipe["digest"],
                    "independent_native_recipe_not_selected")
            stage("installation")
            plan = await self.cli(state, environment, home, "install", "plan", "--import", draft["draft_id"],
                "--revision", attached["revision"], "--json")
            require(plan["start"] is True and plan["start_policy"] == "host", "installation_start_choice_changed")
            expected_tools = [Tool.model_validate(item).mcp_schema() for item in plan["package"]["tools"]]
            require(expected_tools == [item.mcp_schema() for item in package.manifest.tools]
                and [item["name"] for item in expected_tools] == names, "installation_contract_changed")
            operation = await self.finish_operation(state, environment, home,
                await self.cli(state, environment, home, "install", "apply", "--plan", plan["plan_id"], "--json"))
            slot_id = operation["slot_id"]
            installed = await self.cli(state, environment, home, "imports", "inspect", draft["draft_id"], "--json")
            require(installed["state"] == "installed" and installed["result_slot_id"] == slot_id, "installation_binding_failed")
            calls = [(name, {"n": 7}, {"value": 14}) for name in names]
            http_calls = await self.sdk(state, environment, home, slot_id, expected_tools, calls, "http")
            stdio_calls = await self.sdk(state, environment, home, slot_id, expected_tools, calls, "stdio")
            stage("source_removal")
            self.source_check(source, original_files, remove=True)
            if bundle:
                shutil.rmtree(bundle)
            await self.shutdown(child, state)
            stage("restart")
            child = await self.start(state, environment, home, enabled=True)
            resumed = await self.cli(state, environment, home, "imports", "inspect", draft["draft_id"], "--json")
            require(resumed["state"] == "installed" and resumed["result_slot_id"] == slot_id
                and not source.exists(), "installed_restart_binding_failed")
            calls = [(name, {"n": -4}, {"value": -8}) for name in names]
            http_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "http")
            stdio_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "stdio")
            recipe_lifecycle = None
            if selected_recipe:
                stage("recipe_lifecycle")
                retained = await self.cli(state, environment, home, "recipes", "inspect", "--id", selected_recipe["recipe_id"],
                    "--version", "1.0.0", "--json")
                require(retained["state"] == "enabled" and retained["digest"] == selected_recipe["digest"]
                    and retained["epoch"] == selected_recipe["epoch"], "recipe_enabled_restart_failed")
                disabled = await self.cli(state, environment, home, "recipes", "disable", "--id", selected_recipe["recipe_id"],
                    "--version", "1.0.0", "--json")
                await self.shutdown(child, state)
                stage("restart")
                child = await self.start(state, environment, home, enabled=True)
                retained = await self.cli(state, environment, home, "recipes", "inspect", "--id", selected_recipe["recipe_id"],
                    "--version", "1.0.0", "--json")
                require(retained["state"] == "disabled" and retained["digest"] == disabled["digest"]
                    and retained["epoch"] == disabled["epoch"], "recipe_disabled_restart_failed")
                http_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "http")
                stdio_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "stdio")
                stage("recipe_lifecycle")
                accepted_receipt_sha256 = digest(package_root / "mcpme.preparation.json")
                retained_recipe = {key: retained[key] for key in ("digest", "path", "manifest", "evidence")}
                removed = await self.cli(state, environment, home, "recipes", "remove", "--id", selected_recipe["recipe_id"],
                    "--version", "1.0.0", "--json")
                require(removed["state"] == "removed" and removed["digest"] == selected_recipe["digest"]
                    and all(removed[key] == value for key, value in retained_recipe.items())
                    and validate_bundle(Path(removed["path"]))["digest"] == selected_recipe["digest"],
                    "recipe_remove_identity_failed")
                await self.shutdown(child, state)
                stage("restart")
                child = await self.start(state, environment, home, enabled=True)
                retained = await self.cli(state, environment, home, "recipes", "inspect", "--id", selected_recipe["recipe_id"],
                    "--version", "1.0.0", "--json")
                require(retained["state"] == "removed" and retained["digest"] == removed["digest"]
                    and retained["epoch"] == removed["epoch"]
                    and all(retained[key] == value for key, value in retained_recipe.items())
                    and validate_bundle(Path(retained["path"]))["digest"] == selected_recipe["digest"],
                    "recipe_removed_restart_failed")
                resumed = await self.cli(state, environment, home, "imports", "inspect", draft["draft_id"], "--json")
                require(resumed["state"] == "installed" and resumed["result_slot_id"] == slot_id
                    and resumed["attachment"]["digest"] == package.digest
                    and digest(package_root / "mcpme.preparation.json") == accepted_receipt_sha256
                    and validate_package(package_root).digest == package.digest,
                    "recipe_remove_changed_accepted_package")
                http_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "http")
                stdio_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "stdio")
                recipe_lifecycle = {"digest": retained["digest"], "enabled_restart": True, "disabled_restart": True,
                    "removed_restart": True, "installed_runtime_after_disable": True,
                    "installed_runtime_after_remove": True, "accepted_receipt_retained": True,
                    "accepted_package_retained": True, "registered_bundle_retained": True,
                    "independent_evidence_retained": True}
            return {"passed": True, "source_files": len(original_files), "source_locks": 0,
                "native_receipt_sha256": digest(native_receipt), "native_routes": sorted(set(receipt["routes"])),
                "source_revision": receipt["source_revision"], "reviewed_revision": receipt["reviewed_revision"],
                "attached_revision": receipt["attached_revision"], "recipe_id": receipt["recipe_id"],
                "recipe_digest": receipt["recipe_digest"], "resolution_digest": receipt["resolution_digest"],
                "resolution_progress": resolved["progress"], "resolution_peak_memory_bytes": resolved["result"]["peak_memory_bytes"],
                "resolution_budget": {key: resolved["request"]["policy"][key] for key in ("wall_seconds", "disk_bytes", "memory_mb")},
                "preparation_progress": prepared["progress"], "probe_peak_memory_bytes": prepared["verification"]["peak_memory_bytes"],
                "declared_app_memory_mb": package.manifest.resources.memory_mb, "package_digest": package.digest,
                "full_tool_contract_sha256": hashlib.sha256(json.dumps(expected_tools, sort_keys=True).encode()).hexdigest(),
                "tools_checked": len(names), "http_calls": http_calls, "stdio_calls": stdio_calls,
                "source_removed": True, "restart_invoked": True, "recipe_lifecycle": recipe_lifecycle,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
        except BaseException as error:
            if not isinstance(error, AcceptanceError):
                error.acceptance_context = dict(DIAGNOSTIC)
            raise
        finally:
            await self.shutdown(child, state)

    async def cleanup(self):
        stage("cleanup")
        failed = False
        for child in tuple(self.children):
            try:
                await self.stop(child)
            except BaseException:
                self.lease["safe_to_remove"] = False
                failed = True
        require(not failed, "acceptance_cleanup_unsettled")
        if not self.lease["safe_to_remove"]:
            return
        from mcpme_local_transport import socket_path
        for state in self.states:
            try:
                directory = socket_path(state, "manager").parent
            except FileNotFoundError:
                continue
            known = {"manager.sock", *(path.name + ".sock" for path in (state / "slots").glob("*"))}
            for path in directory.iterdir():
                details = path.lstat()
                require(path.name in known and stat.S_ISSOCK(details.st_mode) and details.st_uid == os.getuid(),
                    "unexpected_acceptance_socket")
                path.unlink()
            directory.rmdir()


def verify_host_payload(host):
    build = json.loads((host / "build.json").read_bytes())
    require(isinstance(build.get("source_commit"), str)
        and re.fullmatch(r"[0-9a-f]{40}", build["source_commit"]), "relocated_host_source_commit_invalid")
    require(build.get("source_tree_dirty") is False, "relocated_host_dirty_source")
    artifacts = build["artifact_sha256"]
    require(isinstance(artifacts, dict) and 1 <= len(artifacts) <= 20000
        and all(isinstance(path, str) and len(path) <= 4096 and not Path(path).is_absolute()
            and ".." not in Path(path).parts and (host / path).resolve() == host / path
            and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for path, value in artifacts.items()), "relocated_host_manifest_invalid")
    members = set()
    for path in host.rglob("*"):
        details = path.lstat()
        require(stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode), "relocated_host_unsafe_member")
        if stat.S_ISREG(details.st_mode):
            members.add(path.relative_to(host).as_posix())
            require(len(members) <= 20001, "relocated_host_member_limit")
    require(members == set(artifacts) | {"build.json"}, "relocated_host_membership_mismatch")
    require(all(digest(host / path) == value for path, value in artifacts.items()), "relocated_host_artifact_mismatch")
    return build


async def verify(host_path, native_path, report, *, harness_factory=Harness, cases=CASES):
    _host_paths(host_path)
    verify_host_payload(host_path)
    require(native_path.is_absolute() and native_path.resolve() == native_path and native_path.is_file()
        and os.access(native_path, os.X_OK), "native_acceptance_binary_required")
    with isolated_workspace(report) as (root, lease):
        host, native = root / "relocated host", root / "McpmeMenuBar"
        shutil.copytree(host_path, host, symlinks=True)
        # Keep the compiled bytes without inheriting source-volume file flags.
        shutil.copyfile(native_path, native)
        native.chmod(0o700)
        _host_paths(host)
        build = verify_host_payload(host)
        artifacts = build["artifact_sha256"]
        _host_imports(host)
        report.update({"host_source_commit": build["source_commit"],
            "host_source_tree_dirty": False,
            "host_build_sha256": digest(host / "build.json"), "host_artifacts_checked": len(artifacts),
            "native_binary_sha256": digest(native), "relocated_host": True, "minimal_environment": True,
            "private_sdk": True, "development_execution_only": True})
        harness = harness_factory(root, host, native, lease)
        try:
            report["production_defaults_disabled"] = await harness.defaults()
            for case in cases:
                report["cases"][case] = await harness.case(case)
            _check_module_origins(host)
        except BaseException as error:
            if not isinstance(error, AcceptanceError) and not hasattr(error, "acceptance_context"):
                error.acceptance_context = dict(DIAGNOSTIC)
            raise
        finally:
            try:
                await harness.cleanup()
            except BaseException:
                lease["safe_to_remove"] = False
                raise
            finally:
                report["owned_children_joined"] = not harness.children
        report["verified"] = report["owned_children_joined"] and all(item["passed"] for item in report["cases"].values())
        stage("complete", "none")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "native_installed_python_onboarding",
        "cases": {}, "limits": ["crash_revision_cancel_races_unverified", "probe_filesystem_network_isolation_unverified",
            "hard_aggregate_live_memory_unverified", "other_phase3_families_unverified", "public_execution_disabled"]}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True), report))
    except (Exception, KeyboardInterrupt) as error:
        # Exception text and raw subprocess output may contain caller content.
        error, nested_types = bounded_failure(error)
        report["diagnostic"] = {**(error.context if isinstance(error, AcceptanceError)
                else getattr(error, "acceptance_context", DIAGNOSTIC)),
            "code": error.code if isinstance(error, AcceptanceError) else "acceptance_failed",
            "type": type(error).__name__ if type(error).__name__ in {"AcceptanceError", "AssertionError", "ValueError",
                "KeyError", "TypeError", "OSError", "FileNotFoundError", "PermissionError", "NotADirectoryError",
                "TimeoutError", "KeyboardInterrupt", "ExceptionGroup"}
            else "Exception"}
        if nested_types:
            report["diagnostic"]["nested_types"] = nested_types
        if isinstance(error, AcceptanceError) and hasattr(error, "native_stage"):
            report["diagnostic"]["native_stage"] = error.native_stage
        if isinstance(error, AcceptanceError) and hasattr(error, "native_observation"):
            report["native_failure"] = error.native_observation
    if args.output:
        try:
            write_private_json(args.output.absolute(), report)
        except Exception:
            report["verified"] = False
            report["report_write_error"] = "acceptance_report_write_failed"
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
