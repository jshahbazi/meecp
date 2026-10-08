"""Development-only native directory to measured command and installed SDK proof.

The supplied fixture is an explicitly compiled synthetic executable. Typed host
admissions are constructed only in this private embedding, never by an ordinary
CLI payload, environment switch, inventory, recipe, or selected application.
Selected folder and form answers simulate user choices through production native
handlers. This is not a physical-dialog, signing, hard aggregate, or lifetime claim.
"""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from types import SimpleNamespace
import uuid

import verify_phase3_native_onboarding as onboarding
import verify_phase3_command_host as command_host

require = onboarding.require
digest = onboarding.digest
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
ENABLED_HOST = (
    "import asyncio,json,logging,sys\nfrom pathlib import Path\n"
    "sys.path[:0]=[sys.argv[1],sys.argv[2]]\nlogging.disable(logging.CRITICAL)\n"
    "import meecp.supervisor as module\n"
    "from meecp.host_command import HostCommandAdmission\n"
    "from meecp.preparation_command_owner import CommandPreparationResourceAdmission\n"
    "from meecp.command_resource_execution_v1 import _platform_policy\n"
    "from meecp.preparation_command import CommandFamily\n"
    "from meecp.preparation_runner import PreparationRunner\n"
    "root=Path(sys.argv[4]).parent.parent\n"
    "command=json.loads((root/'command-execution.json').read_bytes())\n"
    "resource=json.loads((root/'command-resource-execution.json').read_bytes())\n"
    "admission=HostCommandAdmission(root/command['helper'],command['helper_sha256'])\n"
    "measured=CommandPreparationResourceAdmission(root/resource['helper'],resource['helper_sha256'],_platform_policy())\n"
    "class DevelopmentSupervisor(module.Supervisor):\n"
    " def __init__(self,*args,**kwargs):\n"
    "  kwargs['command_admission']=admission\n  kwargs['command_resource_admission']=measured\n"
    "  super().__init__(*args,**kwargs)\n"
    "def preparation(*args,**kwargs):\n kwargs['enabled']=True\n"
    " kwargs['enabled_families']=frozenset({CommandFamily(command['helper_sha256']).component.reference.key})\n"
    " return PreparationRunner(*args,**kwargs)\n"
    "module.Supervisor=DevelopmentSupervisor\n"
    "asyncio.run(module.serve(Path(sys.argv[3]),Path(sys.argv[4]),int(sys.argv[5]),preparation_runner_factory=preparation))\n"
)


