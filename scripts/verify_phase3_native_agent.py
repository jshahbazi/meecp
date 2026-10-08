"""Developer-only native generated-adapter acceptance with a simulated provider.

The provider sees only the production backend's encoded reviewed request. The
ordinary host resolver selects dependencies; the fixture supplies no lock or
version. No CLI switch or persisted setting enables production execution.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import time
import tomllib
import uuid

import verify_phase3_native_onboarding as common

CASES = ("scalar", "dependency")
NATIVE_STAGES = common.NATIVE_STAGES | {"agent_options", "agent_review", "agent_start", "agent_poll", "agent_handoff"}
PROVIDER_ID = "simulated-native-fixture"
MODEL = "request-only-fixture"
RECIPE = "mcpme.agent-unlocked-python-adapter"
ENABLED_HOST = (
    "import asyncio,logging,runpy,sys\nfrom pathlib import Path\n"
    "sys.path[:0]=[sys.argv[1],sys.argv[2]]\nlogging.disable(logging.CRITICAL)\n"
    "fixture=runpy.run_path(sys.argv[6])\nimport httpx\n"
    "from meecp.supervisor import serve\nfrom meecp.preparation_runner import PreparationRunner\n"
    "from meecp.preparation_agent_unlocked_resolution import OwnedAgentUnlockedResolutionRunner\n"
    "from meecp.preparation_agent_host import AgentPreparationHost,AgentProviderConfiguration\n"
    "from meecp.preparation_agent_backend import OpenAIResponsesBackend\n"
    "def preparation(*args,**kwargs):\n"
    " selected=next(f for f in args[0].families.values() if f.component.reference.id=='agent-unlocked-python-adapter-v1')\n"
    " kwargs.update(enabled=True,enabled_families=frozenset({selected.component.reference.key}))\n"
    " return PreparationRunner(*args,**kwargs)\n"
    "def resolution(*args,**kwargs):\n kwargs['enabled']=True\n return OwnedAgentUnlockedResolutionRunner(*args,**kwargs)\n"
    "def agent(*args,**kwargs):\n"
    " backend=OpenAIResponsesBackend(lambda _: 'synthetic-only',transport=httpx.MockTransport(fixture['request_only_unlocked_handler']))\n"
    " provider=AgentProviderConfiguration('simulated-native-fixture',backend,('request-only-fixture',),'synthetic-reference',True)\n"
    " return AgentPreparationHost(*args,**kwargs,providers=(provider,),simulated_enabled=True)\n"
    "asyncio.run(serve(Path(sys.argv[3]),Path(sys.argv[4]),int(sys.argv[5]),"
    "preparation_runner_factory=preparation,resolution_runner_factory=resolution,agent_preparation_factory=agent))\n"
)


def file_identity(path):
    info = path.lstat()
    common.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, "unsafe_agent_fixture_identity")
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size,
        "mode": stat.S_IMODE(info.st_mode), "modified_seconds": info.st_mtime_ns // 10**9,
        "modified_nanoseconds": info.st_mtime_ns % 10**9, "changed_seconds": info.st_ctime_ns // 10**9,
        "changed_nanoseconds": info.st_ctime_ns % 10**9}


def native_fixture(base, state, draft_id, recipe):
    nonce = str(uuid.uuid4())
    output, fixture = base / "native-agent-receipt.json", base / "native-agent-case.json"
    marker = state / ("native-agent-" + nonce + ".json")
    common.write_private_json(marker, {"schema_version": 1, "fixture_nonce": nonce,
        "expires_at": int(time.time()) + 295, "manager_file": file_identity(state / "manager-connection.json")})
    common.write_private_json(fixture, {"schema_version": 1, "fixture_nonce": nonce,
        "state_dir": str(state), "draft_id": draft_id, "output_path": str(output), "answers_by_label": {},
        "agent_scope": {"provider_id": PROVIDER_ID, "model": MODEL, "recipe": recipe,
            "selected_paths": ["worker.py"], "selected_metadata_paths": ["LICENSE"]}})
    return fixture, output


def job_counts(state):
    result = {}
    for name, tables in (("imports.sqlite", ("preparation_jobs", "resolution_jobs")),
            ("agent-preparations/attempts.sqlite3", ("agent_host_jobs", "attempts"))):
        path = state / name
        if not path.exists():
            continue
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            for table in tables:
                result[table] = db.execute("SELECT count(*) FROM " + table).fetchone()[0]
        finally:
            db.close()
    return result


class Harness(common.Harness):
    async def defaults(self):
        await super().defaults()
        base, state, home, environment = self.new_case("agent-production-defaults")
        child = await self.start(state, environment, home, enabled=False)
        try:
            providers = await self.get(state, "/v1/agent-providers")
            common.require(providers["providers"] == [] and providers["live_execution_enabled"] is False,
                "production_agent_enabled")
            common.require(all(value == 0 for value in job_counts(state).values()), "production_agent_job_created")
            return True
        finally:
            await self.shutdown(child, state)

    async def run(self, argv, environment, cwd, *, timeout=330, capture=False):
        # Caller output can contain synthetic source or owner credentials. Keep
        # it only in memory; never persist raw CLI/native stdout or exceptions.
        child = self.spawn(argv, environment, cwd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL)
        try:
            output, _ = await asyncio.to_thread(child.communicate, timeout=timeout)
            output = output or b""
            common.require(len(output) <= common.MAX_JSON_BYTES, "acceptance_command_output_limit")
            return child.returncode, output
        except subprocess.TimeoutExpired as expired:
            error = common.AcceptanceError("acceptance_child_timeout")
            for line in (expired.output or b"")[:65536].splitlines():
                if line.startswith(b"native_agent_stage="):
                    stage = line.removeprefix(b"native_agent_stage=").decode("ascii", errors="ignore")
                    if stage in NATIVE_STAGES:
                        error.native_stage = stage
            raise error from None
        finally:
            await self.stop(child)

    async def start(self, state, environment, home, *, enabled):
        if not enabled:
            return await super().start(state, environment, home, enabled=False)
        simulation = Path(__file__).resolve().with_name("agent_acceptance_fixture.py")
        argv = [str(self.host / "python/bin/python3.12"), "-I", "-S", "-B", "-c", ENABLED_HOST,
            str(self.host / "backend"), str(self.host / "python/site-packages"), str(state),
            str(self.reaper), str(os.getpid()), str(simulation)]
        child = self.spawn(argv, environment, home)
        try:
            async with asyncio.timeout(30):
                while child.poll() is None:
                    if (state / "manager-connection.json").is_file():
                        try:
                            await self.cli(state, environment, home, "capabilities", "--json")
                            return child
                        except (common.AcceptanceError, OSError):
                            pass
                    await asyncio.sleep(.1)
            raise common.AcceptanceError("acceptance_host_exited")
        except BaseException:
            await self.shutdown(child, state)
            raise

    async def get(self, state, route):
        import httpx
        from meecp_local_transport import private_transport, socket_path
        connection = common.private_json(state / "manager-connection.json")
        async with httpx.AsyncClient(base_url=connection["endpoint"], trust_env=False, timeout=30,
                follow_redirects=False, transport=private_transport(socket_path(state, "manager")),
                headers={"Authorization": "Bearer " + connection["token"]}) as client:
            response = await client.get(route)
            common.require(len(response.content) <= common.MAX_JSON_BYTES, "manager_response_limit")
            value = response.json()
            common.require(response.status_code == 200 and isinstance(value, dict) and "error" not in value,
                "manager_request_failed")
            return value

    async def shutdown(self, child, state):
        await super().shutdown(child, state)
        ledger = state / "agent-preparations/attempts.sqlite3"
        try:
            if ledger.exists():
                db = sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)
                try:
                    for table in ("agent_host_jobs", "attempts"):
                        count = db.execute("SELECT count(*) FROM " + table + " WHERE json_extract(data,'$.local_io_settled') IS NOT 1").fetchone()[0]
                        common.require(count == 0, "agent_ownership_retained")
                finally:
                    db.close()
        except BaseException:
            self.lease["safe_to_remove"] = False
            raise

    async def case(self, label):
        from meecp.contracts import Tool, validate_package
        from meecp.preparation_agent_adapter_store import CODE_PATH
        from meecp.preparation_agent_unlocked_contracts import DEPENDENCY_NAMESPACE
        from meecp.preparation_inputs import parse_preparation_descriptor, parse_preparation_receipt
        from meecp.preparation_verifier import verify_prepared_package
        from meecp.processes import ProcessOwner

        common.stage("source_intake")
        started = time.monotonic()
        base, state, home, environment = self.new_case(label)
        source = base / "unfamiliar source"; source.mkdir(mode=0o700)
        content = "def double(n):\n return n*2\n" if label == "scalar" else (
            "import humanize\ndef double(n):\n return int(humanize.intcomma(n*2).replace(',', ''))\n")
        (source / "worker.py").write_text(content)
        (source / "LICENSE").write_text("Synthetic original-code permission.\n")
        (source / "unknown.payload").write_bytes(b"\0\xffsynthetic\n")
        original = {path.name: common.digest(path) for path in source.iterdir()}
        child = await self.start(state, environment, home, enabled=True)
        try:
            draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                "--name", "Synthetic native generated adapter", "--intent", "Wrap the selected scalar callable.",
                "--source-mode", "automatic", "--start", "--start-policy", "host", "--json")
            common.require({row["path"]: row["sha256"] for row in draft["source"]["files"]} == original,
                "automatic_intake_dropped_original")
            inspected = await self.cli(state, environment, home, "preparations", "inspect-source", "--import",
                draft["draft_id"], "--revision", draft["revision"], "--json")
            common.require(not inspected["candidates"], "fixture_has_deterministic_candidate")
            options = await self.manager(state, "/v1/agent-preparations/plans",
                {"import_id": draft["draft_id"], "revision": draft["revision"]})
            recipes = [row for row in options["recipes"] if row["id"] == RECIPE]
            common.require(len(recipes) == 1 and options["providers"][0]["simulated"] is True,
                "simulated_agent_options_missing")
            recipe = recipes[0]
            fixture, output = native_fixture(base, state, draft["draft_id"], recipe)
            common.stage("native")
            try:
                code, _ = await self.run([str(self.native), "--agent-native-acceptance", str(fixture)], environment, home, timeout=360, capture=True)
            except common.AcceptanceError as error:
                error.native_observation = job_counts(state)
                raise
            common.require(output.is_file(), "native_agent_receipt_missing")
            native = common.private_json(output, 262144)
            if code or native.get("passed") is not True:
                error = common.AcceptanceError(native.get("error_code", "native_agent_acceptance_failed"))
                error.native_stage = native.get("stage") if native.get("stage") in NATIVE_STAGES else "connection"
                raise error
            common.require(native["stage"] == "attached" and native["draft_id"] == draft["draft_id"]
                and native["source_revision"] == draft["source"]["revision"]
                and native["agent_revision"] == draft["revision"]
                and native["pending_proposal_handoff"] is True and native["simulated_provider"] is True,
                "native_agent_binding_mismatch")
            routes = native["routes"]
            common.require({"POST v1/agent-preparations/plans", "POST v1/agent-preparations/start",
                "POST v1/preparations/form", "POST v1/resolutions/plan", "POST v1/resolutions/start",
                "POST v1/preparations/plan-form", "POST v1/preparations/start"} <= set(routes), "native_agent_routes_missing")
            common.require(routes.index("POST v1/agent-preparations/start") < routes.index("POST v1/preparations/form")
                < routes.index("POST v1/resolutions/start") < routes.index("POST v1/preparations/start"), "native_agent_authority_order")
            agent = await self.get(state, "/v1/agent-preparations/jobs/" + native["agent_job_id"])
            common.require(agent["state"] == "succeeded" and agent["candidate"] is None and agent["local_io_settled"]
                and agent["proposal_digest"] == native["proposal_digest"] and len(agent["calls"]) == 4
                and agent["verification"] is None and not agent["installed"], "agent_proposal_boundary_failed")
            resolved = await self.cli(state, environment, home, "resolutions", "get", native["resolution_job_id"], "--json")
            prepared = await self.cli(state, environment, home, "preparations", "get", native["preparation_job_id"], "--json")
            common.require(all(job["state"] == "succeeded" and job["ownership_settled"] and not job["reservation_retained"]
                for job in (resolved, prepared)), "native_agent_job_unsettled")
            attached = await self.cli(state, environment, home, "imports", "inspect", draft["draft_id"], "--json")
            common.require(attached["revision"] == native["attached_revision"] == native["reviewed_revision"] == draft["revision"] + 1
                and attached["source"]["revision"] == draft["source"]["revision"] and attached["state"] == "ready_to_plan",
                "native_agent_attachment_revision")
            package_root = Path(attached["attachment"]["path"]); package = validate_package(package_root)
            common.require(package.digest == attached["attachment"]["digest"] == prepared["expected_package_digest"]
                == native["attached_package_digest"], "native_agent_attachment_digest")
            proof = parse_preparation_receipt((package_root / "mcpme.preparation.json").read_bytes())
            common.require(proof.binding.revision == attached["revision"] and proof.binding.source_revision == draft["source"]["revision"]
                and not proof.inputs.original_dependency_locks and len(proof.inputs.derived_inputs) == 5,
                "native_agent_derived_binding")
            common.require(all(common.digest(package_root / "source" / name) == digest for name, digest in original.items())
                and (package_root / "source" / CODE_PATH).is_file(), "native_agent_source_changed")
            project = tomllib.loads((package_root / "source" / DEPENDENCY_NAMESPACE / "pyproject.toml").read_text())
            common.require(("humanize" in project["project"]["dependencies"]) == (label == "dependency"),
                "request_requirement_changed")
            self.source_check(source, original)
            common.stage("installation")
            plan = await self.cli(state, environment, home, "install", "plan", "--import", draft["draft_id"],
                "--revision", attached["revision"], "--json")
            tools = [Tool.model_validate(row).mcp_schema() for row in plan["package"]["tools"]]
            common.require(tools == [item.mcp_schema() for item in package.manifest.tools], "agent_install_contract_changed")
            operation = await self.finish_operation(state, environment, home,
                await self.cli(state, environment, home, "install", "apply", "--plan", plan["plan_id"], "--json"))
            slot = operation["slot_id"]
            calls = [("double", {"n": 7}, {"value": 14})]
            http_calls = await self.sdk(state, environment, home, slot, tools, calls, "http")
            stdio_calls = await self.sdk(state, environment, home, slot, tools, calls, "stdio")
            self.source_check(source, original, remove=True)
            await self.shutdown(child, state)
            child = await self.start(state, environment, home, enabled=True)
            resumed = await self.cli(state, environment, home, "imports", "inspect", draft["draft_id"], "--json")
            common.require(resumed["state"] == "installed" and resumed["result_slot_id"] == slot and not source.exists(),
                "agent_installed_restart_failed")
            calls = [("double", {"n": -4}, {"value": -8})]
            http_calls += await self.sdk(state, environment, home, slot, tools, calls, "http")
            stdio_calls += await self.sdk(state, environment, home, slot, tools, calls, "stdio")
            await self.shutdown(child, state)
            # Preserve the accepted descriptor in RAM, remove captured state,
            # then run the copied package through the host's confined SDK probe.
            db = sqlite3.connect((state / "imports.sqlite").as_uri() + "?mode=ro", uri=True)
            try:
                selected = json.loads(db.execute("SELECT data FROM preparation_selections WHERE import_id=?", (draft["draft_id"],)).fetchone()[0])
            finally:
                db.close()
            descriptor = parse_preparation_descriptor(selected["descriptor"])
            moved = base / "relocated generated package"; shutil.copytree(package_root, moved)
            # Keep the state ledger until ordinary cleanup verifies ownership;
            # removing the captured source tree suffices to rule out execution
            # from original capture during this isolated probe.
            captured = Path(attached["source"]["snapshot_path"])
            common.require(captured.is_relative_to(state) and captured.is_dir(), "captured_source_scope")
            shutil.rmtree(captured)
            probe = base / "relocated probe"; probe.mkdir(mode=0o700)
            owner = ProcessOwner(self.reaper)
            result = await verify_prepared_package(moved, descriptor, self.reaper, probe,
                timeout_seconds=30, owner=owner, confine_private_python=True)
            common.require(result.tools_checked == result.fixtures_checked == 1 and result.package_digest == package.digest,
                "relocated_agent_probe_failed")
            return {"passed": True, "simulated_provider": True, "live_provider_calls": 0,
                "provider_calls": len(agent["calls"]), "native_receipt_sha256": common.digest(output), "native_routes": routes,
                "proposal_digest": native["proposal_digest"], "recipe": recipe, "package_digest": package.digest,
                "source_revision": draft["source"]["revision"], "original_file_sha256": original,
                "reviewed_revision": attached["revision"], "pending_proposal_handoff": True,
                "resolution_progress": resolved["progress"], "preparation_progress": prepared["progress"],
                "resolution_producer": resolved["request"]["producer"], "resolution_policy": resolved["request"]["policy"],
                "http_calls": http_calls, "stdio_calls": stdio_calls, "source_removed": True,
                "captured_source_removed": True, "restart_invoked": True, "relocated_confined_probe": True,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
        except BaseException as error:
            if not isinstance(error, common.AcceptanceError):
                error.acceptance_context = dict(common.DIAGNOSTIC)
            raise
        finally:
            await self.shutdown(child, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "simulated_native_generated_adapter",
        "cases": {}, "simulated_provider": True, "live_provider_calls": 0, "a5_scope_complete": False,
        "limits": ["live_provider_acceptance_unverified", "physical_dialog_interaction_unverified",
            "public_execution_disabled", "whole_phase_adversarial_review_pending"]}
    try:
        asyncio.run(common.verify(args.host.resolve(strict=True), args.native.resolve(strict=True), report,
            harness_factory=Harness, cases=CASES))
    except (Exception, KeyboardInterrupt) as error:
        error, _ = common.bounded_failure(error)
        report["diagnostic"] = {"stage": getattr(error, "native_stage", getattr(error, "acceptance_context", common.DIAGNOSTIC)["stage"]),
            "code": error.code if isinstance(error, common.AcceptanceError) else "acceptance_failed"}
        if type(error).__name__ in {"KeyError", "ValueError", "TypeError", "AttributeError", "AssertionError", "ImportError", "PreparationError",
                "FileNotFoundError", "NotADirectoryError", "PermissionError", "OSError", "NameError", "UnboundLocalError", "ExceptionGroup"}:
            report["diagnostic"]["type"] = type(error).__name__
        trace = error.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == __file__:
                report["diagnostic"]["harness_line"] = trace.tb_lineno
            trace = trace.tb_next
        if isinstance(error, common.AcceptanceError) and hasattr(error, "native_observation"):
            report["native_observation"] = error.native_observation
    common.write_private_json(args.output.absolute(), report)
    print(json.dumps({"verified": report["verified"], "cases_passed": sum(item.get("passed") is True for item in report["cases"].values())}))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
