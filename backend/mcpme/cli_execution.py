"""Bounded one-shot CLI spike with native no-fork ownership and durable proof.

This module is not registered as an automatic preparation capability. It
admits only the kernel-confined foreground support-matrix case; A3's broader
descendant topology and installed-host gates remain independent.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from .ownership_receipts import acknowledge_nonlaunch, prepare_receipt, verify_receipt
from .preparation_cli import ArgField, CliJsonSpec
from .preparation_contracts import PreparationError, canonical_bytes, load_contract, schema_accepts
from .private import check_private_file, ensure_private_directory, read_private_json, write_private_json
from .processes import OwnershipLost, ProcessError, ProcessOwner, _await_ownership, _watch_ownership


class CliExecutionError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CliResult:
    value: dict
    returncode: int
    stdout_bytes: int
    input_bytes: int
    peak_memory_bytes: int
    elapsed_ms: int


class CliExecutor:
    def __init__(self, package: Path, state: Path, reaper: Path, launcher: Path, *, memory_mb=128):
        self.package = Path(package).absolute()
        self.launcher = Path(launcher).absolute()
        self.memory_mb = memory_mb
        if (self.package.resolve() != self.package or self.package.is_symlink() or not self.package.is_dir()
                or type(memory_mb) is not int or not 1 <= memory_mb <= 4096):
            raise CliExecutionError("invalid_cli_runtime")
        self.state = ensure_private_directory(Path(state).absolute())
        self.journal_path = self.state / "invocations.json"
        self.lock_path = self.state / "invocations.lock"
        self.lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            check_private_file(self.lock_path)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.entries = read_private_json(self.journal_path) if self.journal_path.exists() else {"schema_version": 1, "entries": []}
            self._validate_journal()
            self.reconcile()
        except BaseException:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise
        self.owner = ProcessOwner(reaper)
        self.serial = asyncio.Lock()
        self.closed = False

    def _validate_journal(self):
        value = self.entries
        if (set(value) != {"schema_version", "entries"} or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["entries"]) is not list or len(value["entries"]) > 256):
            raise CliExecutionError("invalid_cli_journal")
        identities = set()
        for item in value["entries"]:
            if (not isinstance(item, dict) or set(item) != {"receipt", "contract_digest", "declared_memory_mb", "reserved_memory_mb", "state", "outcome", "measurements"}
                    or item["state"] not in {"pending", "settled"} or item["outcome"] not in {
                        "pending", "cli_success", "cli_canceled", "cli_timeout", "cli_failed"}
                    or type(item["reserved_memory_mb"]) is not int or not 0 <= item["reserved_memory_mb"] <= 4096
                    or type(item["declared_memory_mb"]) is not int or not 1 <= item["declared_memory_mb"] <= 4096
                    or item["reserved_memory_mb"] not in {0, item["declared_memory_mb"]}
                    or not isinstance(item["contract_digest"], str) or len(item["contract_digest"]) != 64
                    or not isinstance(item["receipt"], dict)):
                raise CliExecutionError("invalid_cli_journal")
            receipt = item["receipt"]
            if (set(receipt) != {"schema_version", "instance_id", "nonce"} or receipt["schema_version"] != 1
                    or not isinstance(receipt["instance_id"], str) or len(receipt["instance_id"]) != 32
                    or receipt["instance_id"] in identities or not isinstance(receipt["nonce"], str) or len(receipt["nonce"]) != 64
                    or any(c not in "0123456789abcdef" for c in receipt["instance_id"] + receipt["nonce"] + item["contract_digest"])
                    or (item["state"] == "settled") != (item["reserved_memory_mb"] == 0)):
                raise CliExecutionError("invalid_cli_journal")
            identities.add(receipt["instance_id"])
            measurements = item["measurements"]
            if measurements is not None and (not isinstance(measurements, dict)
                    or set(measurements) != {"peak_memory_bytes", "elapsed_ms", "input_bytes"}
                    or any(type(number) is not int or not 0 <= number <= 2**63 - 1 for number in measurements.values())):
                raise CliExecutionError("invalid_cli_journal")

    def reconcile(self):
        """Read exact private receipts. Never discover, adopt or signal a PID."""
        changed = False
        for item in self.entries["entries"]:
            proven = verify_receipt(self.state, item["receipt"])
            if item["state"] == "settled" and not proven:
                item["state"], item["reserved_memory_mb"], item["outcome"] = "pending", item["declared_memory_mb"], "cli_failed"
                changed = True
            elif item["state"] == "pending" and proven:
                item["state"], item["reserved_memory_mb"] = "settled", 0
                item["outcome"] = "cli_failed"
                changed = True
        if changed:
            write_private_json(self.journal_path, self.entries)
        return {"pending": sum(item["state"] == "pending" for item in self.entries["entries"]),
                "reserved_memory_mb": sum(item["reserved_memory_mb"] for item in self.entries["entries"])}

    async def execute(self, spec: CliJsonSpec, arguments: dict):
        if self.closed or not isinstance(spec, CliJsonSpec):
            raise CliExecutionError("cli_runtime_unavailable")
        try:
            if type(arguments) is not dict or not schema_accepts(spec.tool.input_schema, arguments):
                raise CliExecutionError("invalid_tool_arguments")
            payload = canonical_bytes(arguments) + b"\n"
            if len(payload) > spec.stdin_limit:
                raise CliExecutionError("cli_input_limit")
            argv = [arguments[item.field] if isinstance(item, ArgField) else item for item in spec.argv]
            if any("\0" in item for item in argv) or sum(len(item.encode("utf-8")) + 1 for item in argv) > spec.argument_limit:
                raise CliExecutionError("cli_argument_limit")
        except (PreparationError, ValueError, UnicodeError, RecursionError):
            raise CliExecutionError("invalid_tool_arguments") from None
        async with self.serial:
            if self.closed or self.reconcile()["pending"]:
                raise CliExecutionError("cli_ownership_unsettled")
            if len(self.entries["entries"]) >= 256:
                raise CliExecutionError("cli_journal_limit")
            return await self._execute(spec, payload, argv)

    async def _execute(self, spec, payload, argv):
        identifier = uuid.uuid4().hex
        fd, receipt = prepare_receipt(self.state, identifier)
        item = {"receipt": receipt, "contract_digest": spec.digest, "declared_memory_mb": self.memory_mb, "reserved_memory_mb": self.memory_mb,
                "state": "pending", "outcome": "pending", "measurements": None}
        self.entries["entries"].append(item)
        process = None
        started = time.monotonic()
        outcome = "cli_failed"
        try:
            write_private_json(self.journal_path, self.entries)
            async with asyncio.timeout(spec.deadline_seconds):
                executable = self.package / "python/bin/python3.12"
                script = self.package / "source" / spec.script
                if (executable.resolve() != executable or script.resolve() != script
                        or not executable.is_file() or not script.is_file()):
                    acknowledge_nonlaunch(fd, identifier, receipt["nonce"])
                    raise CliExecutionError("invalid_cli_runtime")
                process = await self.owner.launch(self.launcher,
                    [str(self.package), str(executable), "-I", "-S", "-B", str(script), *argv],
                    self.package / "source", {}, owned_group=True, instance_id=identifier,
                    startup_timeout=min(10, spec.deadline_seconds), stop_grace=0.1,
                    completion_fd=fd, completion_nonce=receipt["nonce"],
                    nonlaunch_callback=lambda: acknowledge_nonlaunch(fd, identifier, receipt["nonce"]))
                assert process.stdin is not None and process.stdout is not None
                async def communicate():
                    async def send():
                        try:
                            process.stdin.write(payload)
                            await process.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        finally:
                            process.stdin.close()
                    async def receive():
                        output = bytearray()
                        while True:
                            chunk = await process.stdout.read(min(65536, spec.stdout_limit - len(output) + 1))
                            if not chunk:
                                break
                            if len(output) + len(chunk) > spec.stdout_limit:
                                raise CliExecutionError("cli_output_limit")
                            output.extend(chunk)
                        return bytes(output)
                    sending = asyncio.create_task(send())
                    try:
                        output = await receive()
                        await sending
                        return output, await process.wait()
                    finally:
                        if not sending.done():
                            sending.cancel()
                        await _await_ownership(asyncio.gather(sending, return_exceptions=True))
                output, code = await _watch_ownership(process, communicate())
                if code:
                    raise CliExecutionError("cli_command_signaled" if code < 0 else spec.exit_errors.get(str(code), "cli_command_failed"))
                try:
                    value = load_contract(output)
                    if not schema_accepts(spec.tool.output_schema, value):
                        raise ValueError()
                except (PreparationError, ValueError, UnicodeError, RecursionError):
                    raise CliExecutionError("invalid_tool_result") from None
                if process.peak_memory_bytes is None or process.peak_memory_bytes <= 0:
                    raise CliExecutionError("cli_measurement_unavailable")
                if process.peak_memory_bytes > self.memory_mb * 1024 * 1024:
                    raise CliExecutionError("cli_memory_budget_exceeded")
                outcome = "cli_success"
                return CliResult(value, code, len(output), len(payload), process.peak_memory_bytes,
                                 int((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            outcome = "cli_canceled"
            raise
        except TimeoutError:
            outcome = "cli_timeout"
            raise CliExecutionError("cli_timeout") from None
        except OwnershipLost:
            raise CliExecutionError("cli_ownership_unsettled") from None
        except ProcessError:
            raise CliExecutionError("cli_launch_failed") from None
        finally:
            try:
                if process is not None:
                    await _await_ownership(process.stop())
            except ProcessError:
                outcome = "cli_failed"
            finally:
                os.close(fd)
                if process is not None and process.peak_memory_bytes is not None:
                    item["measurements"] = {"peak_memory_bytes": process.peak_memory_bytes,
                        "elapsed_ms": int((time.monotonic() - started) * 1000), "input_bytes": len(payload)}
                if verify_receipt(self.state, receipt):
                    item["state"], item["reserved_memory_mb"], item["outcome"] = "settled", 0, outcome
                write_private_json(self.journal_path, self.entries)
                if item["state"] != "settled" and outcome == "cli_success":
                    raise CliExecutionError("cli_ownership_unsettled")

    async def close(self):
        self.closed = True
        try:
            await _await_ownership(self.owner.close())
        finally:
            async with self.serial:
                self.reconcile()
                if self.lock_fd is not None:
                    os.close(self.lock_fd)
                    self.lock_fd = None
