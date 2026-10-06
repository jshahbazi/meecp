#!/usr/bin/env python3
"""Explicit A5 live candidate acceptance. Default: prepare a review, no calls.

--run-live requires an operator-selected private credential file, exact model,
and an already signed candidate admitting them. Never discovers credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import time
import tomllib
import uuid

import verify_phase3_native_onboarding as common
import verify_phase3_native_agent as native_common
import verify_phase3_public_native as public_native
import verify_phase3_public_native_command as public_command

app_inventory = public_native.app_inventory
signature = public_command.signature

ROOT = Path(__file__).resolve().parents[1]
UNLOCKED = {"id": "agent-unlocked-python-adapter-v1", "version": "1.0.3",
    "digest": "a34b6de0ced6964bbe51ff8f049863216b51f815b4ea1a5deb4b619403262aed"}
LOCKED = {"id": "agent-python-adapter-v1", "version": "1.0.3",
    "digest": "91f64be7b083517881ff0d9a91af1389f7cc151fc2adb49abbf6e217bc904fec"}
LIMITS = {"max_calls": 4, "max_parallel": 3, "max_transfer_bytes": 524288,
    "max_output_tokens": 4096, "wall_seconds": 180, "max_artifact_bytes": 131072}
RECIPE = "mcpme.agent-unlocked-python-adapter"
ENDPOINT = "https://api.openai.com/v1/responses"
NATIVE_STAGES = native_common.NATIVE_STAGES
LIVE_ERRORS = frozenset({"agent_provider_rejected", "agent_provider_failed", "agent_provider_incomplete",
    "agent_provider_response_limit", "agent_provider_output_invalid", "agent_proposal_invalid", "agent_deadline",
    "agent_credentials_unavailable", "agent_provider_configuration_stale", "agent_source_identity_mismatch"})
NATIVE_FAILURE_CODES = LIVE_ERRORS | frozenset({"native_live_agent_acceptance_failed",
    "unsupported_preparation_form",
    "agent_pending_proposal_unavailable", "agent_generated_form_unavailable", "agent_generated_form_invalid",
    "preparation_form_invalid", "preparation_form_unavailable", "preparation_selection_missing",
    "preparation_recipe_unavailable", "agent_candidate_requires_review", "manager_request_failed"})
HANDOFF_POSTCONDITIONS = frozenset({"proposal_present", "proposal_succeeded", "proposal_settled",
    "requests_complete", "agent_idle", "ordinary_idle", "agent_error_absent", "ordinary_error_absent",
    "form_present", "form_import_matches", "form_revision_matches", "form_source_matches",
    "resolution_required", "resolution_result_absent", "preparation_job_absent", "resolution_job_absent"})
JOB_STATES = frozenset({"queued", "snapshotting", "analyzing", "validating", "cancel_requested",
    "adopting", "adopted", "succeeded", "failed", "canceled", "interrupted"})


def native_failure_metadata(value):
    """Project only finite acceptance diagnostics; never copy native payloads."""
    if not isinstance(value, dict):
        return {"receipt_available": False}
    code = value.get("error_code")
    stage = value.get("stage")
    result = {"receipt_available": True, "native_error_present": "error_code" in value,
        "native_error_allowed": isinstance(code, str) and code in NATIVE_FAILURE_CODES,
        "native_error_code": code if isinstance(code, str) and code in NATIVE_FAILURE_CODES else "native_live_agent_acceptance_failed",
        "native_stage": stage if isinstance(stage, str) and stage in NATIVE_STAGES else "unknown"}
    postconditions = value.get("handoff_postconditions")
    if (isinstance(postconditions, dict) and set(postconditions) == HANDOFF_POSTCONDITIONS
            and all(type(item) is bool for item in postconditions.values())):
        result["handoff_postconditions"] = dict(postconditions)
    else:
        result["handoff_postconditions_available"] = False
    routes = value.get("routes")
    if isinstance(routes, list) and len(routes) <= 512 and all(isinstance(item, str) and len(item) <= 256 for item in routes):
        result["native_route_count"] = len(routes)
        result["agent_start_requested"] = "POST v1/agent-preparations/start" in routes
        result["form_requested"] = "POST v1/preparations/form" in routes
        result["resolution_start_requested"] = "POST v1/resolutions/start" in routes
        result["preparation_start_requested"] = "POST v1/preparations/start" in routes
    return result


def agent_call_metadata(job, draft, job_id):
    """Account durable reservations separately from remote provider execution."""
    unknown = {"known": False, "remote_calls_after_cancel": "unknown"}
    if (not isinstance(draft, dict) or not isinstance(draft.get("source"), dict)
            or not isinstance(draft.get("draft_id"), str)
            or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", draft["draft_id"]) is None
            or not isinstance(job_id, str)
            or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", job_id) is None
            or not isinstance(draft["source"].get("revision"), str)
            or re.fullmatch(r"(?:sha256:[0-9a-f]{64}|[0-9a-f]{40})", draft["source"]["revision"]) is None
            or type(draft.get("revision")) is not int or draft["revision"] < 1 or not isinstance(job, dict)
            or job.get("job_id") != job_id or job.get("import_id") != draft.get("draft_id")
            or type(job.get("revision")) is not int
            or job.get("revision") != draft["revision"] or job.get("source_revision") != draft["source"].get("revision")
            or not isinstance(job.get("state"), str) or job["state"] not in JOB_STATES
            or type(job.get("local_io_settled")) is not bool):
        return unknown
    calls = job.get("calls")
    if not isinstance(calls, list) or len(calls) > LIMITS["max_calls"]:
        return unknown
    roles = set()
    for call in calls:
        if (not isinstance(call, dict) or set(call) != {"role", "transfer_bytes", "reserved_output_tokens", "usage"}
                or not isinstance(call["role"], str) or call["role"] not in {"interface", "dependencies", "fixtures", "adapter"}
                or call["role"] in roles or type(call["transfer_bytes"]) is not int
                or not 0 <= call["transfer_bytes"] <= LIMITS["max_transfer_bytes"]
                or type(call["reserved_output_tokens"]) is not int or call["reserved_output_tokens"] != LIMITS["max_output_tokens"]):
            return unknown
        roles.add(call["role"])
        usage = call["usage"]
        if usage is not None and (not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}
                or any(type(value) is not int or not 0 <= value <= 10**9 for value in usage.values())):
            return unknown
    if sum(call["transfer_bytes"] for call in calls) > LIMITS["max_transfer_bytes"]:
        return unknown
    return {"known": True, "state": job["state"], "local_io_settled": job["local_io_settled"],
        "reserved_calls": len(calls), "usage_recorded_calls": sum(call.get("usage") is not None for call in calls),
        "remote_calls_after_cancel": "unknown"}


def active_candidate(policy, model, adapter_digest):
    from mcpme.preparation_release_policy import ReleasePolicy
    selected = ReleasePolicy.model_validate(policy)
    _, agent, _ = selected.admissions()
    # This harness still exercises the unlocked scalar case. Current releases
    # also admit the exact locked family; that is not new live-case evidence.
    common.require(agent is not None and agent.components in {
        frozenset({tuple(UNLOCKED.values())}), frozenset({tuple(UNLOCKED.values()), tuple(LOCKED.values())})}
        and agent.providers == frozenset({("openai-responses-v1", adapter_digest, model)})
        and agent.unlocked_resolution, "live_agent_candidate_inactive")
    return selected


def read_selected_credential(path, *, on_read_attempt=None, on_read_complete=None):
    """Only explicit operator input, after candidate/ordinary startup checks."""
    common.require(path.is_absolute() and path.resolve(strict=True) == path, "live_agent_credential_file_invalid")
    before = path.lstat()
    common.require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o600
        and before.st_uid == os.getuid() and before.st_nlink == 1 and 1 <= before.st_size <= 4098,
        "live_agent_credential_file_invalid")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        common.require(credential_identity(os.fstat(fd)) == credential_identity(before), "live_agent_credential_file_changed")
        if on_read_attempt is not None:
            on_read_attempt()
        value = os.read(fd, 4099)
        if on_read_complete is not None:
            on_read_complete()
        after = os.fstat(fd)
        common.require(credential_identity(after) == credential_identity(before)
            and credential_identity(path.lstat()) == credential_identity(before) and len(value) == before.st_size,
            "live_agent_credential_file_changed")
    finally:
        os.close(fd)
    # Permit one conventional file terminator, retaining raw identity/size above.
    value = value[:-2] if value.endswith(b"\r\n") else value[:-1] if value.endswith(b"\n") else value
    common.require(1 <= len(value) <= 4096 and all(33 <= byte <= 126 for byte in value),
        "live_agent_credential_file_invalid")
    return value.decode("ascii"), before


def credential_identity(info):
    # Atime may advance during the deliberate read. No key-derived digest.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def live_fixture(base, state, draft_id, recipe, provider, model):
    nonce = str(uuid.uuid4())
    output, fixture = base / "native-live-agent-receipt.json", base / "native-live-agent-case.json"
    common.write_private_json(state / ("native-agent-live-" + nonce + ".json"), {
        "schema_version": 1, "fixture_nonce": nonce, "expires_at": int(time.time()) + 295,
        "manager_file": native_common.file_identity(state / "manager-connection.json")})
    common.write_private_json(fixture, {"schema_version": 1, "fixture_nonce": nonce,
        "state_dir": str(state), "draft_id": draft_id, "output_path": str(output), "answers_by_label": {},
        "agent_scope": {"provider_id": provider["provider_id"], "provider_generation": provider["generation"],
            "model": model, "recipe": recipe, "selected_paths": ["worker.py"], "selected_metadata_paths": ["LICENSE"]}})
    return fixture, output


class LiveHarness(native_common.Harness):
    def __init__(self, *args, model, credential_file, report):
        super().__init__(*args)
        self.model, self.credential_file, self.report = model, credential_file, report

    async def start(self, state, environment, home, *, enabled):
        common.require(enabled is False, "live_agent_factory_forbidden")
        return await common.Harness.start(self, state, environment, home, enabled=False)

    async def configure(self, state):
        providers = await self.get(state, "/v1/agent-providers")
        common.require(providers["providers"] == [] and providers["live_execution_enabled"] is True
            and all(count == 0 for count in native_common.job_counts(state).values()), "live_agent_defaults_invalid")
        key, identity = read_selected_credential(self.credential_file,
            on_read_attempt=lambda: self.report.update(credential_read_attempted=True),
            on_read_complete=lambda: self.report.update(credential_read=True))
        self.report["credential_accepted"] = True
        try:
            response = await self.manager(state, "/v1/agent-providers", {"schema_version": 1,
                "adapter_id": "openai-responses-v1", "name": "Explicit live acceptance",
                "models": [self.model], "configuration": {}, "credentials": {"api_key": key}})
        finally:
            key = None
        common.require(credential_identity(self.credential_file.lstat()) == credential_identity(identity),
            "live_agent_credential_file_changed")
        provider = response["provider"]
        common.require(provider["execution_available"] is True and provider["models"] == [self.model]
            and provider["destination"] == ENDPOINT and provider["credential_status"] == "configured",
            "live_agent_configuration_invalid")
        return provider

    async def observe_failure(self, state, draft, native):
        observation = native_failure_metadata(native)
        observation["call_accounting"] = {"known": False, "remote_calls_after_cancel": "unknown"}
        try:
            counts = native_common.job_counts(state)
            if (set(counts) <= {"preparation_jobs", "resolution_jobs", "agent_host_jobs", "attempts"}
                    and all(type(count) is int and 0 <= count <= 1024 for count in counts.values())):
                observation["journal_counts"] = counts
        except Exception:
            observation["journal_counts_available"] = False
        job_id = native.get("agent_job_id") if isinstance(native, dict) else None
        if (isinstance(job_id, str) and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", job_id)):
            try:
                job = await asyncio.wait_for(self.get(state, "/v1/agent-preparations/jobs/" + job_id), 5)
                observation["call_accounting"] = agent_call_metadata(job, draft, job_id)
            except BaseException:
                # This is a read-only diagnostic. Retain the original failure,
                # including cancellation, and never trigger another attempt.
                observation["call_accounting_available"] = False
        self.report["live_failure_observation"] = observation


    async def case(self, label):
        common.require(label == "scalar", "live_agent_case_not_admitted")
        from mcpme.contracts import Tool, validate_package
        from mcpme.preparation_agent_adapter_store import CODE_PATH
        from mcpme.preparation_agent_unlocked_contracts import DEPENDENCY_NAMESPACE
        from mcpme.preparation_inputs import parse_preparation_descriptor, parse_preparation_receipt
        from mcpme.preparation_verifier import verify_prepared_package
        from mcpme.processes import ProcessOwner

        common.stage("source_intake")
        started = time.monotonic()
        base, state, home, environment = self.new_case(label)
        source = base / "unfamiliar source"; source.mkdir(mode=0o700)
        content = "def double(n):\n return n*2\n"
        (source / "worker.py").write_text(content)
        (source / "LICENSE").write_text("Synthetic original-code permission.\n")
        (source / "unknown.payload").write_bytes(b"\0\xffsynthetic\n")
        original = {path.name: common.digest(path) for path in source.iterdir()}
        child = await self.start(state, environment, home, enabled=False)
        draft, native = None, None
        try:
            provider = await self.configure(state)
            draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                "--name", "Live generated adapter acceptance", "--intent", "Expose worker.double as tool double, input n of type integer, returning the structured object with integer field value equal to double(n). Use a typed wrapper. This source is a nonsecret acceptance fixture.",
                "--source-mode", "automatic", "--start", "--start-policy", "host", "--json")
            common.require({row["path"]: row["sha256"] for row in draft["source"]["files"]} == original,
                "automatic_intake_dropped_original")
            inspected = await self.cli(state, environment, home, "preparations", "inspect-source", "--import",
                draft["draft_id"], "--revision", draft["revision"], "--json")
            common.require(not inspected["candidates"], "fixture_has_deterministic_candidate")
            options = await self.manager(state, "/v1/agent-preparations/plans",
                {"import_id": draft["draft_id"], "revision": draft["revision"]})
            recipes = [row for row in options["recipes"] if row["id"] == RECIPE]
            common.require(len(recipes) == 1 and any(row["provider_id"] == provider["provider_id"] and row["generation"] == provider["generation"] and row["simulated"] is False for row in options["providers"]),
                "live_agent_options_missing")
            recipe = recipes[0]
            fixture, output = live_fixture(base, state, draft["draft_id"], recipe, provider, self.model)
            common.stage("native")
            self.report["live_dispatch_attempted"] = True
            try:
                code, _ = await self.run([str(self.native), "--agent-live-native-acceptance", str(fixture)], environment, home, timeout=360, capture=True)
            except common.AcceptanceError as error:
                try:
                    error.native_observation = native_common.job_counts(state)
                except BaseException:
                    pass  # Optional diagnostics cannot replace the native failure.
                raise
            common.require(output.is_file(), "native_agent_receipt_missing")
            native = common.private_json(output, 262144)
            if code or native.get("passed") is not True:
                error = common.AcceptanceError(native_failure_metadata(native)["native_error_code"])
                error.native_stage = native_failure_metadata(native)["native_stage"]
                raise error
            common.require(native["stage"] == "attached" and native["draft_id"] == draft["draft_id"]
                and native["source_revision"] == draft["source"]["revision"]
                and native["agent_revision"] == draft["revision"]
                and native["pending_proposal_handoff"] is True and native["simulated_provider"] is False and native["provider_generation"] == provider["generation"] and native["live_transport_selected"] is True,
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
            child = await self.start(state, environment, home, enabled=False)
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
            return {"passed": True, "simulated_provider": False, "live_provider_calls": len(agent["calls"]),
                "provider_calls": len(agent["calls"]), "provider_generation": provider["generation"], "native_receipt_sha256": common.digest(output), "native_routes": routes,
                "proposal_digest": native["proposal_digest"], "recipe": recipe, "package_digest": package.digest,
                "source_revision": draft["source"]["revision"], "original_file_sha256": original,
                "reviewed_revision": attached["revision"], "pending_proposal_handoff": True,
                "resolution_progress": resolved["progress"], "preparation_progress": prepared["progress"],
                "resolution_producer": resolved["request"]["producer"], "resolution_policy": resolved["request"]["policy"],
                "http_calls": http_calls, "stdio_calls": stdio_calls, "source_removed": True,
                "captured_source_removed": True, "restart_invoked": True, "relocated_confined_probe": True,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
        except BaseException as error:
            if draft is not None:
                try:
                    await self.observe_failure(state, draft, native)
                except BaseException:
                    # A secondary observation can never replace the original
                    # acceptance failure or cancellation.
                    self.report["live_failure_observation"] = {"observation_available": False,
                        "call_accounting": {"known": False, "remote_calls_after_cancel": "unknown"}}
            if not isinstance(error, common.AcceptanceError):
                error.acceptance_context = dict(common.DIAGNOSTIC)
            raise
        finally:
            await self.shutdown(child, state)


def prepare_review(policy, model):
    """Review a closed-base proposal or the exact already-admitted selection.

    No credential argument exists: even an explicitly selected credential is not
    needed until the separately reviewed, signed live executor is available.
    """
    from mcpme.preparation_agent_contracts import AgentLimitsV1
    from mcpme.preparation_agent_guides import builtin_analysis_guides
    from mcpme.preparation_agent_providers import OpenAIResponsesAdapter
    from mcpme.preparation_release_policy import ReleasePolicy

    if not isinstance(model, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model) is None:
        raise ValueError("live_agent_model_required")
    parsed = ReleasePolicy.model_validate(policy)
    parsed.admissions()
    if sum(guide.component.model_dump() == UNLOCKED for guide in builtin_analysis_guides()) != 1:
        raise ValueError("live_agent_guide_mismatch")
    adapter = OpenAIResponsesAdapter()
    candidate = copy.deepcopy(policy)
    closed = parsed.agent.model_dump() == {"components": [], "providers": [], "unlocked_resolution": False}
    if closed:
        candidate["policy_id"] = "phase3-live-agent-acceptance-candidate-v1"
        preparation = candidate["preparation"]
        if UNLOCKED not in preparation["families"]:
            preparation["families"].append(copy.deepcopy(UNLOCKED))
        if [UNLOCKED] not in preparation["recipe_component_sets"]:
            preparation["recipe_component_sets"].append([copy.deepcopy(UNLOCKED)])
        candidate["agent"] = {"components": [copy.deepcopy(UNLOCKED)], "providers": [{
            "adapter_id": adapter.adapter_id, "adapter_digest": adapter.digest, "models": [model]}],
            "unlocked_resolution": True}
    else:
        try:
            active_candidate(candidate, model, adapter.digest)
        except common.AcceptanceError:
            raise ValueError("live_agent_existing_authority_mismatch") from None
    validated = ReleasePolicy.model_validate(candidate)
    _, admission, _ = validated.admissions()
    if not admission.permits(adapter.adapter_id, adapter.digest, model):
        raise ValueError("live_agent_candidate_invalid")
    AgentLimitsV1.model_validate(LIMITS)
    return {"schema_version": 1, "scope": "live_agent_review_preparation_only",
        "dispatch_authorized": False, "live_provider_calls": 0, "credential_reads": 0,
        "live_acceptance_verified": False, "candidate_policy": candidate,
        "candidate_policy_changed": candidate != policy,
        "destination": adapter.destination, "limits": dict(LIMITS),
        "maximum_reserved_output_tokens": LIMITS["max_calls"] * LIMITS["max_output_tokens"],
        "required_before_dispatch": ["explicit_account_credential_selection", "reviewed_live_native_driver",
            "independent_candidate_policy_review", "matching_signed_candidate_build", "exclusive_installed_acceptance_window"],
        "scope_limits": ["no_credential_or_connection_discovery", "live_execution_unverified",
            "no_policy_activation", "no_public_release_acceptance", "no_physical_click_evidence"]}


async def verify_live(host_path, native_path, model, credential_file, report):
    common._host_paths(host_path)
    build = common.verify_host_payload(host_path)
    app = common.native_app_root(native_path)
    common.require(app is not None and host_path == app / "Contents/Resources/host", "live_agent_signed_pair_required")
    signature(app, deep=True)
    before = app_inventory(app)
    script_paths = [Path(__file__), Path(common.__file__), Path(native_common.__file__),
        Path(public_native.__file__), Path(public_command.__file__)]
    scripts = {path.name: common.digest(path) for path in script_paths}
    # Reject old binaries before invoking an unknown flag that could open the UI.
    common.require(native_path.stat().st_size <= 64 * 1024**2
        and b"agent_live_acceptance_preflight_v1" in native_path.read_bytes(), "live_agent_native_driver_missing")
    try:
        with common.isolated_workspace(report) as (root, lease):
            host, native = common.relocate_host_and_native(root, host_path, native_path, report)
            common.verify_host_payload(host)
            common._host_imports(host)
            common._check_module_origins(host)
            from mcpme.preparation_contracts import load_contract
            from mcpme.preparation_agent_providers import OpenAIResponsesAdapter
            from mcpme.preparation_agent_backend import MAX_RESPONSE_BYTES
            common.require(MAX_RESPONSE_BYTES == 524288, "live_agent_response_limit_changed")
            policy_data = (host / "templates/release-policies/phase3-v1.json").read_bytes()
            active_candidate(load_contract(policy_data), model, OpenAIResponsesAdapter().digest)
            harness = LiveHarness(root, host, native, lease, model=model, credential_file=credential_file, report=report)
            try:
                _, _, home, environment = harness.new_case("live-preflight")
                code, output = await harness.run([str(native), "--agent-live-acceptance-preflight"],
                    environment, home, timeout=10, capture=True)
                common.require(code == 0 and output == b"agent_live_acceptance_preflight_v1\n", "live_agent_native_preflight_failed")
                report.update(host_source_commit=build["source_commit"],
                    host_artifacts_checked=len(build["artifact_sha256"]), input_app_files_checked=len(before),
                    policy_sha256=hashlib.sha256(policy_data).hexdigest(), native_binary_sha256=common.digest(native),
                    ordinary_host_launcher=True, execution_factory_injected=False,
                    maximum_response_bytes=MAX_RESPONSE_BYTES, limits=dict(LIMITS))
                report["cases"]["scalar"] = await harness.case("scalar")
                common._check_module_origins(host)
            finally:
                await harness.cleanup()
                report["owned_children_joined"] = not harness.children
            common.require(app_inventory(common.native_app_root(native)) == before, "live_agent_relocated_app_changed")
            signature(common.native_app_root(native), deep=True)
            report["relocated_app_unchanged"] = True
    finally:
        report["driver_sources_unchanged"] = scripts == {path.name: common.digest(path) for path in script_paths}
        report["driver_source_sha256"] = scripts
        report["input_app_unchanged"] = before == app_inventory(app)
        signature(app, deep=True)
        common.require(report["driver_sources_unchanged"], "live_agent_driver_changed")
    report["verified"] = (report["cases"].get("scalar", {}).get("passed") is True
        and report.get("owned_children_joined") is True and report["input_app_unchanged"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-live", action="store_true")
    for name in ("host", "native", "credential-file"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.run_live:
        if any(value is None for value in (args.host, args.native, args.credential_file)):
            parser.error("live execution requires an explicit host, native binary and credential file")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", args.model) is None:
            parser.error("live execution requires an explicit model ID")
        # Reserve the receipt before doing anything that could dispatch. A lost
        # result never justifies silently replaying a provider attempt.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.umask(0o077); logging.disable(logging.CRITICAL)
        report = {"schema_version": 1, "verified": False, "scope": "signed_candidate_live_agent_acceptance",
            "cases": {}, "credential_read_attempted": False, "credential_read": False,
            "credential_accepted": False, "live_dispatch_attempted": False,
            "simulated_provider": False, "automatic_retries": 0, "public_release_accepted": False,
            "scope_limits": ["explicit_candidate_policy_only", "physical_clicks_unverified",
                "provider_remote_work_settlement_unclaimed", "model_quality_unclaimed"]}
        try:
            asyncio.run(verify_live(args.host.resolve(strict=True), args.native.resolve(strict=True),
                args.model, args.credential_file, report))
        except (Exception, KeyboardInterrupt) as error:
            error, _ = common.bounded_failure(error)
            code = error.code if isinstance(error, common.AcceptanceError) else "live_agent_acceptance_failed"
            report["diagnostic"] = {"code": code if code.startswith(("live_agent_", "native_agent_", "acceptance_"))
                or code in NATIVE_FAILURE_CODES else "live_agent_acceptance_failed",
                "stage": getattr(error, "native_stage", "unknown") if getattr(error, "native_stage", "unknown") in NATIVE_STAGES else "unknown"}
        finally:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(report, indent=2, sort_keys=True).encode() + b"\n")
                stream.flush(); os.fsync(stream.fileno())
        print("live_agent_acceptance_passed" if report["verified"] else "live_agent_acceptance_failed")
        raise SystemExit(0 if report["verified"] else 1)
    if any(value is not None for value in (args.host, args.native, args.credential_file)):
        parser.error("artifact and credential inputs require explicit --run-live")
    # Only fixed source-owned policy input; no source/root/endpoint selector.
    policy_path = ROOT / "templates/release-policies/phase3-v1.json"
    data = policy_path.read_bytes()
    if len(data) > 65536:
        raise SystemExit("live_agent_policy_limit")
    # Source review is intentionally separate from live bundled imports. The
    # ordinary script entry point has no pytest backend path configuration.
    # This fixed path is never added on the --run-live branch above.
    previous_path = list(sys.path)
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from mcpme.preparation_contracts import load_contract
        report = prepare_review(load_contract(data), args.model)
    except Exception:
        raise SystemExit("live_agent_review_rejected") from None
    finally:
        sys.path[:] = previous_path
    report["base_policy_sha256"] = hashlib.sha256(data).hexdigest()
    payload = json.dumps(report, indent=2, sort_keys=True).encode() + b"\n"
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    print("live_agent_review_prepared_no_dispatch")


if __name__ == "__main__":
    main()
