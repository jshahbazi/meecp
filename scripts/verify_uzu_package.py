"""Verify a Uzu package through an isolated Meecp host and real MCP clients.

This never opens the user's app or state directory. The only persisted result
contains package identities, aggregate counts, timings and acceptance flags.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import ClientNotification, CancelledNotification, CancelledNotificationParams

from meecp.contracts import validate_package
from meecp.private import snapshot_package
from meecp.processes import _await_ownership
from meecp.supervisor import Supervisor
from meecp_uzu.service import GenerationResult, PROFILE, model_declaration


class AcceptanceFailure(ValueError):
    pass


def require(condition):
    if not condition:
        raise AcceptanceFailure()


def has_error_code(result, code):
    if result.isError is not True:
        return False
    for item in result.content[:8]:
        text = getattr(item, "text", None)
        if (type(text) is str and len(text.encode()) <= 4096
                and re.search(r"\b" + re.escape(code) + r"\b", text)):
            return True
    return False


class ObservedWrite:
    """Observe the last SDK request identity without retaining its arguments."""
    def __init__(self, stream):
        self.stream = stream
        self.request_id = None
        self.sent = asyncio.Event()

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *arguments):
        return await self.stream.__aexit__(*arguments)

    async def send(self, message):
        root = message.message.root
        await self.stream.send(message)
        if (getattr(root, "method", None) == "tools/call"
                and getattr(root, "params", {}).get("name") == "text_generate"):
            self.request_id = root.id
            self.sent.set()


@asynccontextmanager
async def sdk_session(connection):
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]},
            trust_env=False, timeout=130) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
            observed = ObservedWrite(write)
            async with ClientSession(read, observed) as session:
                initialized = await session.initialize()
                require(initialized.protocolVersion == "2025-11-25")
                require(set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"})
                yield session, observed


async def status(session):
    result = await session.call_tool("text_status", {})
    require(not result.isError and type(result.structuredContent) is dict)
    value = result.structuredContent
    require(value.get("ready") is True and value.get("profile") == PROFILE)
    require(type(value.get("completed")) is int and type(value.get("queue_depth")) is int)
    require(0 <= value["queue_depth"] <= 4)
    return value


async def generation(session, text, maximum, output_schema):
    result = await session.call_tool("text_generate", {"text": text, "max_output_tokens": maximum})
    require(not result.isError and type(result.structuredContent) is dict)
    value = result.structuredContent
    Draft202012Validator(output_schema).validate(value)
    parsed = GenerationResult.model_validate(value)
    require(parsed.input_tokens + maximum + 8 <= 4096 and 0 < parsed.output_tokens <= maximum)
    require(len(parsed.text.encode()) <= maximum * 256)
    return value


async def install(host, package_root):
    plan = await host.plan_install_async({"package": str(package_root), "start": True, "start_policy": "manual"})
    operation = await host.apply_install(plan["plan_id"])
    async with asyncio.timeout(300):
        while True:
            value = host.registry.get_operation(operation["operation_id"])
            if value.status not in {"pending", "running"}:
                require(value.status == "completed")
                return host.registry.get_slot(value.slot_id)
            await asyncio.sleep(.02)


async def transport_checks(host, slot):
    connection = host.connection(slot.id)
    adapter = host.residents[slot.id]["adapter"]
    engine = host.residents[slot.id]["engine"]
    authenticated = 0
    rejected = 0
    async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
        for resident in (engine, adapter):
            endpoint = resident.endpoint + "/health"
            headers = {"Authorization": "Bearer " + resident.key}
            response = await http.get(endpoint, headers=headers)
            require(response.status_code == 200)
            value = response.json()
            require(value.get("ready") is True and type(value.get("generation")) is int
                and value["generation"] == slot.generation and value.get("nonce") == resident.nonce)
            if resident is engine:
                require(value.get("context_tokens") == 4096 and value.get("profile") == PROFILE)
                require(value.get("engine_identity") == host.package(slot).manifest.model.engine_identity)
            authenticated += 1
            for extra, expected in (({}, 401), ({"Host": "untrusted.invalid"}, 403),
                    ({"Origin": "https://untrusted.invalid"}, 403)):
                request_headers = {} if not extra else {**headers, **extra}
                require((await http.get(endpoint, headers=request_headers)).status_code == expected)
                rejected += 1
        endpoint = connection["endpoint"]
        auth = {"Authorization": "Bearer " + connection["token"]}
        for extra, expected in (({}, 401), ({"Host": "untrusted.invalid"}, 403),
                ({"Origin": "https://untrusted.invalid"}, 403)):
            headers = {} if not extra else {**auth, **extra}
            require((await http.post(endpoint, headers=headers)).status_code == expected)
            rejected += 1
        require((await http.post(endpoint, headers=auth, content=b"x" * 32769)).status_code == 413)
        require((await http.post(endpoint, headers=auth, content=b"invalid-json")).status_code == 400)
        rejected += 2
    return {"authenticated_health_routes": authenticated, "rejected_transport_probes": rejected}


async def cancellation_checks(host, slot, connection):
    pending = []
    async with sdk_session(connection) as (first, observed):
        async with sdk_session(connection) as (second, _):
            before = await status(second)
            identities = set(host.owner(slot.id).instances)
            require(len(identities) == 3 and host.reservations[slot.id]["gpu"] is True)
            observed.sent.clear()
            active = asyncio.create_task(first.call_tool("text_generate", {
                "text": "Write the integers from 1 to 10000 separated by spaces. Keep going until stopped.\n" + "x " * 1800,
                "max_output_tokens": 256}))
            pending.append(active)
            try:
                await asyncio.wait_for(observed.sent.wait(), 10)
                async with asyncio.timeout(10):
                    while True:
                        during = await status(second)
                        if during["queue_depth"] == 1:
                            require(during["completed"] == before["completed"] and not active.done())
                            break
                        require(not active.done())
                        await asyncio.sleep(.005)
                await first.send_notification(ClientNotification(CancelledNotification(
                    params=CancelledNotificationParams(requestId=observed.request_id))))
                cancelled_client = False
                try:
                    await asyncio.wait_for(asyncio.shield(active), 10)
                except McpError:
                    cancelled_client = True
                require(cancelled_client)
                live_samples = 0
                async with asyncio.timeout(130):
                    while True:
                        require(set(host.owner(slot.id).instances) == identities
                            and slot.id in host.reservations and host.reservations[slot.id]["gpu"] is True)
                        settled = await status(second)
                        require(settled["engine_instance_id"] == before["engine_instance_id"])
                        if settled["queue_depth"] == 0:
                            break
                        require(settled["completed"] == before["completed"])
                        live_samples += 1
                        await asyncio.sleep(.005)
                require(live_samples > 0 and settled["completed"] == before["completed"] + 1)
                return {"sdk_cancellation_observed": True, "reservation_retained_until_settlement": True,
                    "cancelled_generation_settled": True, "live_ownership_samples": live_samples}
            finally:
                await _await_ownership(asyncio.gather(*pending, return_exceptions=True))


def retired(processes):
    require(all(process.returncode is not None and not process.ownership_lost
        and process.wrapper.returncode is not None for process in processes))


async def close_host(host, processes):
    await _await_ownership(host.close())
    retired(processes)
    require(not host.residents and not host.workers and not host.reservations)
    require(all(not owner.instances for owner in host.owners.values()))
    require(all(not ledger for ledger in host.ledgers.values()))


async def verify(args, record):
    started = time.perf_counter()
    record["stage"] = "package_identity"
    package = await asyncio.to_thread(validate_package, args.package.resolve())
    require(package.manifest.package_id == "local.uzu-textgen-experimental"
        and package.manifest.profile == "model-service")
    declaration = await asyncio.to_thread(model_declaration, package.root)
    require(package.manifest.model.model_dump() == declaration)
    require({process.role for process in package.manifest.processes} == {"engine", "adapter"}
        and len(package.manifest.processes) == 2)
    require(args.reaper.is_file() and os.access(args.reaper, os.X_OK))
    record["package_digest"] = package.digest
    record["engine_binary_sha256"] = declaration["engine_identity"]["binary_sha256"]
    record["checks"]["package_identity"] = True
    if args.static_only:
        record.update({"passed": True, "static_only": True, "stage": "completed",
            "elapsed_seconds": time.perf_counter() - started})
        return

    workspace_parent = args.work_parent.resolve()
    workspace_parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="isolated-acceptance-", dir=workspace_parent))
    relocated = workspace / "relocated Uzu package"
    record["stage"] = "package_relocation"
    moved = await asyncio.to_thread(snapshot_package, package, relocated)
    require(moved.digest == package.digest)
    record["checks"]["relocated_package_identity"] = True
    state = workspace / "isolated-host-state"
    host = Supervisor(state, args.reaper.resolve())
    processes = []
    try:
        record["stage"] = "isolated_host_install"
        await host.initialize()
        slot = await install(host, relocated)
        require(slot.observed_state == "ready")
        require(Path(slot.package_path).is_relative_to(state) and Path(slot.package_path) != relocated)
        require(len(host.owner(slot.id).instances) == 3 and slot.id in host.reservations)
        processes.extend(host.owner(slot.id).instances.values())
        relocated.rename(workspace / "source package unavailable")
        require(not relocated.exists())
        record["checks"]["isolated_install_and_start"] = True
        record["checks"]["installed_copy_runs_after_source_move"] = True

        record["stage"] = "transport"
        record["transport"] = await transport_checks(host, slot)
        record["checks"]["authenticated_loopback_transport"] = True

        record["stage"] = "public_mcp_generation"
        expected_tools = [tool.mcp_schema() for tool in package.manifest.tools]
        output_schema = next(tool["outputSchema"] for tool in expected_tools if tool["name"] == "text_generate")
        connection = host.connection(slot.id)
        async with sdk_session(connection) as (session, _):
            listing = await session.list_tools()
            require(listing.nextCursor is None)
            require([tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools] == expected_tools)
            before = await status(session)
            first = await generation(session, "Write one short sentence about a red umbrella.", 32, output_schema)
            second = await generation(session, "Write one short sentence about a red umbrella.", 32, output_schema)
            after = await status(session)
            require(after["completed"] == before["completed"] + 2 and after["queue_depth"] == 0
                and after["engine_instance_id"] == before["engine_instance_id"])
            record["generation"] = {"successful_calls": 2, "repeat_output_equal": first["text"] == second["text"],
                "first_input_tokens": first["input_tokens"], "first_output_tokens": first["output_tokens"],
                "second_input_tokens": second["input_tokens"], "second_output_tokens": second["output_tokens"]}
            for invalid, code in (({"text": "synthetic", "max_output_tokens": 257}, "invalid_request"),
                    ({"text": "synthetic", "max_output_tokens": True}, "invalid_request"),
                    ({"text": "synthetic", "max_output_tokens": 1, "extra": "synthetic"}, "invalid_request"),
                    ({"text": "界" * 5462, "max_output_tokens": 1}, "input_limit"),
                    ({"text": "x " * 3900, "max_output_tokens": 256}, "context_limit")):
                require(has_error_code(await session.call_tool("text_generate", invalid), code))
            settled = await status(session)
            require(settled["completed"] == after["completed"] and settled["queue_depth"] == 0
                and settled["engine_instance_id"] == after["engine_instance_id"])
            record["generation"]["rejected_boundary_calls"] = 5
        record["checks"]["public_sdk_schema_and_generation"] = True
        record["checks"]["context_rejection_preserves_generation"] = True

        record["stage"] = "public_mcp_cancellation"
        record["cancellation"] = await cancellation_checks(host, slot, connection)
        record["checks"]["public_sdk_cancellation_ownership"] = True

        record["stage"] = "explicit_stop"
        active_graph = tuple(host.owner(slot.id).instances.values())
        first_generation = host.registry.get_slot(slot.id).generation
        await host.control(slot.id, "stop")
        retired(active_graph)
        require(slot.id not in host.residents and slot.id not in host.reservations)
        require(host.registry.get_slot(slot.id).observed_state == "stopped")
        require(len(host.owner(slot.id).instances) == 1)
        processes.extend(host.owner(slot.id).instances.values())
        record["checks"]["stop_reaps_active_engine_adapter_gateway"] = True
        await close_host(host, processes)
        host = None
        record["checks"]["host_close_reaps_stopped_gateway"] = True

        record["stage"] = "host_restart"
        host = Supervisor(state, args.reaper.resolve())
        processes = []
        await host.initialize()
        require(host.registry.get_slot(slot.id).observed_state == "stopped" and slot.id not in host.residents)
        processes.extend(host.owner(slot.id).instances.values())
        await host.control(slot.id, "start")
        require(host.registry.get_slot(slot.id).generation > first_generation)
        processes.extend(host.owner(slot.id).instances.values())
        async with sdk_session(host.connection(slot.id)) as (session, _):
            restarted = await status(session)
            require(restarted["engine_instance_id"] != before["engine_instance_id"])
            await generation(session, "Write one short sentence about a blue umbrella.", 16, output_schema)
        record["checks"]["restart_uses_fresh_owned_generation"] = True
        record["stage"] = "final_cleanup"
        await close_host(host, processes)
        host = None
        record["checks"]["final_graph_reaped_and_reservation_released"] = True
        record.update({"passed": True, "static_only": False, "stage": "completed",
            "elapsed_seconds": time.perf_counter() - started})
    finally:
        if host is not None:
            await close_host(host, processes)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=ROOT / "dist/uzu-textgen-experimental")
    parser.add_argument("--reaper", type=Path, default=ROOT / "runtime/uzu-rust/meecp-reap")
    parser.add_argument("--work-parent", type=Path, default=ROOT / "runtime/uzu-rust/acceptance")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/uzu-rust/acceptance.json")
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()
    record = {"schema_version": 1, "passed": False, "static_only": args.static_only, "checks": {},
        "recorded_at": datetime.now(timezone.utc).isoformat(), "stage": "initializing"}
    try:
        asyncio.run(verify(args, record))
    except BaseException:
        record["passed"] = False
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, sort_keys=True, indent=2, allow_nan=False) + "\n")
    except BaseException:
        record["passed"] = False
        record["stage"] = "record_persistence"
    print(json.dumps(record, sort_keys=True, allow_nan=False))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
