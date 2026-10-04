"""Check a public signed release with closed agent defaults and private A5 cases.

Default discovery uses the shipped CLI. The two generated-adapter cases retain
the reviewed private request-only provider/factory path; they do not enable or
test public agent execution. No model inference or live provider is requested.
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
import stat

import verify_phase3_native_agent as agent
import verify_phase3_public_native as public
import verify_phase3_public_native_command as command
import verify_phase3_public_textgen as textgen

common = agent.common
TEXTGEN_VERSION = "1.0.10"


def closed_agent_record(value):
    record = value.get("agent") if type(value) is dict else None
    common.require(type(record) is dict and set(record) == {"components", "providers", "unlocked_resolution"}
        and type(record["components"]) is list and record["components"] == []
        and type(record["providers"]) is list and record["providers"] == []
        and record["unlocked_resolution"] is False, "combined_public_agent_policy_open")


def closed_release_policy(value):
    # Called only after common.verify imports the relocated packaged backend.
    from mcpme.preparation_release_policy import ReleasePolicy
    closed_agent_record(value)
    try:
        policy = ReleasePolicy.model_validate(value)
        preparation, agent_admission, command_admission = policy.admissions()
    except ValueError:
        raise common.AcceptanceError("combined_release_policy_invalid") from None
    common.require(preparation is not None and agent_admission is None and command_admission is not None,
        "combined_release_policy_invalid")
    return policy


def combined_capabilities(value, engine, task, policy):
    from mcpme.preparation_textgen import ENGINE_ID, TASK_ID
    common.require(engine[:2] == (ENGINE_ID, TEXTGEN_VERSION)
        and task[:2] == (TASK_ID, TEXTGEN_VERSION), "combined_textgen_version_changed")
    identity = textgen.public_textgen_capabilities(value, engine, task)
    execution = value["preparation_execution"]
    families = {tuple(item) for item in execution.get("families", ())}
    closures = {frozenset(tuple(item) for item in group)
        for group in execution.get("recipe_component_sets", ())}
    common.require(command.COMPONENT in families and frozenset({command.COMPONENT}) in closures
        and value.get("package_schema_versions") == [1, 2]
        and value.get("profiles") == ["mcp-stdio", "mcp-http", "model-service", "host-command"],
        "combined_command_authority_unavailable")
    common.require(not any(key[0].startswith("agent-") for key in families)
        and not any(key[0].startswith("agent-") for group in closures for key in group),
        "combined_public_agent_admitted")
    common.require(families == {item.key for item in policy.preparation.families}
        and closures == {frozenset(item.key for item in group) for group in policy.preparation.recipe_component_sets}
        and set(value["preparation_resolution"]["candidate_kinds"]) == set(policy.preparation.resolution_candidate_kinds),
        "combined_release_policy_capabilities_changed")
    return identity


def textgen_keys():
    from mcpme.preparation_textgen import ENGINE_ID, TASK_ID, VERSION, references
    common.require(VERSION == TEXTGEN_VERSION, "combined_textgen_version_changed")
    keys = {item.id: item.key for item in references()}
    return keys[ENGINE_ID], keys[TASK_ID]


def input_file(path):
    common.require(path.is_absolute() and path.resolve(strict=True) == path,
        "combined_agent_source_path_invalid")
    before = command._identity(path)
    common.require(stat.S_ISREG(before[2]) and before[3] == os.getuid() and before[4] == 1
        and not before[2] & 0o6022 and before[5] <= 512 * 1024, "combined_agent_source_file_invalid")
    digest = common.digest(path)
    common.require(command._identity(path) == before, "combined_agent_source_changed")
    return before, digest


class PublicReleaseAgentHarness(agent.Harness):
    def __init__(self, *args, report):
        super().__init__(*args)
        self.combined_report = report

    async def start(self, state, environment, home, *, enabled):
        field = "private_embedding_starts" if enabled else "ordinary_public_cli_starts"
        self.combined_report[field] = self.combined_report.get(field, 0) + 1
        if enabled:
            return await super().start(state, environment, home, enabled=True)
        return await common.Harness.start(self, state, environment, home, enabled=False)

    async def defaults(self):
        common.stage("production_defaults", "defaults")
        _, policy_value = command._json(self.host / command.POLICY_PATH)
        policy = closed_release_policy(policy_value)
        base, state, home, environment = self.new_case("combined-agent-closed-defaults")
        child = await self.start(state, environment, home, enabled=False)
        try:
            identity = combined_capabilities(
                await self.cli(state, environment, home, "capabilities", "--json"), *textgen_keys(), policy)
            providers = await self.get(state, "/v1/agent-providers")
            common.require(providers["providers"] == [] and providers["live_execution_enabled"] is False,
                "combined_public_agent_enabled")
            common.require((await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"] == []
                and (await self.cli(state, environment, home, "resolutions", "list", "--json"))["resolutions"] == [],
                "combined_public_default_job_created")
            counts = agent.job_counts(state)
            common.require(set(counts) == {"preparation_jobs", "resolution_jobs", "agent_host_jobs", "attempts"}
                and all(type(value) is int and value == 0 for value in counts.values()),
                "combined_public_agent_job_created")
            self.combined_report["public_defaults"] = {"passed": True, "admission_digest": identity,
                "ordinary_cli_launcher": True, "execution_factory_injected": False,
                "typed_release_policy_validated": True, "closed_agent_record_validated": True,
                "python_mcp_onnx_cli_textgen_admitted": True, "provider_registry_empty": True,
                "live_agent_enabled": False, "job_counts": counts, "review_created_jobs": False}
            return True
        finally:
            await self.shutdown(child, state)

    async def case(self, label):
        result = await super().case(label)
        common.require(result["passed"] is True and result["simulated_provider"] is True
            and result["provider_calls"] == 4 and result["live_provider_calls"] == 0
            and result["http_calls"] == result["stdio_calls"] == 2
            and all(result[key] is True for key in ("pending_proposal_handoff", "source_removed",
                "captured_source_removed", "restart_invoked", "relocated_confined_probe")),
            "combined_private_agent_case_incomplete")
        result.update(execution_authority="trusted_private_embedding", execution_factory_injected=True,
            public_agent_execution_verified=False, provider_transport="request_derived_httpx_mock_transport",
            captured_state_retained_until_owned_cleanup=True)
        return result


async def verify(host_path, native_path, report):
    common._host_paths(host_path)
    build = common.verify_host_payload(host_path)
    common.require(native_path.is_absolute() and native_path.resolve() == native_path
        and native_path.is_file() and os.access(native_path, os.X_OK), "native_acceptance_binary_required")
    app = native_path.parents[2]
    common.require(app.suffix == ".app" and native_path.parent == app / "Contents/MacOS"
        and host_path == app / "Contents/Resources/host", "combined_signed_app_required")
    policy_bytes, policy = command._json(host_path / command.POLICY_PATH)
    closed_agent_record(policy)
    command.signature(app, deep=True)
    common.verify_native_app_signature(native_path)
    original_app = command.snapshot(app)
    source_paths = (Path(agent.__file__).absolute(), Path(agent.__file__).absolute().with_name("agent_acceptance_fixture.py"),
        Path(__file__).absolute())
    original_sources = {path: input_file(path) for path in source_paths}
    report.update(host_source_commit=build["source_commit"], host_build_sha256=common.digest(host_path / "build.json"),
        release_policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        original_signed_app_files=sum(item[1] is not None for item in original_app.values()),
        request_fixture_sha256=original_sources[source_paths[1]][1], simulated_provider=True,
        live_provider_calls=0, public_agent_execution_verified=False, model_inference_calls=0,
        private_agent_case_authority="trusted_private_embedding")
    completed = False
    try:
        await common.verify(host_path, native_path, report,
            harness_factory=partial(PublicReleaseAgentHarness, report=report), cases=agent.CASES)
        completed = report["verified"]
    finally:
        report["verified"] = False
        if "production_defaults_disabled" in report:
            report["public_release_defaults_and_closed_agent_verified"] = report.pop("production_defaults_disabled")
        report["original_signed_app_preserved"] = command.snapshot(app) == original_app
        report["original_agent_fixture_and_driver_preserved"] = all(input_file(path) == expected
            for path, expected in original_sources.items())
        common.require(report["original_signed_app_preserved"] and report["original_agent_fixture_and_driver_preserved"],
            "combined_agent_original_input_changed")
        command.signature(app, deep=True)
        common.verify_native_app_signature(native_path)
    report["simulated_provider_calls"] = sum(item["provider_calls"] for item in report["cases"].values())
    report["verified"] = (completed and report.get("public_defaults", {}).get("passed") is True
        and report["owned_children_joined"] is True and set(report["cases"]) == set(agent.CASES)
        and report["simulated_provider_calls"] == 8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "native", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        print(json.dumps({"verified": False, "code": "acceptance_report_output_exists"}))
        return 1
    os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "combined_public_defaults_private_native_agent",
        "cases": {}, "simulated_provider": True, "live_provider_calls": 0, "model_inference_calls": 0,
        "public_agent_execution_verified": False, "a5_scope_complete": False,
        "limits": ["agent_cases_use_trusted_private_embedding", "live_provider_acceptance_unverified",
            "public_agent_execution_unverified", "physical_dialog_interaction_unverified",
            "hard_aggregate_live_memory_unverified", "complete_lifetime_accounting_unverified",
            "ordinary_family_inference_and_execution_require_separate_public_acceptance"]}
    try:
        asyncio.run(verify(args.host.absolute(), args.native.absolute(), report))
    except (Exception, KeyboardInterrupt) as error:
        error, _ = common.bounded_failure(error)
        report["verified"] = False
        report["diagnostic"] = {"stage": getattr(error, "native_stage",
            getattr(error, "acceptance_context", common.DIAGNOSTIC)["stage"]),
            "code": error.code if isinstance(error, common.AcceptanceError) else "acceptance_failed"}
        if isinstance(error, common.AcceptanceError) and hasattr(error, "native_observation"):
            report["native_observation"] = error.native_observation
    common.write_private_json(args.output.absolute(), report)
    print(json.dumps({"verified": report["verified"], "cases_passed": sum(item.get("passed") is True
        for item in report["cases"].values()), "simulated_provider_calls": report.get("simulated_provider_calls", 0),
        "live_provider_calls": 0, "model_inference_calls": 0, "public_agent_execution_verified": False}))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
