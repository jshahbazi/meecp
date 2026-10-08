"""Public signed service onboarding using a synthetic authenticated localhost peer.

The selected shipped policy is the only preparation authority. This harness
never injects factories, builds an adapter in advance, or calls a real model.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

import verify_phase3_native_onboarding as common
from verify_phase3_public_native import app_inventory
from verify_phase3_public_native_command import _json, signature

require = common.require
REFERENCES = (
    ("openai-chat-completions-v1", "1.0.0", "01d9379a0ed5fdd9fc86e1b6aeb47465f153162af57157cfbd42f6644f338587"),
    ("loopback-http-bearer-v1", "1.0.0", "2f93a9f9258fa5d2b6b9c854f9e828d2f43ceeb7f0108183759e9f428fbaa4dc"),
    ("service-text-generate-v1", "1.0.0", "bc540561a87cba329d1d62d837596d6318a2d740fc4f6cf49b3eab95a9145d75"),
)
RECIPE = ("mcpme.local-openai-text", "1.0.0", "579defab9a646d37f07eb92f2e6a2260ad1923477f1cadb96c46fc9edf6d28c3")
POLICY_PATH = "templates/release-policies/phase3-v1.json"
NATIVE_STAGES = common.NATIVE_STAGES | {"service_selection", "service_review_only"}
NATIVE_ERRORS = frozenset({"service_native_context_stale", "native_acceptance_context_stale",
    "service_probe_failed", "service_probe_authority_mismatch", "service_secret_unavailable",
    "preparation_failed", "preparation_probe_failed", "preparation_deadline", "preparation_resources_unavailable"})


def active_policy(host):
    data, value = _json(host / POLICY_PATH)
    require(set(value) == {"schema_version", "policy_id", "preparation", "command", "agent"}
        and type(value["schema_version"]) is int and value["schema_version"] == 1,
        "public_service_policy_invalid")
    preparation = value["preparation"]
    refs = [dict(zip(("id", "version", "digest"), item)) for item in REFERENCES]
    require(type(preparation) is dict and set(preparation) == {
        "families", "recipe_component_sets", "resolution_candidate_kinds"}
        and refs[-1] in preparation["families"]
        and any(type(group) is list and len(group) == 3 and all(item in group for item in refs)
                for group in preparation["recipe_component_sets"]), "public_service_release_inactive")
    return hashlib.sha256(data).hexdigest()


def capabilities(value):
    service = value.get("service_intake", {})
    execution = value.get("preparation_execution", {})
    require(value.get("automatic_preparation") is True and value.get("recipe_execution") is True
        and "service_connection" in value.get("import_sources", [])
        and service.get("execution") is True and service.get("remote_endpoints") is False
        and service.get("upstream_process_owned") is False and service.get("model_bytes_attested") is False
        and list(REFERENCES[-1]) in execution.get("families", [])
        and any({tuple(item) for item in group} == set(REFERENCES)
                for group in execution.get("recipe_component_sets", [])), "public_service_capability_inactive")
    return execution["admission_digest"]


def native_diagnostic(receipt):
    # Persist only fixed stages/codes and explicit booleans, never native payloads.
    return {"stage": receipt.get("stage") if receipt.get("stage") in NATIVE_STAGES else "unknown",
        "error_code": receipt.get("error_code") if receipt.get("error_code") in NATIVE_ERRORS else None,
        "passed": receipt.get("passed") is True,
        "native_service_selection": receipt.get("native_service_selection") is True,
        "review_created_no_jobs": receipt.get("review_created_no_jobs") is True}


def file_identity(path):
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size,
        "mode": stat.S_IMODE(info.st_mode), "modified_seconds": info.st_mtime_ns // 10**9,
        "modified_nanoseconds": info.st_mtime_ns % 10**9, "changed_seconds": info.st_ctime_ns // 10**9,
        "changed_nanoseconds": info.st_ctime_ns % 10**9}


@asynccontextmanager
async def protocol_fixture():
    tasks, failures = set(), []
    counters = {"authenticated_requests": 0}
    credential = "synthetic-public-service-credential"
    async def handle(reader, writer):
        current = asyncio.current_task(); tasks.add(current)
        try:
            async with asyncio.timeout(5):
                head = await reader.readuntil(b"\r\n\r\n")
                require(len(head) <= 16384, "service_fixture_header_limit")
                lines = head.split(b"\r\n")
                require(lines[0] == b"POST /v1/chat/completions HTTP/1.1", "service_fixture_route")
                pairs = [line.split(b":", 1) for line in lines[1:] if line]
                require(all(len(pair) == 2 for pair in pairs), "service_fixture_headers")
                headers = {key.lower(): value.strip() for key, value in pairs}
                require(len(headers) == len(pairs) and headers.get(b"authorization") == b"Bearer " + credential.encode(),
                    "service_fixture_authentication")
                size = int(headers.get(b"content-length", b"0"))
                require(0 < size <= 32768 and counters["authenticated_requests"] < 64, "service_fixture_request_limit")
                body = json.loads(await reader.readexactly(size))
                require(body.get("model") == "synthetic-model" and body.get("stream") is False
                    and type(body.get("messages")) is list and len(body["messages"]) == 1,
                    "service_fixture_request_contract")
                counters["authenticated_requests"] += 1
                payload = json.dumps({"model": "synthetic-model", "choices": [{"message": {
                    "role": "assistant", "content": "synthetic-result"}, "finish_reason": "stop"}]}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(payload)).encode() + b"\r\n\r\n" + payload)
                await writer.drain()
        except Exception:
            failures.append("service_fixture_request_failed")
        finally:
            writer.close()
            try: await writer.wait_closed()
            except OSError: pass
            tasks.discard(current)
    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=16384)
    try:
        yield server, server.sockets[0].getsockname()[1], credential, counters
        require(not failures, "service_fixture_request_failed")
    finally:
        server.close(); await server.wait_closed()
        for task in tuple(tasks): task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)
        require(not tasks, "service_fixture_ownership_retained")


class PublicServiceHarness(common.Harness):
    def __init__(self, *args, report):
        super().__init__(*args); self.report = report

    async def start(self, state, environment, home, *, enabled):
        return await common.Harness.start(self, state, environment, home, enabled=False)

    async def native_preparation(self, base, state, home, environment, port, secret):
        nonce = str(uuid.uuid4())
        fixture, output = base / "native-service.json", base / "native-service-receipt.json"
        common.write_private_json(state / ("native-service-" + nonce + ".json"), {
            "schema_version": 1, "fixture_nonce": nonce, "expires_at": int(time.time()) + 300,
            "manager_file": file_identity(state / "manager-connection.json")})
        common.write_private_json(fixture, {"schema_version": 1, "fixture_nonce": nonce,
            "state_dir": str(state), "output_path": str(output), "answers_by_label": {},
            "service": {"name": "Synthetic public service", "model": "synthetic-model",
                "port": port, "secret_reference": secret}})
        receipt = None
        try:
            code, _ = await self.run([str(self.native), "--service-native-acceptance", str(fixture)], environment, home, timeout=330)
        finally:
            if output.is_file():
                receipt = common.private_json(output, 262144)
                self.report["native_diagnostic"] = native_diagnostic(receipt)
                self.report["native_receipt_sha256"] = common.digest(output)
        require(receipt is not None, "public_service_native_receipt_missing")
        require(code == 0 and receipt.get("passed") is True and receipt.get("stage") == "attached"
            and receipt.get("native_service_selection") is True and receipt.get("review_created_no_jobs") is True,
            "public_service_native_failed")
        required = {"GET v1/service-connectors", "POST v1/imports/service", "POST v1/preparations/form",
            "POST v1/preparations/plan-form", "POST v1/preparations/start"}
        require(required <= set(receipt["routes"]) and not any("resolutions/start" in route or "agent-preparations" in route
            for route in receipt["routes"]), "public_service_native_route_mismatch")
        return receipt

    async def case(self):
        from meecp.contracts import Tool, validate_package
        from meecp.preparation_service import references
        from meecp.preparation_service_receipts import parse_receipt
        from meecp.recipes import validate_bundle
        require(tuple(ref.key for ref in references()) == REFERENCES, "public_service_component_mismatch")
        base, state, home, environment = self.new_case("service")
        child = await self.start(state, environment, home, enabled=False)
        try:
            self.report["admission_digest"] = capabilities(await self.cli(state, environment, home, "capabilities", "--json"))
            async with protocol_fixture() as (server, port, credential, counters):
                secret = await self.manager(state, "/v1/secrets", {"name": "Synthetic service", "value": credential})
                unrelated = await self.manager(state, "/v1/secrets", {"name": "Unrelated synthetic", "value": "synthetic-unrelated-value"})
                common.stage("native")
                native = await self.native_preparation(base, state, home, environment, port, secret["secret_id"])
                require(counters["authenticated_requests"] == 1, "public_service_probe_count_mismatch")
                draft = await self.cli(state, environment, home, "imports", "inspect", native["draft_id"], "--json")
                job = await self.cli(state, environment, home, "preparations", "get", native["preparation_job_id"], "--json")
                require(draft["source"]["kind"] == "service_connection" and "locator" not in draft["source"]
                    and draft["source"]["files"] == [] and draft["source"]["revision"] == native["source_revision"]
                    and job["state"] == "succeeded" and job["ownership_settled"] is True
                    and job["reservation_retained"] is False and common.preparation_ownership_count(state) == 0,
                    "public_service_source_or_ownership_mismatch")
                package_root = Path(draft["attachment"]["path"]); package = validate_package(package_root)
                proof = parse_receipt((package_root / "mcpme.preparation.json").read_bytes())
                require(proof.schema_version == 3 and proof.inputs.schema_version == 2
                    and proof.binding.source_revision == draft["source"]["revision"]
                    and proof.service_probe_grant_digest == job["service_probe_grant_digest"]
                    and proof.inputs.digest == job["bound_inputs_digest"]
                    and package.digest == native["attached_package_digest"] == job["expected_package_digest"]
                    and job["verification"]["tools_checked"] == 2 and job["verification"]["fixtures_checked"] == 1,
                    "public_service_receipt_mismatch")
                for name in ("service.json", "mcpme.preparation.json", "mcpme.package.json", "mcpme.lock.json"):
                    data = (package_root / name).read_bytes()
                    require(secret["secret_id"].encode() not in data and credential.encode() not in data
                        and str(state).encode() not in data, "public_service_portability_mismatch")
                recipe = await self.cli(state, environment, home, "recipes", "inspect", "--id", RECIPE[0], "--version", RECIPE[1], "--json")
                require(recipe["digest"] == RECIPE[2] and Path(recipe["path"]).is_relative_to(state)
                    and Path(recipe["path"]).resolve() == Path(recipe["path"])
                    and validate_bundle(Path(recipe["path"]))["digest"] == RECIPE[2],
                    "public_service_recipe_mismatch")
                plan = await self.cli(state, environment, home, "install", "plan", "--import", draft["draft_id"], "--revision", draft["revision"], "--json")
                expected = [Tool.model_validate(item).mcp_schema() for item in plan["package"]["tools"]]
                require([item["name"] for item in expected] == ["generate", "check_connection"]
                    and expected == [item.mcp_schema() for item in package.manifest.tools], "public_service_tools_mismatch")
                operation = await self.finish_operation(state, environment, home,
                    await self.cli(state, environment, home, "install", "apply", "--plan", plan["plan_id"], "--json"))
                slot = operation["slot_id"]
                calls = [("generate", {"prompt": "Synthetic fixture", "max_output_tokens": 8}, {"text": "synthetic-result", "finish_reason": "stop"}),
                    ("check_connection", {}, {"compatible": True})]
                totals = {name: await self.sdk(state, environment, home, slot, expected, calls, name) for name in ("http", "stdio")}
                removed = await self.cli(state, environment, home, "recipes", "remove", "--id", RECIPE[0], "--version", RECIPE[1], "--json")
                require(removed["state"] == "removed" and removed["digest"] == RECIPE[2], "public_service_recipe_remove_failed")
                await self.shutdown(child, state); child = None
                original_base, original_state = base, state
                base, state, home, environment = self.new_case("relocated-service")
                exported = base / "accepted-package"
                shutil.copytree(package_root, exported)
                require(validate_package(exported).digest == package.digest, "public_service_export_changed")
                child = await self.start(state, environment, home, enabled=False)
                prior_secret = secret["secret_id"]
                secret = await self.manager(state, "/v1/secrets", {"name": "Remapped synthetic service", "value": credential})
                unrelated = await self.manager(state, "/v1/secrets", {"name": "Unrelated synthetic", "value": "synthetic-unrelated-value"})
                require(secret["secret_id"] != prior_secret, "public_service_reference_not_remapped")
                settings = base / "settings.json"
                common.write_private_json(settings, {"configuration": {}, "secret_references": {"api_key": secret["secret_id"]}})
                relocated_plan = await self.cli(state, environment, home, "install", "plan", "--package", exported,
                    "--settings", settings, "--start", "--start-policy", "host", "--json")
                require(relocated_plan["package_digest"] == package.digest, "public_service_relocated_plan_changed")
                operation = await self.finish_operation(state, environment, home,
                    await self.cli(state, environment, home, "install", "apply", "--plan", relocated_plan["plan_id"], "--json"))
                slot = operation["slot_id"]
                await self.shutdown(child, state); child = None
                # Both hosts are joined. Remove original authoring/import state
                # and the exported package; retain only the independently sealed
                # installation in the new state. No live audit rows are edited.
                await self.cleanup()
                require(original_base.parent == self.root and original_state.parent == original_base
                    and original_base.resolve() == original_base, "public_service_cleanup_scope")
                shutil.rmtree(original_base); shutil.rmtree(exported)
                require(not package_root.exists() and not original_state.exists() and not exported.exists(),
                    "public_service_source_retained")
                child = await self.start(state, environment, home, enabled=False)
                require((await self.cli(state, environment, home, "imports", "list", "--json"))["imports"] == [],
                    "public_service_import_retained")
                for name in totals: totals[name] += await self.sdk(state, environment, home, slot, expected, calls, name)
                await self.cli(state, environment, home, "secrets", "revoke", unrelated["secret_id"], "--json")
                for name in totals: totals[name] += await self.sdk(state, environment, home, slot, expected, calls, name)
                await self.cli(state, environment, home, "secrets", "revoke", secret["secret_id"], "--json")
                slots = (await self.cli(state, environment, home, "slots", "list", "--json"))["slots"]
                selected = next(item for item in slots if item["slot_id"] == slot)
                require(selected["observed_state"] == "stopped" and selected["wake_suppressed"] is True
                    and common.private_json(state / "slots" / slot / "ownership.json") == {}
                    and server.is_serving(), "public_service_revoke_drain_failed")
                self.report["case"] = {"passed": True, "native_service_source_without_folder": True,
                    "review_created_no_jobs": True, "receipt_schema": 3, "bound_inputs_schema": 2,
                    "http_calls": totals["http"], "stdio_calls": totals["stdio"],
                    "original_import_removed": True, "copied_recipe_removed": True,
                    "signed_author_recipe_preserved": True, "restart_both_transports": True, "reviewed_portable_secret_remapping": True,
                    "wrong_secret_preserved_runtime": True, "selected_secret_drained_local_owners": True,
                    "external_fixture_still_alive": True, "protocol_fixture_requests": counters["authenticated_requests"]}
        finally:
            if child is not None: await self.shutdown(child, state)


async def verify(host_path, native_path, report):
    common._host_paths(host_path); build = common.verify_host_payload(host_path)
    app = common.native_app_root(native_path)
    require(app is not None and host_path == app / "Contents/Resources/host", "public_service_signed_app_required")
    signature(app, deep=True); policy = active_policy(host_path); before = app_inventory(app)
    report.update(host_source_commit=build["source_commit"], policy_sha256=policy,
        native_binary_sha256=common.digest(native_path), host_artifacts_checked=len(build["artifact_sha256"]),
        input_app_files_checked=len(before), ordinary_host_launcher=True, execution_factory_injected=False)
    try:
        with common.isolated_workspace(report) as (root, lease):
            host, native = common.relocate_host_and_native(root, host_path, native_path, report)
            require(active_policy(host) == policy, "public_service_policy_changed")
            common.verify_host_payload(host); common._host_imports(host)
            harness = PublicServiceHarness(root, host, native, lease, report=report)
            try:
                await harness.case(); common._check_module_origins(host)
            finally:
                await harness.cleanup(); report["owned_children_joined"] = not harness.children
            require(app_inventory(common.native_app_root(native)) == before, "public_service_relocated_app_changed")
            report["relocated_app_unchanged"] = True
            signature(common.native_app_root(native), deep=True)
    finally:
        report["input_app_unchanged"] = before == app_inventory(app)
        signature(app, deep=True)
    report["verified"] = report.get("case", {}).get("passed") is True and report.get("owned_children_joined") is True and report["input_app_unchanged"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "native", "output"): parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(); os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "public_signed_local_service_protocol_fixture",
        "live_provider_calls": 0, "model_calls": 0, "limits": ["physical_clicks_not_operated",
            "synthetic_protocol_fixture_not_model_quality_or_live_provider_acceptance",
            "upstream_service_work_settlement_not_claimed", "remote_endpoint_support_not_advertised"]}
    try: asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True), report))
    except (Exception, KeyboardInterrupt) as error: report["diagnostic"] = common.bounded_failure(error)
    common.write_private_json(args.output, report)
    print(json.dumps({"verified": report["verified"], "diagnostic": report.get("diagnostic")}))
    return 0 if report["verified"] else 1


if __name__ == "__main__": raise SystemExit(main())
