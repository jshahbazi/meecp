"""Owned, bounded execution of host-selected preparation steps.

Only reviewed foreground toolchain steps may use this facility. Descendants
must remain in the dedicated process group. It does not admit arbitrary
recipe workers, daemonizing tools, or package lifecycle scripts.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .preparation_contracts import PreparationError
from .processes import OwnedProcess, OwnershipLost, ProcessError, ProcessOwner, _await_ownership, _watch_ownership


@dataclass(frozen=True)
class StepResult:
    stdout: bytes
    returncode: int
    peak_memory_bytes: int


async def _settle(process: OwnedProcess) -> None:
    """Repeated caller cancellation cannot abandon the native ownership wait."""
    await _await_ownership(process.stop())


class PreparationExecutor:
    def __init__(self, reaper: Path, *, owner: ProcessOwner | None = None):
        self.owner = owner if owner is not None else ProcessOwner(reaper)
        self.closed = False

    async def execute(
        self, executable: Path, args: list[str], cwd: Path, env: dict[str, str], *,
        timeout_seconds: float, stdout_limit: int = 65536,
    ) -> StepResult:
        """Consume bounded transient output, discard stderr, and reap on exit.

        A deadline includes spawn acknowledgement and output drain. No partial
        output is exposed on failure, timeout or cancellation. Ownership loss
        remains distinct and keeps the ProcessOwner reservation in place.
        """
        if (self.closed or type(stdout_limit) is not int or not 0 <= stdout_limit <= 1024 * 1024
                or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= 3600):
            raise PreparationError("invalid_preparation_step")
        process = None
        try:
            async with asyncio.timeout(timeout_seconds):
                process = await self.owner.launch(
                    executable, args, cwd, env, owned_group=True,
                    startup_timeout=min(10, timeout_seconds), stop_grace=0.1,
                )
                assert process.stdout is not None and process.stdin is not None
                process.stdin.close()
                async def consume():
                    output = bytearray()
                    while True:
                        chunk = await process.stdout.read(min(65536, stdout_limit - len(output) + 1))
                        if not chunk:
                            break
                        if len(output) + len(chunk) > stdout_limit:
                            raise PreparationError("preparation_output_limit")
                        output.extend(chunk)
                    return output, await process.wait()
                output, code = await _watch_ownership(process, consume())
                if code:
                    raise PreparationError("preparation_step_failed")
                if process.peak_memory_bytes is None or process.peak_memory_bytes <= 0:
                    raise PreparationError("preparation_step_measurement_unavailable")
                return StepResult(bytes(output), code, process.peak_memory_bytes)
        except TimeoutError:
            raise PreparationError("preparation_step_timeout") from None
        except OwnershipLost:
            raise
        except ProcessError:
            raise PreparationError("preparation_step_launch_failed") from None
        finally:
            if process is not None:
                await _settle(process)

    async def close(self) -> None:
        self.closed = True
        await _await_ownership(self.owner.close())
