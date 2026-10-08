"""Native, identity-bound ownership of declared foreground package processes.

Raw stdio descriptors can be transferred to a gateway using SCM_RIGHTS. This
module never sends a signal to a recorded target PID; only its native parent
can terminate that child. Wrapper loss deliberately retains the instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import socket
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio


class ProcessError(RuntimeError):
    """A sanitized process lifecycle failure."""


class OwnershipLost(ProcessError):
    def __init__(self) -> None:
        super().__init__("ownership_lost")


async def _await_ownership(awaitable):
    """Settle an owned action even when a caller repeatedly cancels cleanup.

    Native spawn/reap handles outlive a canceled API request. AnyIO applies
    level cancellation; asyncio can deliver several explicit cancel calls.
    Shield both without suppressing the caller's original cancellation.
    """
    task = asyncio.ensure_future(awaitable)
    with anyio.CancelScope(shield=True):
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()


async def _watch_ownership(process, action):
    """Interrupt a stream/protocol action promptly on observed wrapper loss."""
    task = asyncio.ensure_future(action)
    try:
        await asyncio.wait((task, process._done), return_when=asyncio.FIRST_COMPLETED)
        if process._done.done() and process.ownership_lost:
            raise OwnershipLost()
        return await task
    finally:
        if not task.done():
            task.cancel()
        await _await_ownership(asyncio.gather(task, return_exceptions=True))


class _WriteProtocol(asyncio.streams.FlowControlMixin):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop=loop)
        self.closed: asyncio.Future[None] = loop.create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        if not self.closed.done():
            self.closed.set_result(None)

    def _get_close_waiter(self, stream: asyncio.StreamWriter) -> asyncio.Future[None]:
        return self.closed


class OwnedProcess:
    def __init__(
        self, owner: ProcessOwner, instance_id: str,
        wrapper: asyncio.subprocess.Process, control: socket.socket,
        stdin_fd: int, stdout_fd: int, stop_grace: float,
    ) -> None:
        self.owner = owner
        self.instance_id = instance_id
        self.wrapper = wrapper
        self.reaper_pid = wrapper.pid
        self.pid: int | None = None
        self.returncode: int | None = None
        self.peak_memory_bytes: int | None = None
        self.ownership_lost = False
        self.stdin_fd: int | None = stdin_fd
        self.stdout_fd: int | None = stdout_fd
        self.stdin: asyncio.StreamWriter | None = None
        self.stdout: asyncio.StreamReader | None = None
        self._read_transport: asyncio.Transport | None = None
        self._control = control
        self._stop_grace = stop_grace
        self._spawned: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._monitor = asyncio.create_task(self._observe())

    async def _observe(self) -> None:
        exited = False
        buffer = bytearray()
        try:
            while True:
                chunk = await asyncio.get_running_loop().sock_recv(self._control, 4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > 8192:
                    break
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    event = json.loads(line)
                    value = event["value"]
                    if type(value) is not int:
                        raise ValueError
                    if event["event"] == "spawn" and self.pid is None and value > 1:
                        self.pid = value
                        if self.owner.graph_accounting is not None:
                            self.owner.graph_accounting.record(self.instance_id, "active")
                        if not self._spawned.done():
                            self._spawned.set_result(True)
                    elif event["event"] == "peak_memory" and self.peak_memory_bytes is None and 0 <= value <= 2**63 - 1:
                        self.peak_memory_bytes = value
                    elif event["event"] in {"exit", "launch_failed"}:
                        self.returncode = value
                        exited = True
                        if not self._spawned.done():
                            self._spawned.set_result(False)
                    else:
                        raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            self._control.close()
            await self.wrapper.wait()
            if self.owner.graph_accounting is not None:
                self.owner.graph_accounting.record(self.instance_id, "settled" if exited else "unknown", self.peak_memory_bytes)
            if not exited:
                self.ownership_lost = True
                if not self._spawned.done():
                    self._spawned.set_result(False)
                callback = self.owner.on_ownership_lost
                if callback is not None:
                    try:
                        result = callback(self)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        # Metadata persistence errors must not emit exception data.
                        self.owner.persistence_failed = True
            else:
                self.owner.instances.pop(self.instance_id, None)
            self._done.set_result(None)

    async def _streams(self) -> None:
        loop = asyncio.get_running_loop()
        assert self.stdout_fd is not None and self.stdin_fd is not None
        reader = asyncio.StreamReader(limit=1024 * 1024)
        reader_protocol = asyncio.StreamReaderProtocol(reader)
        read_pipe = os.fdopen(self.stdout_fd, "rb", buffering=0)
        self.stdout_fd = None
        transport, _ = await loop.connect_read_pipe(lambda: reader_protocol, read_pipe)
        self._read_transport = transport
        self.stdout = reader
        writer_protocol = _WriteProtocol(loop)
        write_pipe = os.fdopen(self.stdin_fd, "wb", buffering=0)
        self.stdin_fd = None
        transport, _ = await loop.connect_write_pipe(lambda: writer_protocol, write_pipe)
        self.stdin = asyncio.StreamWriter(transport, writer_protocol, None, loop)

    def close_stdio(self) -> None:
        """Close supervisor-held copies, including after a successful FD transfer."""
        if self.stdin is not None:
            self.stdin.close()
        if self._read_transport is not None:
            self._read_transport.close()
        for name in ("stdin_fd", "stdout_fd"):
            fd = getattr(self, name)
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, name, None)

    async def wait(self) -> int:
        await asyncio.shield(self._done)
        if self.ownership_lost:
            raise OwnershipLost()
        assert self.returncode is not None
        return self.returncode

    async def stop(self, timeout: float | None = None) -> int:
        self.close_stdio()
        if not self._done.done():
            with contextlib.suppress(OSError):
                await asyncio.get_running_loop().sock_sendall(self._control, b"S")
        try:
            return await asyncio.wait_for(
                self.wait(), timeout if timeout is not None else self._stop_grace + 5,
            )
        except TimeoutError:
            # Do not destroy the wrapper and invalidate its target ownership.
            raise ProcessError("reap_timeout") from None


class ProcessOwner:
    def __init__(
        self, reaper: Path,
        on_ownership_lost: Callable[[OwnedProcess], Awaitable[None] | None] | None = None,
        *, graph_accounting=None, graph_admission_check=None,
    ) -> None:
        if graph_accounting is not None:
            from .process_graph_accounting_v1 import FixedProcessGraphV1
            if type(graph_accounting) is not FixedProcessGraphV1:
                raise ProcessError("invalid_process_graph")
            if not callable(graph_admission_check):
                raise ProcessError("process_graph_admission_required")
            graph_accounting.bind_owner(self)
        elif graph_admission_check is not None:
            raise ProcessError("invalid_process_graph")
        self.graph_accounting = graph_accounting
        self.graph_admission_check = graph_admission_check
        self._graph_admission_identity = graph_admission_check
        self._graph_identity = graph_accounting
        self._graph_contract = graph_accounting.contract_digest if graph_accounting is not None else None
        self.reaper = Path(reaper).absolute()
        self._graph_reaper = self.reaper
        self.on_ownership_lost = on_ownership_lost
        self.instances: dict[str, OwnedProcess] = {}
        self.persistence_failed = False
        self._closed = False
        self._launches: set[asyncio.Task[OwnedProcess]] = set()
        self._pending_ids: set[str] = set()
        self._graph_checks: set[asyncio.Task] = set()

    def _check_graph_admission(self, deadline):
        if self._graph_identity is None:
            return
        def check_binding():
            if (self._closed or self.graph_accounting is not self._graph_identity or
                    self.graph_admission_check is not self._graph_admission_identity or
                    self.graph_accounting.contract_digest != self._graph_contract or time.monotonic() >= deadline):
                raise ProcessError("process_graph_not_admitted")
            if self.reaper != self._graph_reaper:
                raise ProcessError("native_reaper_changed")
        check_binding()
        try:
            accepted = self.graph_admission_check()
            if inspect.iscoroutine(accepted):
                accepted.close()
            if accepted is not True:
                raise ProcessError("process_graph_not_admitted")
        except Exception:
            raise ProcessError("process_graph_not_admitted") from None
        check_binding()

    @staticmethod
    def _check_admit(admit):
        if admit is None:
            return
        try:
            accepted = admit()
            if inspect.iscoroutine(accepted):
                accepted.close()
            if accepted is not True:
                raise ProcessError("launch_not_admitted")
        except Exception:
            raise ProcessError("launch_not_admitted") from None

    def holds_live_instances(self, identifiers: set[str]) -> bool:
        """Confirm current capabilities without discovering or adopting PIDs."""
        if (self._closed or self.persistence_failed or self._launches or self._pending_ids or self._graph_checks
                or identifiers != set(self.instances)):
            return False
        return all(process.owner is self and process.instance_id == identifier
                   and process.pid is not None and not process.ownership_lost
                   and process.returncode is None and process.wrapper.returncode is None
                   and not process._done.done() and not process._monitor.done()
                   for identifier, process in self.instances.items())

    def _preflight(self, identifier, io_mode, inherited_fds, startup_timeout, stop_grace,
                   completion_fd, completion_nonce, owned_group):
        if self._closed:
            raise ProcessError("owner_closed")
        if any(p.ownership_lost for p in self.instances.values()):
            raise OwnershipLost()
        if io_mode not in {"streams", "fds"} or not 0 <= stop_grace <= 30 or type(owned_group) is not bool:
            raise ProcessError("invalid_launch")
        if not 0 < startup_timeout <= 120:
            raise ProcessError("invalid_launch")
        if identifier in self.instances or identifier in self._pending_ids:
            raise ProcessError("duplicate_instance")
        if completion_fd is None and completion_nonce is not None:
            raise ProcessError("invalid_launch")
        if completion_fd is not None:
            from .ownership_receipts import validate_receipt_descriptor
            if (completion_fd in inherited_fds or not validate_receipt_descriptor(completion_fd, identifier, completion_nonce)):
                raise ProcessError("invalid_launch")

    async def launch(
        self, executable: Path, args: list[str], cwd: Path, env: dict[str, str], *,
        instance_id: str | None = None, io_mode: str = "streams",
        inherited_fds: tuple[int, ...] = (), startup_timeout: float = 10,
        stop_grace: float = 2, completion_fd: int | None = None,
        completion_nonce: str | None = None, owned_group: bool = False,
        nonlaunch_callback=None, expected_reaper: Path | None = None,
        graph_role: str | None = None,
        admit: Callable[[], bool] | None = None,
    ) -> OwnedProcess:
        if nonlaunch_callback is not None and not callable(nonlaunch_callback):
            raise ProcessError("invalid_launch")
        identifier = instance_id or uuid.uuid4().hex
        launch_status = {"attempted": False, "not_started": False}
        graph_reserved = False
        try:
            if admit is not None and not callable(admit):
                raise ProcessError("launch_not_admitted")
            if self.graph_accounting is not None:
                self.graph_accounting.reserve(graph_role, identifier, executable)
                graph_reserved = True
            elif graph_role is not None:
                raise ProcessError("invalid_process_graph")
            self._preflight(identifier, io_mode, inherited_fds, startup_timeout, stop_grace,
                            completion_fd, completion_nonce, owned_group)
            if expected_reaper is not None and Path(expected_reaper) != self.reaper:
                raise ProcessError("native_reaper_changed")
            inputs = (Path(executable).absolute(), list(args), Path(cwd).absolute(), dict(env),
                      identifier, io_mode, tuple(inherited_fds), startup_timeout, stop_grace,
                      completion_fd, completion_nonce, owned_group, launch_status,
                      None if expected_reaper is None else Path(expected_reaper),
                      time.monotonic() + startup_timeout if graph_reserved else None, admit)
            if graph_reserved:
                verification = asyncio.create_task(asyncio.to_thread(self.graph_accounting.verify_executable, graph_role))
                self._graph_checks.add(verification)
                try:
                    await asyncio.shield(verification)
                finally:
                    await _await_ownership(asyncio.gather(verification, return_exceptions=True))
                    self._graph_checks.discard(verification)
                self._check_graph_admission(inputs[-2])
                self._check_admit(admit)
        except BaseException:
            if graph_reserved:
                self.graph_accounting.record(identifier, "nonlaunch")
            # No native creation task exists at this point. This proof is
            # independent of any unrelated instance still owned by the host.
            if nonlaunch_callback is not None:
                nonlaunch_callback()
            raise
        # Shield creation through acknowledgment so cancellation cannot orphan a
        # subprocess between OS spawn and Python receiving its owned handle.
        self._pending_ids.add(identifier)
        task = asyncio.create_task(self._launch(*inputs))
        self._launches.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                process = await _await_ownership(task)
            except ProcessError:
                pass
            else:
                await _await_ownership(process.stop())
            raise
        except BaseException:
            if graph_reserved:
                self.graph_accounting.record(identifier, "nonlaunch" if launch_status["not_started"] or not launch_status["attempted"] else "unknown")
            raise
        finally:
            self._pending_ids.discard(identifier)
            self._launches.discard(task)
            if graph_reserved and (not launch_status["attempted"] or launch_status["not_started"]):
                self.graph_accounting.record(identifier, "nonlaunch")
            if nonlaunch_callback is not None and (not launch_status["attempted"] or launch_status["not_started"]):
                nonlaunch_callback()

    async def _launch(
        self, executable: Path, args: list[str], cwd: Path, env: dict[str, str],
        identifier: str, io_mode: str, inherited_fds: tuple[int, ...],
        startup_timeout: float, stop_grace: float,
        completion_fd: int | None, completion_nonce: str | None,
        owned_group: bool,
        launch_status: dict,
        expected_reaper: Path | None = None,
        graph_deadline: float | None = None,
        admit: Callable[[], bool] | None = None,
    ) -> OwnedProcess:
        if any(not isinstance(v, str) or "\0" in v for v in args):
            raise ProcessError("invalid_launch")
        if expected_reaper is not None and self.reaper != expected_reaper:
            raise ProcessError("native_reaper_changed")
        environment = []
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str) or not key or "=" in key or "\0" in key + value:
                raise ProcessError("invalid_launch")
            if key.startswith(("MEECP_", "MCPME_")) and any(part in key for part in ("LAUNCH", "CONTROL", "SPEC")):
                raise ProcessError("invalid_launch")
            environment.append(f"{key}={value}")
        if len(args) > 4096 or len(environment) > 4096 or len(inherited_fds) > 64:
            raise ProcessError("invalid_launch")
        if any(type(fd) is not int or fd < 3 for fd in inherited_fds):
            raise ProcessError("invalid_launch")
        try:
            for fd in inherited_fds:
                os.fstat(fd)
        except OSError:
            raise ProcessError("invalid_launch") from None
        spec_parent, spec_child = socket.socketpair()
        control_parent, control_child = socket.socketpair()
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        process = None
        try:
            payload = bytearray((b"MCPME004" if completion_fd is not None else b"MCPME003") if owned_group
                                else (b"MCPME002" if completion_fd is not None else b"MCPME001"))
            def number(value: int) -> None:
                payload.extend(struct.pack("!I", value))
            def string(value: str) -> None:
                data = value.encode("utf-8")
                number(len(data))
                payload.extend(data)
            if completion_fd is not None:
                string(identifier); string(completion_nonce)
            for value in (int(stop_grace * 1000), input_read, output_write, len(inherited_fds)):
                number(value)
            for fd in inherited_fds:
                number(fd)
            string(str(executable)); string(str(cwd)); number(len(args))
            for value in args:
                string(value)
            number(len(environment))
            for value in environment:
                string(value)
            if len(payload) > 1024 * 1024:
                raise ProcessError("invalid_launch")
            if self.graph_accounting is not None:
                self._check_graph_admission(graph_deadline)
                self.graph_accounting.check_launch(identifier)
            self._check_admit(admit)
            if self._closed:
                raise ProcessError("owner_closed")
            if expected_reaper is not None and self.reaper != expected_reaper:
                raise ProcessError("native_reaper_changed")
            launch_status["attempted"] = True
            try:
                wrapper = await asyncio.create_subprocess_exec(
                    str(self.reaper if expected_reaper is None else expected_reaper),
                    str(spec_child.fileno()), str(control_child.fileno()), str(os.getpid()),
                    *([str(completion_fd)] if completion_fd is not None else []),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, env={},
                    pass_fds=(spec_child.fileno(), control_child.fileno(), input_read, output_write, *inherited_fds,
                              *((completion_fd,) if completion_fd is not None else ())),
                )
            except (FileNotFoundError, PermissionError):
                # POSIX exec failure means the requested native wrapper never
                # ran. Other creation errors and cancellation remain unknown.
                launch_status["not_started"] = True
                raise
            control_parent.setblocking(False)
            process = OwnedProcess(self, identifier, wrapper, control_parent, input_write, output_read, stop_grace)
            self.instances[identifier] = process
            spec_child.close(); control_child.close()
            os.close(input_read); input_read = -1
            os.close(output_write); output_write = -1
            spec_parent.setblocking(False)
            async with asyncio.timeout(startup_timeout):
                await asyncio.get_running_loop().sock_sendall(spec_parent, payload)
                spec_parent.close()
                spawned = await asyncio.shield(process._spawned)
            if not spawned:
                await process.wait()
                raise ProcessError("launch_failed")
            if self._closed:
                await process.stop()
                raise ProcessError("owner_closed")
            if io_mode == "streams":
                await process._streams()
            return process
        except asyncio.CancelledError:
            if process is not None:
                await _await_ownership(process.stop())
            raise
        except (OSError, TimeoutError, ProcessError, ValueError, UnicodeError):
            if process is not None:
                await _await_ownership(process.stop())
            raise ProcessError("launch_failed") from None
        finally:
            spec_parent.close(); spec_child.close(); control_child.close()
            for fd in (input_read, output_write):
                if fd >= 0:
                    os.close(fd)
            if process is None:
                control_parent.close()
                os.close(input_write); os.close(output_read)

    async def close(self) -> None:
        self._closed = True
        if self._graph_checks:
            await _await_ownership(asyncio.gather(*list(self._graph_checks), return_exceptions=True))
        if self._launches:
            await _await_ownership(asyncio.gather(*list(self._launches), return_exceptions=True))
        results = await _await_ownership(asyncio.gather(
            *(process.stop() for process in list(self.instances.values())), return_exceptions=True,
        ))
        if any(isinstance(result, OwnershipLost) for result in results):
            raise OwnershipLost()
        if any(isinstance(result, BaseException) for result in results):
            raise ProcessError("reap_timeout")
