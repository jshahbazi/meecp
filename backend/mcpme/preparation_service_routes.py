"""Owner-only service intake and secret-revocation lifecycle integration."""
from __future__ import annotations

import asyncio

from .preparation_contracts import PreparationError
from .preparation_service import references, validate_components
from .preparation_service_source import source
from .processes import _await_ownership


def registry():
    protocol, transport, task = references()
    return {"schema_version": 1, "connectors": [{"protocol": protocol.model_dump(),
        "transport_auth": transport.model_dump(), "task": task.model_dump(),
        "label": "Local OpenAI-compatible text service", "endpoint": {"scheme": "http", "host": "127.0.0.1", "base_path": "/v1"},
        "authentication": "bearer", "secret_role": "api_key", "response_framing": "content-length-json",
        "streaming": False, "redirects": False, "dns": False, "remote_endpoints": False,
        "upstream_process_owned": False, "model_bytes_attested": False,
        "limits": {"timeout_seconds": 60, "max_input_bytes": 16384, "max_output_tokens": 512, "max_response_bytes": 1048576}}]}


def create(host, payload):
    required = {"schema_version", "name", "intent", "start", "start_policy", "connection"}
    if type(payload) is not dict or set(payload) != required or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise PreparationError("invalid_service_request")
    selected = source(payload["connection"], host.secrets, validate_components=validate_components)
    return host.imports.create_service(selected, **{key: payload[key] for key in ("name", "intent", "start", "start_policy")})


def revise(host, draft_id, payload):
    if type(payload) is not dict or set(payload) != {"revision", "connection"} or type(payload["revision"]) is not int:
        raise PreparationError("invalid_service_request")
    selected = source(payload["connection"], host.secrets, validate_components=validate_components)
    return host.imports.revise_service(draft_id, payload["revision"], selected)


async def drain_revoked_service(host, secret_id):
    """Drain every affected owned scope even if another scope cannot settle."""
    from .preparations import TERMINAL
    from .preparation_service_contracts import parse_policy, ServiceExecutionPolicyV1
    import json
    failed = False
    # The caller revokes synchronously first. This retained task continues after
    # client cancellation; an unrelated corrupt slot cannot skip later cleanup.
    for job in host.preparations.list():
        if job["state"] in TERMINAL and not job["reservation_retained"]:
            continue
        if (job.get("bound_inputs") or {}).get("source", {}).get("kind") != "service_connection":
            continue
        try:
            with host.imports._lock:
                row = host.preparations.db.execute("SELECT data FROM preparation_job_inputs WHERE job_id=?", (job["job_id"],)).fetchone()
            if row is None:
                raise PreparationError("service_probe_authority_required")
            policy = parse_policy(json.loads(row["data"])["execution_policy"])
            if type(policy) is not ServiceExecutionPolicyV1 or policy.digest != job["binding"]["policy_digest"]:
                raise PreparationError("service_probe_authority_mismatch")
            if policy.service_probe.secret_reference == secret_id:
                await host.preparation_runner.cancel(job["job_id"])
        except Exception:
            failed = True
    for slot in host.registry.list_slots():
        try:
            package = await host.static_work(host.package, slot)
            if package.manifest.profile != "mcp-stdio":
                continue
            settings = host.package_settings(slot, package)
            if secret_id not in settings["secret_references"].values():
                continue
            paths = {item.path for item in package.lock.artifacts}
            if "backend/mcpme_service/server.py" not in paths or "service.json" not in paths:
                continue
            lock = host.locks.setdefault(slot.id, asyncio.Lock())
            async with lock:
                host.registry.update_slot(slot.id, desired_state="stopped", observed_state="draining", wake_suppressed=True)
                settled = True
                for stop in (host.stop_gateway, host.stop_residents):
                    try:
                        await stop(slot.id)
                    except Exception:
                        settled = False
                if not settled:
                    raise PreparationError("service_revocation_incomplete")
                current = host.registry.get_slot(slot.id)
                host.registry.update_slot(slot.id, generation=current.generation + 1, observed_state="stopped", error_code="service_secret_revoked")
        except Exception:
            failed = True
    if failed:
        raise PreparationError("service_revocation_incomplete")


async def revoke(host, secret_id):
    result = host.secrets.revoke(secret_id)
    task = asyncio.create_task(drain_revoked_service(host, secret_id))
    await _await_ownership(task)
    return result
