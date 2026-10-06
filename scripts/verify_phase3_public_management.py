"""Bounded acceptance of shipped, signed public management MCP preparation.

This driver cannot enable execution. It requires an already admitted Python
recipe and uses the shipped CLI for both manager startup and owner grant mint.
Captured protocol bytes remain in RAM; the receipt contains metadata only.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
from contextlib import asynccontextmanager
from datetime import timedelta
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
import sys
import uuid

import verify_phase3_native_onboarding as common
import verify_phase3_public_native_command as signed
import verify_phase3_live_agent as live

require = common.require
PYTHON = ("python-function-v1", "1.0.0", "b28f98582aeff09e344b4a7049af7fdadbaf19436dc6724638ab64b6a9139d4e")
RESOLVED = ("resolved-python-v1", "1.0.1", "3c24410b46415cb83b9d945ebf0049f552e9f7493943164f311939a79b49000e")
POLICY_PATH = "templates/release-policies/phase3-v1.json"
REVIEWED_AGENT_MODEL = "gpt-6.1-sol"
REVIEWED_ADAPTER_DIGEST = "f651b36ab4b3d4fe6479457e5a31f129b1339c41536e02bfb53cbbf4025cebc1"
TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}
SDK_READ_SECONDS = 30
SDK_STEP_SECONDS = 20
SDK_SESSION_SECONDS = 900


class _BsdInfo(ctypes.Structure):
    # Exact Darwin macOS 26 SDK proc_bsdinfo layout (sys/proc_info.h).
    _fields_ = [(name, ctypes.c_uint32) for name in ("flags", "status", "xstatus", "pid", "ppid", "uid",
        "gid", "ruid", "rgid", "svuid", "svgid", "reserved")] + [("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32)] + [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")] \
        + [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


class _ThreadInfo(ctypes.Structure):
    _fields_ = [("user_time", ctypes.c_uint64), ("system_time", ctypes.c_uint64)] \
        + [(name, ctypes.c_int32) for name in ("cpu_usage", "policy", "run_state", "flags", "sleep_time",
            "curpri", "priority", "maxpriority")] + [("name", ctypes.c_char * 64)]


class DarwinProcessReader:
    """Bounded read-only observation; discovered PIDs never become signal handles."""
    def __init__(self):
        require(sys.platform == "darwin" and os.uname().machine == "arm64" and os.uname().release.startswith("25.")
            and ctypes.sizeof(_BsdInfo) == 136 and ctypes.sizeof(_ThreadInfo) == 112, "management_probe_observation_unavailable")
        self.api = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.api.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.api.proc_pidinfo.restype = ctypes.c_int
        self.api.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.api.proc_pidpath.restype = ctypes.c_int
        self.api.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        self.api.proc_listpids.restype = ctypes.c_int

    def identity(self, pid):
        require(type(pid) is int and pid > 1, "management_probe_observation_unavailable")
        data = _BsdInfo(); ctypes.set_errno(0)
        result = self.api.proc_pidinfo(pid, 3, 0, ctypes.byref(data), ctypes.sizeof(data))
        if result == 0 and ctypes.get_errno() == errno.ESRCH:
            return None
        require(result == ctypes.sizeof(data) and data.pid == pid and data.uid == os.getuid()
            and data.start_sec > 0, "management_probe_observation_unavailable")
        return (data.pid, data.ppid, data.uid, data.start_sec, data.start_usec)

    def path(self, pid):
        data = ctypes.create_string_buffer(4096)
        result = self.api.proc_pidpath(pid, data, len(data))
        require(0 < result < len(data), "management_probe_observation_unavailable")
        return os.fsdecode(data.value)

    def children(self, pid):
        data = (ctypes.c_int * 128)()
        result = self.api.proc_listpids(6, pid, data, ctypes.sizeof(data))  # PROC_PPID_ONLY; byte count.
        require(0 <= result < ctypes.sizeof(data) and result % ctypes.sizeof(ctypes.c_int) == 0,
            "management_probe_observation_unavailable")
        values = list(data[:result // ctypes.sizeof(ctypes.c_int)])
        require(all(value > 1 for value in values) and len(values) == len(set(values)), "management_probe_observation_unavailable")
        return values

    def tagged(self, pid, marker):
        data = (ctypes.c_uint64 * 256)()
        result = self.api.proc_pidinfo(pid, 6, 0, data, ctypes.sizeof(data))  # PROC_PIDLISTTHREADS.
        require(0 < result < ctypes.sizeof(data) and result % 8 == 0, "management_probe_observation_unavailable")
        for identifier in data[:result // 8]:
            thread = _ThreadInfo()
            result = self.api.proc_pidinfo(pid, 5, identifier, ctypes.byref(thread), ctypes.sizeof(thread))
            require(result == ctypes.sizeof(thread), "management_probe_observation_unavailable")
            if thread.name == marker.encode("ascii"):
                return True
        return False


def probe_entry(reader, manager, manager_identity, reaper, interpreter, marker):
    require(re.fullmatch(r"mcpme-probe-[0-9a-f]{32}", marker), "management_probe_marker_invalid")
    require(manager.poll() is None and reader.identity(manager.pid) == manager_identity,
        "management_original_manager_changed")
    for wrapper_pid in reader.children(manager.pid):
        wrapper = reader.identity(wrapper_pid)
        if wrapper is None or wrapper[1] != manager.pid or reader.path(wrapper_pid) != str(reaper):
            continue
        for pid in reader.children(wrapper_pid):
            before = reader.identity(pid)
            if before is None or before[1] != wrapper_pid or reader.path(pid) != str(interpreter):
                continue
            if not reader.tagged(pid, marker):
                continue
            require(reader.identity(pid) == before and reader.identity(wrapper_pid) == wrapper
                and reader.path(pid) == str(interpreter) and reader.path(wrapper_pid) == str(reaper)
                and reader.identity(manager.pid) == manager_identity and manager.poll() is None,
                "management_probe_identity_changed")
            return {"probe": before, "reaper": wrapper}
    return None


def agent_policy_scope(value):
    """Recognize only the historical closure or the separately accepted tuple."""
    closed = {"components": [], "providers": [], "unlocked_resolution": False}
    accepted = {"components": [live.UNLOCKED], "providers": [{"adapter_id": "openai-responses-v1",
        "adapter_digest": REVIEWED_ADAPTER_DIGEST, "models": [REVIEWED_AGENT_MODEL]}],
        "unlocked_resolution": True}
    locked_accepted = {**accepted, "components": [live.UNLOCKED, live.LOCKED]}
    require(type(value) is dict and type(value.get("unlocked_resolution")) is bool
        and value in (closed, accepted, locked_accepted), "management_agent_not_reviewed")
    return "closed" if value == closed else "selected_model_accepted"


def typed_policy_scope(data):
    """Validate using the selected host modules after host-origin binding."""
    from mcpme.preparation_release_policy import ReleasePolicy
    from mcpme.preparation_contracts import load_contract
    from mcpme.preparation_agent_providers import OpenAIResponsesAdapter
    try:
        value = load_contract(data)
        ReleasePolicy.model_validate(value).admissions()
        scope = agent_policy_scope(value["agent"])
        if scope != "closed":
            adapter = OpenAIResponsesAdapter()
            require(adapter.digest == REVIEWED_ADAPTER_DIGEST, "management_adapter_not_reviewed")
            live.active_candidate(value, REVIEWED_AGENT_MODEL, adapter.digest)
        return scope
    except (ValueError, TypeError, KeyError) as error:
        if isinstance(error, common.AcceptanceError):
            raise
        raise common.AcceptanceError("management_policy_invalid") from None


def active_policy(host):
    data, value = signed._json(host / POLICY_PATH)
    component = dict(zip(("id", "version", "digest"), PYTHON))
    resolved = dict(zip(("id", "version", "digest"), RESOLVED))
    require(type(value.get("schema_version")) is int and value["schema_version"] == 1
        and set(value) == {"schema_version", "policy_id", "preparation", "command", "agent"},
        "management_policy_invalid")
    preparation = value.get("preparation")
    require(type(preparation) is dict
        and set(preparation) == {"families", "recipe_component_sets", "resolution_candidate_kinds"}
        and type(preparation["families"]) is list and resolved in preparation["families"]
        and type(preparation["recipe_component_sets"]) is list
        and [component] in preparation["recipe_component_sets"], "management_python_not_admitted")
    scope = agent_policy_scope(value["agent"])
    if scope != "closed":
        require(live.UNLOCKED in preparation["families"]
            and [live.UNLOCKED] in preparation["recipe_component_sets"], "management_agent_not_admitted")
    return data


def public_capabilities(value):
    execution = value.get("preparation_execution", {})
    require(value.get("automatic_preparation") is True and value.get("recipe_execution") is True
        and value.get("management_mcp") == "scoped_stdio"
        and list(RESOLVED) in execution.get("families", [])
        and [list(PYTHON)] in execution.get("recipe_component_sets", [])
        and isinstance(execution.get("admission_digest"), str)
        and re.fullmatch(r"[0-9a-f]{64}", execution["admission_digest"]), "management_public_admission_unavailable")
    return execution["admission_digest"]


def answers(form):
    result = {}
    for field in form["fields"]:
        if field["kind"] == "schema-preview":
            continue
        if field["label"] == "Test input":
            result[field["id"]] = {"n": 7}
        elif field["label"] == "Expected test result":
            result[field["id"]] = {"value": 14}
        elif field.get("value") is not None:
            result[field["id"]] = field["value"]
        else:
            require(not field["required"], "management_form_field_changed")
    return result


def validate_export(value, launcher, state, credential):
    expected = {"command": str(launcher), "args": ["--state-dir", str(state), "--credential-file",
        str(credential), "management", "connect"]}
    require(value.get("configuration") == {"mcpServers": {"mcpme-management": expected}},
        "management_export_binding_changed")
    return expected


def source_snapshot(root):
    def git(*args):
        result = subprocess.run(["/usr/bin/git", "-C", str(root), *args], capture_output=True, timeout=30)
        require(result.returncode == 0, "management_source_identity_unavailable")
        return result.stdout
    require(git("status", "--porcelain", "--untracked-files=all") == b"", "management_source_dirty")
    commit = git("rev-parse", "HEAD").decode().strip()
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "management_source_identity_unavailable")
    files = {}
    for member in git("ls-files", "-z").decode().split("\0"):
        if not member:
            continue
        path = root / member
        detail = path.lstat()
        require(stat.S_ISREG(detail.st_mode) and path.resolve() == path and detail.st_nlink == 1,
            "management_source_member_invalid")
        files[member] = (stat.S_IMODE(detail.st_mode), common.digest(path))
    require(1 <= len(files) <= 20000, "management_source_inventory_invalid")
    return commit, files


def binding_snapshot(state, grant, job):
    """Read original persisted metadata; never mutate a job, grant, or authority."""
    from mcpme.management_workflow_grants import job_identity
    from mcpme.preparation_contracts import canonical_bytes, contract_digest
    path = state / "imports.sqlite"
    detail = path.lstat()
    require(stat.S_ISREG(detail.st_mode) and detail.st_uid == os.getuid() and detail.st_nlink == 1
        and stat.S_IMODE(detail.st_mode) == 0o600 and path.resolve() == path, "management_database_invalid")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        row = database.execute("SELECT scope_digest,scope,job_id,revoked FROM workflow_management_grants WHERE grant_id=?",
            (grant["grant_id"],)).fetchone()
        association = database.execute("SELECT grant_id,scope_digest,binding_digest FROM workflow_management_jobs WHERE kind='preparation' AND job_id=?",
            (job["job_id"],)).fetchone()
        marker = database.execute("SELECT authority FROM workflow_management_job_kinds WHERE kind='preparation' AND job_id=?",
            (job["job_id"],)).fetchone()
        original = database.execute("SELECT data FROM preparation_execution_plans WHERE plan_id=?",
            (grant["plan_id"],)).fetchone()
    require(row is not None and association is not None and marker is not None and original is not None,
        "management_persisted_binding_missing")
    scope, plan = json.loads(row[1]), json.loads(original[0])
    require(contract_digest("management-workflow-scope-v1", scope) == row[0]
        and scope["kind"] == "preparation" and scope["mode"] == "preparation-execution"
        and scope["grant_id"] == grant["grant_id"] and scope["plan"] == {key: item for key, item in plan.items() if key != "job_id"}
        and row[0] == grant["scope_digest"] and row[2] == plan["job_id"] == job["job_id"]
        and scope["idempotency_key"] == grant["idempotency_key"]
        and association == (grant["grant_id"], grant["scope_digest"], job_identity("preparation", job))
        and marker[0] == canonical_bytes(job["management_authority"]).decode(), "management_persisted_binding_changed")
    require(job.get("schema_version") == 2 and job["management_authority"] == {"schema_version": 1,
        "kind": "execution_grant", "grant_id": grant["grant_id"], "scope_digest": grant["scope_digest"]}
        and job["authority_plan_id"] == grant["plan_id"] and job["authority_plan_digest"] == grant["plan_digest"]
        and all(job["binding"][key] == plan[key] for key in ("import_id", "revision", "source_revision",
            "descriptor_digest", "policy_digest", "runner_digest", "verifier_digest"))
        and job["binding"]["recipe"] == plan["descriptor"]["recipe"]
        and job["runtime_digest"] == plan["runtime_digest"], "management_job_binding_changed")
    return {"scope_digest": row[0], "plan_digest": grant["plan_digest"],
        "association_digest": hashlib.sha256(canonical_bytes(list(association))).hexdigest(),
        "authority_kind_digest": hashlib.sha256(marker[0].encode()).hexdigest(),
        "original_plan_sha256": hashlib.sha256(canonical_bytes(scope["plan"])).hexdigest()}


class ManagementHarness(common.Harness):
    def __init__(self, *args, report):
        super().__init__(*args)
        self.report = report

    async def start(self, state, environment, home, *, enabled=False):
        self.report["public_cli_starts"] += 1
        return await common.Harness.start(self, state, environment, home, enabled=False)

    async def run(self, argv, environment, cwd, *, timeout=330, capture=False):
        """Own the original process and bound nonblocking capture entirely in RAM."""
        child = self.spawn(argv, environment, cwd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL)
        data, eof = bytearray(), not capture
        if capture:
            os.set_blocking(child.stdout.fileno(), False)
        try:
            async with asyncio.timeout(timeout):
                while child.poll() is None or not eof:
                    if not eof:
                        try:
                            block = os.read(child.stdout.fileno(), 65536)
                        except BlockingIOError:
                            block = None
                        if block == b"":
                            eof = True
                        elif block is not None:
                            require(len(data) + len(block) <= common.MAX_JSON_BYTES, "acceptance_command_output_limit")
                            data.extend(block)
                    await asyncio.sleep(.01)
            return child.returncode, bytes(data) if capture else b""
        except TimeoutError:
            raise common.AcceptanceError("acceptance_child_timeout") from None
        finally:
            await self.stop(child)
            if capture:
                child.stdout.close()

    @asynccontextmanager
    async def sdk(self, state, environment, home, credential):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        exported = await self.cli(state, environment, home, "--credential-file", credential, "management", "export", "--json")
        # The production bundled CLI exports its enclosing app's native entry.
        # verify() has already bound this exact native/host pair and full app.
        configuration = validate_export(exported, self.native, state, credential)
        timed_out = False
        lifetime = asyncio.timeout(SDK_SESSION_SECONDS)
        try:
            async with lifetime:
                with open(os.devnull, "w") as errors:
                    async with stdio_client(StdioServerParameters(**configuration, env=environment, cwd=str(home)), errlog=errors) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=SDK_READ_SECONDS)) as session:
                            try:
                                async with asyncio.timeout(SDK_STEP_SECONDS):
                                    initialized = await session.initialize()
                            except TimeoutError:
                                timed_out = True
                                raise common.AcceptanceError("management_sdk_timeout") from None
                            require(initialized.protocolVersion == "2025-11-25"
                                and set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"}, "management_sdk_contract_changed")
                            try:
                                yield session
                            except common.AcceptanceError as error:
                                timed_out = error.code == "management_sdk_timeout"
                                raise
                    self.report["sdk_sessions_exited"] += 1
        except BaseException:
            # AnyIO teardown can mask a body deadline with BrokenResourceError.
            # Preserve only an explicitly fired timer, never classify a generic
            # transport failure as a timeout.
            if timed_out or lifetime.expired():
                raise common.AcceptanceError("management_sdk_timeout") from None
            raise

    async def discovery(self, session):
        try:
            async with asyncio.timeout(SDK_STEP_SECONDS):
                return await session.list_tools()
        except TimeoutError:
            raise common.AcceptanceError("management_sdk_timeout") from None

    async def invoke(self, session, name, arguments=None, *, error=None):
        try:
            async with asyncio.timeout(SDK_STEP_SECONDS):
                result = await session.call_tool(name, arguments or {})
        except TimeoutError:
            raise common.AcceptanceError("management_sdk_timeout") from None
        value = result.structuredContent
        require(type(value) is dict, "management_result_invalid")
        if error is not None:
            require(result.isError and value.get("error", {}).get("code") == error, "management_denial_changed")
        else:
            if result.isError or "error" in value:
                from mcpme.management import SAFE_ERROR_CODES
                code = value.get("error", {}).get("code")
                raise common.AcceptanceError(code if isinstance(code, str) and code in SAFE_ERROR_CODES else "management_call_failed")
        return value

    async def review(self, session, source):
        listed = await self.discovery(session)
        names = {item.name for item in listed.tools}
        require(len(names) == len(listed.tools) == 40 and not any("authorize" in name or "secret" in name for name in names)
            and all(item.inputSchema.get("additionalProperties") is False for item in listed.tools), "management_tools_changed")
        public_capabilities(await self.invoke(session, "mcpme_capabilities"))
        await self.invoke(session, "mcpme_get_recipe_schema")
        await self.invoke(session, "mcpme_list_recipes")
        draft = await self.invoke(session, "mcpme_create_import", {"source": str(source), "name": "Synthetic management fixture"})
        args = {"import_id": draft["draft_id"], "revision": draft["revision"]}
        await self.invoke(session, "mcpme_inspect_preparation_source", args)
        form = await self.invoke(session, "mcpme_get_preparation_form", args)
        require(form["execution_available"] is True and form["resolution_required"] is False
            and form["recipe"]["id"] == "mcpme.python-callable" and form["recipe"]["version"] == "1.0.0",
            "management_form_not_admitted")
        recipe = await self.invoke(session, "mcpme_get_recipe", {"recipe_id": form["recipe"]["id"], "version": form["recipe"]["version"]})
        require(recipe["state"] == "enabled" and recipe["revoked"] is False and recipe["digest"] == form["recipe"]["digest"]
            and {(item["component"], item["version"], item["digest"]) for item in recipe["manifest"]["steps"]} == {PYTHON},
            "management_recipe_not_admitted")
        plan = await self.invoke(session, "mcpme_plan_preparation_form", {**args, "candidate_id": form["candidate_id"],
            "recipe": form["recipe"], "form_digest": form["form_digest"], "answers": answers(form)})
        require(plan["execution_available"] is True and plan["blockers"] == [] and isinstance(plan["plan_id"], str)
            and re.fullmatch(r"[0-9a-f]{64}", plan["plan_digest"]), "management_review_unavailable")
        require((await self.invoke(session, "mcpme_list_preparations"))["preparations"] == []
            and (await self.invoke(session, "mcpme_list_resolutions"))["resolutions"] == [], "management_review_created_jobs")
        payload = {"plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())}
        await self.invoke(session, "mcpme_start_preparation", payload, error="grant_scope")
        await self.invoke(session, "mcpme_enable_recipe", {"recipe_id": recipe["recipe_id"], "version": recipe["version"]}, error="grant_scope")
        await self.invoke(session, "mcpme_apply_install", {"plan_id": str(uuid.uuid4())}, error="grant_scope")
        return draft, plan

    async def wait_job(self, state, environment, home, identifier, *, owned=False, observation=None):
        async with asyncio.timeout(600):
            while True:
                job = await self.cli(state, environment, home, "preparations", "get", identifier, "--json")
                if owned:
                    require(job["state"] not in TERMINAL, "management_slow_worker_not_observed")
                    if job["state"] == "verifying" and common.preparation_ownership_count(state, identifier) > 0:
                        require((state / "preparation-reservations" / (identifier + ".json")).is_file(), "management_reservation_missing")
                        require(observation is not None, "management_probe_observation_required")
                        reader, manager, manager_identity, marker = observation
                        expected = state / "preparations" / identifier
                        require(job["workspace"] == str(expected), "management_probe_workspace_changed")
                        witness = probe_entry(reader, manager, manager_identity, self.reaper,
                            expected / "candidate/python/bin/python3.12", marker)
                        if witness is not None:
                            # Re-read durable stage/ownership/reservation after
                            # observing a real entry; pending launch alone fails.
                            current = await self.cli(state, environment, home, "preparations", "get", identifier, "--json")
                            require(current["state"] == "verifying" and current["workspace"] == job["workspace"]
                                and common.preparation_ownership_count(state, identifier) > 0
                                and (state / "preparation-reservations" / (identifier + ".json")).is_file()
                                and probe_entry(reader, manager, manager_identity, self.reaper,
                                    expected / "candidate/python/bin/python3.12", marker) == witness,
                                "management_probe_identity_changed")
                            self.report["probe_entry_witness"] = {key: list(value) for key, value in witness.items()}
                            return current
                elif job["state"] in TERMINAL:
                    return job
                await asyncio.sleep(.1)

    async def settled(self, state, environment, home, job, draft, *, canceled):
        require(job["state"] == ("canceled" if canceled else "succeeded") and job["ownership_settled"] is True
            and job["reservation_retained"] is False and common.preparation_ownership_count(state, job["job_id"]) == 0
            and not (state / "preparation-reservations" / (job["job_id"] + ".json")).exists(), "management_work_unsettled")
        current = await self.cli(state, environment, home, "imports", "inspect", draft["draft_id"], "--json")
        require((current.get("attachment") is None) == canceled
            and (await self.cli(state, environment, home, "slots", "list", "--json"))["slots"] == [], "management_attachment_or_install_changed")
        if not canceled:
            from mcpme.contracts import validate_package
            package = validate_package(Path(current["attachment"]["path"]))
            require(package.digest == job["expected_package_digest"] == job["verification"]["package_digest"]
                and job["verification"]["tools_checked"] == job["verification"]["fixtures_checked"] == 1,
                "management_actual_probe_missing")

    async def case(self, label, *, revoke):
        base, state, home, environment = self.new_case(label)
        source = base / "source"; source.mkdir(mode=0o700)
        marker = "mcpme-probe-" + uuid.uuid4().hex
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copyfile(self.host / name, source / name)
        (source / "LICENSE").write_text("Synthetic fixture dedicated to the public domain under CC0.\n")
        (source / "worker.py").write_text("def double(n: int) -> dict[str, int]:\n"
            + ("    import ctypes, time\n    api = ctypes.CDLL('/usr/lib/libSystem.B.dylib')\n"
                "    api.pthread_setname_np.argtypes = [ctypes.c_char_p]\n    api.pthread_setname_np.restype = ctypes.c_int\n"
                "    if api.pthread_setname_np(" + repr(marker.encode("ascii")) + ") != 0:\n"
                "        raise RuntimeError('synthetic_probe_marker_failed')\n    time.sleep(120)\n" if revoke else "")
            + "    return {'value': n * 2}\n")
        before = signed.snapshot(source)
        child = await self.start(state, environment, home)
        try:
            reader = DarwinProcessReader() if revoke else None
            manager_identity = reader.identity(child.pid) if revoke else None
            require(not revoke or manager_identity is not None, "management_original_manager_changed")
            prepare = await self.cli(state, environment, home, "authorize", "--prepare", "--json")
            async with self.sdk(state, environment, home, Path(prepare["credential_file"])) as session:
                draft, plan = await self.review(session, source)
            key = str(uuid.uuid4())
            grant = await self.cli(state, environment, home, "authorize", "--preparation-plan", plan["plan_id"],
                "--plan-digest", plan["plan_digest"], "--idempotency-key", key, "--ttl-seconds", "3600", "--json")
            require(grant["mode"] == "preparation-execution" and grant["job_id"] is None
                and grant["plan_id"] == plan["plan_id"] and grant["plan_digest"] == plan["plan_digest"]
                and grant["idempotency_key"] == key, "management_owner_grant_changed")
            credential = base / "retained-credential.json"
            common.write_private_json(credential, common.private_json(Path(grant["credential_file"])))
            await self.cli(state, environment, home, "--credential-file", credential, "authorize", "--prepare", "--json",
                expected_error="grant_scope")
            await self.cli(state, environment, home, "--credential-file", credential, "secrets", "list", "--json",
                expected_error="grant_scope")
            payload = {"plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": key}
            async with self.sdk(state, environment, home, credential) as session:
                await self.invoke(session, "mcpme_start_preparation", {**payload, "idempotency_key": str(uuid.uuid4())}, error="grant_scope")
                await self.invoke(session, "mcpme_create_import", {"source": str(source), "name": "Denied"}, error="grant_scope")
                await self.invoke(session, "mcpme_apply_install", {"plan_id": str(uuid.uuid4())}, error="grant_scope")
                first = await self.invoke(session, "mcpme_start_preparation", payload)
                duplicate = await self.invoke(session, "mcpme_start_preparation", payload)
                require(first["job_id"] == duplicate["job_id"], "management_attempt_duplicated")
                require([item["job_id"] for item in (await self.invoke(session, "mcpme_list_preparations"))["preparations"]]
                    == [first["job_id"]], "management_job_scope_changed")
                active = await self.wait_job(state, environment, home, first["job_id"], owned=revoke,
                    observation=(reader, child, manager_identity, marker) if revoke else None)
                captured = binding_snapshot(state, grant, active)
                if revoke:
                    await self.cli(state, environment, home, "grants", "revoke", grant["grant_id"], "--json")
                    await self.invoke(session, "mcpme_capabilities", error="grant_revoked")
                final = await self.wait_job(state, environment, home, first["job_id"])
                await self.settled(state, environment, home, final, draft, canceled=revoke)
                if revoke:
                    require(all(reader.identity(value[0]) != tuple(value)
                        for value in self.report["probe_entry_witness"].values()), "management_observed_process_still_live")
            require(binding_snapshot(state, grant, final) == captured, "management_binding_changed_after_settle")
            await self.shutdown(child, state)
            child = await self.start(state, environment, home)
            restarted = await self.cli(state, environment, home, "preparations", "get", final["job_id"], "--json")
            require(binding_snapshot(state, grant, restarted) == captured, "management_binding_changed_on_restart")
            await self.settled(state, environment, home, restarted, draft, canceled=revoke)
            async with self.sdk(state, environment, home, credential) as session:
                if revoke:
                    await self.invoke(session, "mcpme_start_preparation", payload, error="grant_revoked")
                    await self.cli(state, environment, home, "grants", "revoke", grant["grant_id"], "--json")
                else:
                    repeated = await self.invoke(session, "mcpme_start_preparation", payload)
                    require(repeated["job_id"] == final["job_id"], "management_restart_attempt_duplicated")
            await self.settled(state, environment, home, restarted, draft, canceled=revoke)
            jobs = (await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"]
            require(len(jobs) == 1 and jobs[0]["job_id"] == final["job_id"] and signed.snapshot(source) == before,
                "management_source_or_job_changed")
            return {"passed": True, **captured, "source_files": 4, "source_preserved": True,
                "metadata_review_created_jobs": False, "exact_attempt_duplicate_and_restart": True,
                "state": final["state"], "actual_sealed_probe_completed": not revoke,
                "live_owned_probe_observed": revoke, "revoked_token_copy_denied_after_restart": revoke,
                "ownership_ledger_empty": True, "reservation_absent_after_settle_and_restart": True,
                "observed_reservation_removals": 1 if revoke else None, "installation_slots": 0}
        finally:
            await self.shutdown(child, state)


async def verify(host_path, native_path, report, *, source_root):
    app = common.native_app_root(native_path)
    require(app is not None and host_path == app / "Contents/Resources/host", "management_signed_pair_required")
    before = signed.snapshot(app)
    source_before = source_snapshot(source_root)
    try:
        active_policy(host_path)
        require(Path(__file__).resolve() == source_root / "scripts/verify_phase3_public_management.py",
            "management_driver_source_mismatch")
        signed.signature(app, deep=True)
        common.verify_host_payload(host_path)
        with common.isolated_workspace(report) as (root, lease):
            host, native = common.relocate_host_and_native(root, host_path, native_path, report)
            require(report["native_bundle_preserved"] is True, "management_signed_bundle_not_preserved")
            common._host_paths(host); build = common.verify_host_payload(host)
            require(build["source_commit"] == source_before[0], "management_source_app_commit_mismatch")
            common._host_imports(host)
            # Typed inspection passes no authority to a host or worker.
            agent_scope = typed_policy_scope(active_policy(host))
            relocated = common.native_app_root(native)
            copied = signed.snapshot(relocated)
            harness = ManagementHarness(root, host, native, lease, report=report)
            report.update(host_source_commit=build["source_commit"], host_build_sha256=common.digest(host / "build.json"),
                policy_sha256=common.digest(host / POLICY_PATH), original_app_files=sum(item[1] is not None for item in before.values()),
                source_commit=source_before[0], source_files=len(source_before[1]),
                agent_policy_scope=agent_scope,
                driver_sha256=common.digest(Path(__file__).resolve()),
                source_inventory_sha256=hashlib.sha256(json.dumps(source_before[1], sort_keys=True).encode()).hexdigest(),
                relocated_whole_signed_app=True, public_execution_authority="shipped_signed_release_policy",
                public_factory_or_admission_injection=False, physical_native_clicks_verified=False)
            try:
                report["cases"]["completed"] = await harness.case("completed", revoke=False)
                report["cases"]["revoked"] = await harness.case("revoked", revoke=True)
                common._check_module_origins(host)
            finally:
                await harness.cleanup()
                report["driver_children_joined"] = not harness.children
            # Root cosmetic xattr/ctime changes are permitted by the released
            # contract; every regular-file identity/hash/mode remains exact.
            after = signed.snapshot(relocated)
            require(set(copied) == set(after) and {key: value for key, value in copied.items() if value[1] is not None}
                == {key: value for key, value in after.items() if value[1] is not None}, "management_relocated_app_changed")
            signed.signature(relocated, deep=True)
            report["relocated_app_files_preserved"] = True
    finally:
        report["original_signed_app_preserved"] = signed.snapshot(app) == before
        report["source_tree_preserved"] = source_snapshot(source_root) == source_before
        require(report["original_signed_app_preserved"] and report["source_tree_preserved"], "management_original_inputs_changed")
    report["verified"] = report["driver_children_joined"] and all(item["passed"] for item in report["cases"].values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "native", "source-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(); os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "public_signed_management_mcp_preparation",
        "public_cli_starts": 0, "sdk_sessions_exited": 0, "cases": {}, "limits": ["physical_native_clicks_unverified",
            "release_callback_count_not_observable", "no_provider_model_or_install_workloads",
            "no_hard_aggregate_or_complete_lifetime_measurement_claim", "sdk_transport_owns_its_facade_process"]}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True), report,
            source_root=args.source_root.resolve(strict=True)))
    except (Exception, KeyboardInterrupt) as error:
        failure, _ = common.bounded_failure(error)
        report["verified"] = False
        report["diagnostic"] = {"code": failure.code if isinstance(failure, common.AcceptanceError)
            else "management_acceptance_failed"}
    try:
        common.write_private_json(args.output.absolute(), report)
    except Exception:
        report["verified"] = False
    print(json.dumps({"verified": report["verified"], "scope": report["scope"], "diagnostic": report.get("diagnostic")}))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