def file_identity(path):
    value = path.stat(follow_symlinks=False)
    return {"device": value.st_dev, "inode": value.st_ino, "bytes": value.st_size, "mode": value.st_mode & 0o777,
        "modified_seconds": value.st_mtime_ns // 1_000_000_000, "modified_nanoseconds": value.st_mtime_ns % 1_000_000_000,
        "changed_seconds": value.st_ctime_ns // 1_000_000_000, "changed_nanoseconds": value.st_ctime_ns % 1_000_000_000}


def native_fixture(base, state, source, answers):
    nonce = str(uuid.uuid4())
    fixture, output = base / "native-directory.json", base / "native-receipt.json"
    identity = source.stat(follow_symlinks=False)
    onboarding.write_private_json(state / ("native-directory-" + nonce + ".json"), {
        "schema_version": 1, "fixture_nonce": nonce, "expires_at": int(time.time()) + 300,
        "manager_file": file_identity(state / "manager-connection.json"),
        "source_directory": {"device": identity.st_dev, "inode": identity.st_ino}})
    onboarding.write_private_json(fixture, {"schema_version": 1, "fixture_nonce": nonce,
        "state_dir": str(state), "output_path": str(output), "source_directory": str(source), "answers_by_label": answers})
    return fixture, output


def command_authority_settled(path):
    """Read settlement proof without reconciling, mutating, or adopting a PID."""
    value = onboarding.private_json(path)
    if path.name == "command-reservations.json":
        return (set(value) == {"version", "entries"} and type(value["version"]) is int
            and value["version"] == 1 and type(value["entries"]) is list and not value["entries"])
    from meecp.command_execution_v2 import validate_command_journal, verify_command_completion
    validate_command_journal(value)
    return all(item["state"] == "settled" and verify_command_completion(path.parent, item)[0]
        for item in value["entries"])


class CommandHarness(onboarding.Harness):
    def __init__(self, *args, fixture):
        super().__init__(*args)
        self.fixture = fixture

    async def run(self, argv, environment, cwd, *, timeout=330, capture=False):
        child = self.spawn(argv, environment, cwd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(asyncio.to_thread(child.communicate), timeout)
            data = output or b""
            require(len(data) <= onboarding.MAX_JSON_BYTES, "acceptance_command_output_limit")
            return child.returncode, data if capture else b""
        except TimeoutError:
            raise onboarding.AcceptanceError("acceptance_child_timeout") from None
        finally:
            await self.stop(child)

    def source_fixture(self, source, label):
        shutil.copyfile(self.fixture, source / "command"); (source / "command").chmod(0o700)
        for name in ("command.input.schema.json", "command.output.schema.json"):
            (source / name).write_text(json.dumps(SCHEMA, sort_keys=True))
        (source / "LICENSE").write_text("Synthetic meecp tests/fixtures/host_command_target.c acceptance fixture notice.\n")
        return {path.name: digest(path) for path in source.iterdir()}

    async def start(self, state, environment, home, *, enabled):
        if not enabled:
            return await super().start(state, environment, home, enabled=False)
        argv = [str(self.host / "python/bin/python3.12"), "-I", "-S", "-B", "-c", ENABLED_HOST,
            str(self.host / "backend"), str(self.host / "python/site-packages"), str(state), str(self.reaper), str(os.getpid())]
        child = self.spawn(argv, environment, home)
        try:
            async with asyncio.timeout(30):
                while child.poll() is None:
                    if (state / "manager-connection.json").is_file():
                        try:
                            await self.cli(state, environment, home, "capabilities", "--json")
                            return child
                        except (onboarding.AcceptanceError, OSError):
                            pass
                    await asyncio.sleep(0.1)
            raise onboarding.AcceptanceError("acceptance_host_exited")
        except BaseException:
            await self.shutdown(child, state)
            raise

    async def shutdown(self, child, state):
        await super().shutdown(child, state)
        try:
            for path in (state / "slots").glob("*/command-reservations.json"):
                require(command_authority_settled(path), "command_reservation_retained")
            for path in (state / "slots").glob("*/command-v3/command-v2.json"):
                require(command_authority_settled(path), "command_journal_unsettled")
        except BaseException:
            self.lease["safe_to_remove"] = False
            raise

    async def cleanup(self):
        # The injected fault controls create their own Supervisor children. A
        # failed control must preserve their authority as well as driver state.
        for state in self.states:
            for path in (state / "slots").glob("*/ownership.json"):
                try:
                    if onboarding.private_json(path) != {}:
                        self.lease["safe_to_remove"] = False
                except Exception:
                    self.lease["safe_to_remove"] = False
            for pattern in ("*/command-reservations.json", "*/command-v3/command-v2.json"):
                for path in (state / "slots").glob(pattern):
                    try:
                        if not command_authority_settled(path):
                            self.lease["safe_to_remove"] = False
                    except Exception:
                        self.lease["safe_to_remove"] = False
        await super().cleanup()

    async def defaults(self):
        onboarding.stage("production_defaults", "defaults")
        base, state, home, environment = self.new_case("defaults")
        source = base / "ordinary source"; source.mkdir(mode=0o700)
        original = self.source_fixture(source, "defaults")
        child = await self.start(state, environment, home, enabled=False)
        try:
            capabilities = await self.cli(state, environment, home, "capabilities", "--json")
            require(capabilities["automatic_preparation"] is False
                and capabilities["recipe_execution"] is False, "production_execution_enabled")
            draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                "--source-mode", "automatic", "--name", "Production command defaults", "--json")
            require(draft["source"]["kind"] == "application_directory", "command_source_intake_missing")
            form = await self.manager(state, "/v1/preparations/form", {"import_id": draft["draft_id"], "revision": draft["revision"]})
            require(form["execution_available"] is False and form["resolution_required"] is False
                and form["recipe"]["id"] == "mcpme.native-command", "production_command_form_enabled")
            await self.cli(state, environment, home, "preparations", "start", "--plan", str(uuid.uuid4()),
                "--plan-digest", "a" * 64, "--idempotency-key", str(uuid.uuid4()), "--json",
                expected_error="preparation_runner_unavailable")
            require((await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"] == [],
                "production_default_job_created")
            self.source_check(source, original, remove=True)
            return True
        except BaseException as error:
            if not isinstance(error, onboarding.AcceptanceError):
                error.acceptance_context = dict(onboarding.DIAGNOSTIC)
            raise
        finally:
            await self.shutdown(child, state)

    async def retained_gateway_loss(self, state, inventory):
        """A killed legacy reaper provides no join proof; preserve its authority.

        The held backend Popen in command_host.verify was already waited. The
        gateway/reaper handles belonged to that killed process, so this path
        never adopts or signals a recorded PID. It checks public replacement
        admission and durable recovery, and deliberately retains this state.
        """
        from meecp.supervisor import HostError
        self.lease["safe_to_remove"] = False
        host = command_host.make_host(self.host, state, inventory)
        try:
            slots = host.registry.list_slots()
            require(len(slots) == 1, "lost_gateway_slot_count_changed")
            slot = slots[0]
            ledger = state / "slots" / slot.id / "ownership.json"
            journal = state / "slots" / slot.id / "command-v3/command-v2.json"
            before_ledger, before_journal = digest(ledger), digest(journal)
            require(len(onboarding.private_json(ledger)) == 1, "lost_gateway_authority_missing")
            await host.initialize()
            current = host.registry.get_slot(slot.id)
            require(current.observed_state == current.error_code == "ownership_lost"
                and slot.id in host.reservations and slot.id not in host.workers and slot.id not in host.listeners,
                "lost_gateway_restart_not_fenced")
            denials = 0
            for _ in range(3):
                try:
                    await host.control(slot.id, "start")
                except HostError as error:
                    require(error.code == "ownership_lost", "lost_gateway_wrong_denial")
                    denials += 1
                else:
                    raise onboarding.AcceptanceError("lost_gateway_replacement_admitted")
            require(not host.commands.pending(slot.id) and not host.commands.records[slot.id]
                and not host.workers and not any(owner.instances for owner in host.owners.values()),
                "lost_gateway_replacement_or_command_retained")
            result = {"expected_retention_verified": True, "reconciliation_required": True,
                "reservation_retained": True, "replacement_start_denials": denials, "new_owned_processes": 0,
                "unknown_legacy_child_joined": False, "recorded_pid_adoption_or_signaling": False,
                "isolated_command_settled": True, "legacy_ownership_sha256": before_ledger,
                "command_journal_sha256": before_journal}
        finally:
            await host.close()
        require(digest(ledger) == before_ledger and digest(journal) == before_journal,
            "lost_gateway_authority_erased")
        result["authority_preserved_after_close"] = True
        return result

    async def case(self, label):
        from meecp.contracts import Tool, validate_package
        from meecp.preparation_forms import _choice
        from meecp.preparation_inputs import parse_preparation_receipt
        onboarding.stage("source_intake", label)
        base, state, home, environment = self.new_case(label)
        source = base / "ordinary source"; source.mkdir(mode=0o700)
        original = self.source_fixture(source, label)
        started = time.monotonic()
        child = await self.start(state, environment, home, enabled=True)
        try:
            answers = {"Confirm the documented command interface": _choice("command-protocol", "json-stdin-stdout-v1").id,
                "Confirm the documented command effects": _choice("command-effects", "none").id,
                "Source license or notice": _choice("command-license", "LICENSE").id,
                "Synthetic test input": {"ok": True}, "Expected synthetic result": {"ok": True},
                "Fixed command arguments": {"args": ["measured-tree"]}, "Command deadline (milliseconds)": 2000}
            fixture, receipt_path = native_fixture(base, state, source, answers)
            onboarding.stage("native")
            code, _ = await self.run([str(self.native), "--directory-native-acceptance", str(fixture)], environment, home, timeout=180)
            require(receipt_path.is_file(), "native_directory_receipt_missing")
            receipt = onboarding.private_json(receipt_path, 262144)
            if code or receipt.get("passed") is not True:
                error = onboarding.AcceptanceError(receipt.get("error_code", "native_directory_failed"))
                error.native_stage = receipt.get("stage", "connection")
                jobs = (await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"]
                error.native_observation = {"routes": receipt.get("routes", []), "jobs": len(jobs),
                    "job_states": [job["state"] for job in jobs], "native_receipt_sha256": digest(receipt_path)}
                raise error
            require(receipt["native_directory_selection"] is True and receipt["stage"] == "attached"
                and receipt["recipe_id"] == "mcpme.native-command" and "resolution_job_id" not in receipt,
                "native_directory_path_changed")
            require({"POST v1/imports", "POST v1/preparations/form", "POST v1/preparations/plan-form",
                "POST v1/preparations/start"} <= set(receipt["routes"]), "native_directory_routes_missing")
            draft_id = receipt["draft_id"]
            prepared = await self.cli(state, environment, home, "preparations", "get", receipt["preparation_job_id"], "--json")
            attached = await self.cli(state, environment, home, "imports", "inspect", draft_id, "--json")
            require(prepared["state"] == "succeeded" and prepared["ownership_settled"] is True
                and prepared["reservation_retained"] is False and onboarding.preparation_ownership_count(state) == 0,
                "command_preparation_unsettled")
            require(attached["revision"] == receipt["attached_revision"] == receipt["reviewed_revision"]
                and attached["source"]["revision"] == receipt["source_revision"]
                and prepared["binding"]["revision"] == attached["revision"], "command_attachment_revision_changed")
            package_root = Path(attached["attachment"]["path"])
            package = validate_package(package_root)
            require(package.digest == attached["attachment"]["digest"] == prepared["expected_package_digest"]
                == receipt["attached_package_digest"] and package.manifest.profile == "host-command", "command_attachment_identity_changed")
            proof = parse_preparation_receipt((common.receipt_path(package)).read_bytes())
            require(proof.schema_version == 2 and proof.binding.revision == attached["revision"]
                and proof.binding.source_revision == attached["source"]["revision"]
                and proof.inputs.recipe.id == "mcpme.native-command" and proof.inputs.recipe.version == "1.0.2",
                "command_sealed_identity_changed")
            verification = prepared["verification"]
            require(verification["tools_checked"] == verification["fixtures_checked"] == 1
                and verification["package_digest"] == package.digest
                and verification["measurement_scope"] == "held-group-plus-helper-windowed-footprint-v1"
                and len(verification["command_resource_evidence"]) == 1, "command_measured_proof_missing")
            measurement = verification["command_resource_evidence"][0]["report"]
            require(measurement["sample_windows_valid"] is True and measurement["max_group_members"] == 3
                and measurement["max_scope_window_footprint_bytes"] > 8 * 1024**2
                and measurement["hard_aggregate_enforced"] is False and measurement["lifetime_coverage_complete"] is False,
                "command_measurement_scope_changed")
            self.source_check(source, original)
            plan = await self.cli(state, environment, home, "install", "plan", "--import", draft_id,
                "--revision", attached["revision"], "--json")
            expected_tools = [Tool.model_validate(item).mcp_schema() for item in plan["package"]["tools"]]
            require(expected_tools == [tool.mcp_schema() for tool in package.manifest.tools], "command_full_contract_changed")
            operation = await self.finish_operation(state, environment, home,
                await self.cli(state, environment, home, "install", "apply", "--plan", plan["plan_id"], "--json"))
            slot = operation["slot_id"]
            calls = [("run", {"ok": True}, {"ok": True})]
            http_calls = await self.sdk(state, environment, home, slot, expected_tools, calls, "http")
            stdio_calls = await self.sdk(state, environment, home, slot, expected_tools, calls, "stdio")
            relocated = base / "relocated sealed command"
            shutil.copytree(package_root, relocated)
            require(validate_package(relocated).digest == package.digest, "relocated_command_digest_changed")
            self.source_check(source, original, remove=True)
            await self.shutdown(child, state)
            child = await self.start(state, environment, home, enabled=True)
            resumed = await self.cli(state, environment, home, "imports", "inspect", draft_id, "--json")
            require(resumed["state"] == "installed" and resumed["result_slot_id"] == slot
                and resumed["attachment"]["digest"] == package.digest and not source.exists(), "command_restart_binding_changed")
            http_calls += await self.sdk(state, environment, home, slot, expected_tools, calls, "http")
            stdio_calls += await self.sdk(state, environment, home, slot, expected_tools, calls, "stdio")
            await self.shutdown(child, state)
            await self.cleanup()
            shutil.rmtree(state)
            relocation_base, relocation_state, relocation_home, relocation_env = self.new_case("relocation")
            child = await self.start(relocation_state, relocation_env, relocation_home, enabled=True)
            state = relocation_state
            plan = await self.cli(state, relocation_env, relocation_home, "install", "plan", "--package", relocated,
                "--start", "--start-policy", "host", "--json")
            operation = await self.finish_operation(state, relocation_env, relocation_home,
                await self.cli(state, relocation_env, relocation_home, "install", "apply", "--plan", plan["plan_id"], "--json"))
            relocated_slot = operation["slot_id"]
            shutil.rmtree(relocated)
            http_calls += await self.sdk(state, relocation_env, relocation_home, relocated_slot, expected_tools, calls, "http")
            stdio_calls += await self.sdk(state, relocation_env, relocation_home, relocated_slot, expected_tools, calls, "stdio")
            await self.shutdown(child, state)
            onboarding.stage("recipe_lifecycle", label)
            topology = base / "actual command topology"; topology.mkdir(mode=0o700)
            topology_states = [topology / name / "state" for name in ("echo", "tree", "fds", "escape", "cancel", "owner-kill", "group-kill")]
            self.states.extend(topology_states)
            _, inventory = command_host.verify_payload(self.host)
            controls = await command_host.verify(SimpleNamespace(host=self.host, workspace=topology, fixture=self.fixture),
                onboarding.verify_host_payload(self.host), inventory)
            require(controls["verified"] is True, "command_topology_controls_failed")
            lost = []
            for owned_state in topology_states:
                for path in (owned_state / "slots").glob("*/ownership.json"):
                    if onboarding.private_json(path) != {}:
                        self.lease["safe_to_remove"] = False
                        lost.append(owned_state.parent.name)
            require(lost == ["group-kill"], "unexpected_command_topology_ownership_retained")
            controls["deliberate_ancestor_group_loss"] = await self.retained_gateway_loss(topology / "group-kill/state", inventory)
            return {"passed": True, "native_directory_selection": True, "native_routes": sorted(set(receipt["routes"])),
                "native_receipt_sha256": digest(receipt_path), "source_files": len(original), "source_removed": True,
                "original_state_removed": True, "restart_invoked": True, "sealed_package_relocated": True,
                "package_digest": package.digest, "recipe_digest": receipt["recipe_digest"],
                "source_revision": receipt["source_revision"], "attached_revision": receipt["attached_revision"],
                "full_tool_contract_sha256": hashlib.sha256(json.dumps(expected_tools, sort_keys=True).encode()).hexdigest(),
                "tools_checked": 1, "http_calls": http_calls, "stdio_calls": stdio_calls,
                "measurement_scope": verification["measurement_scope"], "measurement": measurement, "command_topology": controls,
                "ordinary_native_install_ownership_settled": True,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
        finally:
            await self.shutdown(child, state)


async def verify(host, native, fixture, report):
    require(fixture.is_absolute() and fixture.resolve() == fixture and fixture.is_file()
        and stat.S_ISREG(fixture.lstat().st_mode), "synthetic_command_fixture_required")
    build = onboarding.verify_host_payload(host)
    _, inventory = command_host.verify_payload(host)
    resource = json.loads((host / "command-resource-execution.json").read_bytes())
    require(resource["helper"] == "bin/mcpme-command-resource-reap"
        and digest(host / resource["helper"]) == resource["helper_sha256"]
        and resource["production_admission"] == "disabled" and resource["configuration_authority"] == "inventory-only"
        and resource["hard_aggregate_enforced"] is False and resource["lifetime_coverage_complete"] is False,
        "command_resource_inventory_changed")
    before_native, before_fixture = digest(native), digest(fixture)
    factory = partial(CommandHarness, fixture=fixture)
    await onboarding.verify(host, native, report, harness_factory=factory, cases=("native_command",))
    require(onboarding.verify_host_payload(host) == build and digest(native) == before_native
        and digest(fixture) == before_fixture, "command_acceptance_inputs_mutated")
    report["driver_children_joined"] = report.pop("owned_children_joined")
    report["all_ownership_settled"] = False
    report["reconciliation_required"] = True
    report["unknown_legacy_child_joined"] = False
    report["limits"].append("deliberate_ancestor_group_loss_retains_unjoined_legacy_gateway_authority")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "native", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "development_native_installed_command",
        "cases": {}, "limits": ["physical_dialog_clicks_unverified", "signed_native_app_unverified",
            "hard_aggregate_memory_unverified", "complete_lifetime_measurement_unverified", "public_execution_disabled"]}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True), args.fixture.resolve(strict=True), report))
    except (Exception, KeyboardInterrupt) as error:
        failure, _ = onboarding.bounded_failure(error)
        report["verified"] = False
        report["diagnostic"] = {**getattr(failure, "context", getattr(failure, "acceptance_context", onboarding.DIAGNOSTIC)),
            "code": failure.code if isinstance(failure, onboarding.AcceptanceError) else "command_acceptance_failed",
            "type": type(failure).__name__ if type(failure).__name__ in {"KeyError", "ValueError", "TypeError",
                "AttributeError", "AcceptanceError", "TimeoutError", "ExceptionGroup", "FileNotFoundError", "OSError"} else "Exception"}
        if isinstance(failure, onboarding.AcceptanceError) and hasattr(failure, "native_stage"):
            report["diagnostic"]["native_stage"] = failure.native_stage
        if isinstance(failure, onboarding.AcceptanceError) and hasattr(failure, "native_observation"):
            report["native_observation"] = failure.native_observation
    try:
        onboarding.write_private_json(args.output.absolute(), report)
    except Exception:
        report["verified"] = False
    print(json.dumps({"verified": report["verified"], "scope": report["scope"], "diagnostic": report.get("diagnostic")}, sort_keys=True))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
