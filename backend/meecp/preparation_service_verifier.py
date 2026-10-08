"""Service-specific probe: one reviewed origin, one call, owned adapter only."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import socket

from mcp import ClientSession

from .contracts import PackageError
from .preparation_contracts import PreparationError, canonical_bytes
from .preparation_processes import _settle
from .preparation_verifier import _validate_joined, _owned_transport, _ProbeLaunchIdentity, VerificationReport
from .processes import OwnershipLost, _await_ownership, _watch_ownership


async def joined(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    finally:
        await _await_ownership(asyncio.gather(task, return_exceptions=True))


def confined_profile(base, connection):
    """Darwin's address selector accepts localhost, not numeric IPv4 text.

    Seatbelt bounds this to loopback TCP and the reviewed port. The separately
    sealed transport selects numeric AF_INET 127.0.0.1 without DNS or redirects.
    """
    from .preparation_service_contracts import ServiceConnectionV1
    if type(connection) is not ServiceConnectionV1:
        raise PreparationError("service_probe_authority_required")
    return base + '\n(allow network-outbound (remote tcp "localhost:' + str(connection.endpoint.port) + '"))'


def validate_generated(package, descriptor, connection, job):
    from .preparation_service_receipts import parse_receipt, validate_job_receipt
    from .preparation_service_builder import BOOTSTRAP, LAUNCHER
    from .contracts import _read_file
    records = {item.path: item for item in package.lock.artifacts}
    root = Path(__file__).resolve().parents[1]
    expected = {"bootstrap.py": BOOTSTRAP, "bin/service": LAUNCHER,
                "service.json": canonical_bytes(connection.portable())}
    expected.update({"backend/meecp_service/" + path.name: path.read_bytes()
                    for path in sorted((root / "meecp_service").glob("*.py"))})
    if {path for path in records if path.startswith("backend/")} != {path for path in expected if path.startswith("backend/")}:
        raise PreparationError("service_adapter_identity_mismatch")
    for name, data in expected.items():
        if name not in records or records[name].sha256 != hashlib.sha256(data).hexdigest():
            raise PreparationError("service_adapter_identity_mismatch")
    from .preparations import receipt_name
    receipt = parse_receipt(_read_file(package.root / receipt_name(package), 524288))
    validate_job_receipt(receipt, job)
    if (receipt.schema_version != 3 or receipt.binding.model_dump() != job["binding"]
            or package.manifest.source_revision != connection.revision or receipt.inputs.descriptor_digest != descriptor.digest
            or package.manifest.tools != descriptor.tools or package.manifest.access != descriptor.access
            or package.manifest.resources.model_dump() != descriptor.resources.model_dump()
            or package.manifest.lifecycle != descriptor.lifecycle or package.manifest.profile != "mcp-stdio"
            or package.manifest.package_id != descriptor.package.id or package.manifest.version != descriptor.package.version
            or package.manifest.name != descriptor.package.name):
        raise PreparationError("service_candidate_mismatch")


async def verify_service(family, package_root, descriptor, reaper, workspace, *, timeout_seconds=60, owner=None):
    # Only the journaled managed attempt can provide the original grant. A
    # package or callers of the ordinary verifier cannot select this policy.
    from .preparation_runner import JournaledPreparationOwner
    from .preparation_probe_confinement import ProbeConfinementV1
    if type(owner) is not JournaledPreparationOwner or owner.reaper != reaper:
        raise PreparationError("service_probe_authority_required")
    value, draft, policy = owner.store.job_inputs(owner.job_id)
    if value != descriptor:
        raise PreparationError("service_probe_authority_mismatch")
    selected = family.execution_policy(value, draft, policy)
    connection = family.validate_inputs(value, draft)
    job = owner.store.get(owner.job_id)
    process = None; sender = receiver = None
    try:
        async with asyncio.timeout(timeout_seconds):
            package = await _validate_joined(package_root)
            await joined(validate_generated, package, descriptor, connection, job)
            base = await joined(ProbeConfinementV1.capture, package, workspace, reaper)
            identity = _ProbeLaunchIdentity.capture(package, base)
            launch = await joined(base.launch, package, reaper)
            # The underlying private-Python policy remains unchanged. This
            # distinct grant adds the exact TCP port on Seatbelt localhost. The sealed transport
            # independently fixes AF_INET 127.0.0.1 and performs no DNS lookup.
            profile = confined_profile(base.profile, connection)
            executable, argv, cwd, env = launch
            argv = ["-p", profile, *argv[2:]]
            sender, receiver = socket.socketpair(); sender.setblocking(False)
            env["MCPME_SETTINGS_FD"] = str(receiver.fileno())
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            def admit():
                identity.check(); owner.check_current()
                family.execution_policy(value, draft, selected)
                if asyncio.get_running_loop().time() >= deadline:
                    raise PreparationError("service_probe_deadline")
                return True
            process = await owner.launch(executable, argv, cwd, env, owned_group=True, expected_reaper=reaper,
                inherited_fds=(receiver.fileno(),), startup_timeout=min(timeout_seconds, 120), stop_grace=0.1, admit=admit)
            admit()
            # Resolve after spawn acknowledgment, never cache the intake value.
            values = family.secrets.resolve({"api_key": selected.service_probe.secret_reference})
            admit()
            payload = json.dumps({"schema_version": 1, "configuration": {}, "secrets": values},
                ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            if len(payload) > 524288:
                raise PreparationError("service_settings_invalid")
            await asyncio.get_running_loop().sock_sendall(sender, payload)
            sender.shutdown(socket.SHUT_WR)
            payload = values = None
            async def probe():
                async with _owned_transport(process) as (incoming, outgoing):
                    async with ClientSession(incoming, outgoing) as client:
                        result = await client.initialize()
                        if result.protocolVersion != "2025-11-25" or set(result.capabilities.model_dump(exclude_none=True)) != {"tools"}:
                            raise PreparationError("service_probe_capabilities")
                        listing = await client.list_tools()
                        if listing.nextCursor is not None or [t.model_dump(by_alias=True, exclude_none=True) for t in listing.tools] != [t.mcp_schema() for t in descriptor.tools]:
                            raise PreparationError("service_probe_tools")
                        admit()
                        output = await client.call_tool("check_connection", {})
                        if output.isError or output.structuredContent != {"compatible": True}:
                            raise PreparationError("service_probe_failed")
                        admit()
            await _watch_ownership(process, probe())
        await _settle(process)
        peak = process.peak_memory_bytes
        if type(peak) is not int or not 0 < peak <= descriptor.resources.memory_mb * 1024**2:
            raise PreparationError("service_probe_measurement_unavailable")
        current = await _validate_joined(package.root)
        if current.digest != package.digest:
            raise PreparationError("service_package_mutated")
        await joined(base.check, current, reaper)
        owner.check_current(); family.execution_policy(value, draft, selected)
        return VerificationReport(package.digest, 2, 1, "2025-11-25", peak)
    except OwnershipLost:
        raise
    except TimeoutError:
        raise PreparationError("service_probe_deadline") from None
    except PreparationError:
        raise
    except Exception:
        raise PreparationError("service_probe_failed") from None
    finally:
        if sender is not None: sender.close()
        if receiver is not None: receiver.close()
        if process is not None: await _settle(process)
        await owner.close()
