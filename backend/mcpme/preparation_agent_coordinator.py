"""Durable proposal attempts; no source execution, installation or attachment.

The host must own the state directory and validate_current callback. Backends
are trusted implementations, not extensions supplied by imported source.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Callable
import uuid

from .preparation_agent_backend import AgentBackend, AgentReply
from .preparation_agent_contracts import AnalysisRequestV1, AgentProposalV1, TaskProposalV1
from .preparation_contracts import PreparationError, canonical_bytes

TERMINAL = frozenset({"succeeded", "canceled", "failed", "interrupted"})
MAX_ATTEMPTS = 1024
MAX_PENDING = 4


def _private_file(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise PreparationError("agent_state_file_invalid")
    return fd


class AgentAttemptStore:
    """Single-host journal, with durable local cancellation and stale-result fences.

    Remote usage is unknown after a disconnect/cancel/crash. Its full reservation
    stays charged to the attempt; interruption never retries provider calls.
    Only typed requested artifacts are stored, never raw provider responses.
    """
    def __init__(self, root: Path):
        root = Path(root)
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PreparationError("agent_state_directory_invalid")
        self._lock = _private_file(root / "owner.lock")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock)
            raise PreparationError("agent_coordinator_already_owned") from None
        database = root / "attempts.sqlite3"
        try:
            fd = _private_file(database)
            os.close(fd)
            self.db = sqlite3.connect(database, isolation_level=None)
        except BaseException:
            os.close(self._lock)
            raise
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        with self.transaction():
            for row in self.db.execute("SELECT data FROM attempts").fetchall():
                value = json.loads(row["data"])
                if value["state"] not in TERMINAL:
                    value.update(state="interrupted", generation=str(uuid.uuid4()),
                                 local_io_settled=True, error_code="agent_host_restarted")
                    self.save(value)

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def transaction():
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return transaction()

    def close(self):
        self.db.close()
        os.close(self._lock)

    def get(self, attempt_id):
        row = self.db.execute("SELECT data FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise PreparationError("agent_attempt_not_found")
        return json.loads(row["data"])

    def save(self, value):
        value["updated_at"] = time.time()
        self.db.execute("UPDATE attempts SET data=? WHERE id=?",
                        (canonical_bytes(value).decode(), value["attempt_id"]))

    def create(self, request):
        with self.transaction():
            if self.db.execute("SELECT count(*) FROM attempts").fetchone()[0] >= MAX_ATTEMPTS:
                raise PreparationError("agent_attempt_limit")
            # One reviewed authority starts at most one attempt. Retry requires a
            # newly reviewed authority; an interrupted call cannot be replayed.
            for row in self.db.execute("SELECT data FROM attempts").fetchall():
                old = json.loads(row["data"])
                if old["authority_id"] == request.authority_id:
                    raise PreparationError("agent_authority_consumed")
            pending = sum(json.loads(row["data"])["state"] not in TERMINAL
                          for row in self.db.execute("SELECT data FROM attempts"))
            if pending >= MAX_PENDING:
                raise PreparationError("agent_pending_limit")
            value = {"attempt_id": str(uuid.uuid4()), "generation": str(uuid.uuid4()),
                     "request_digest": request.digest, "authority_id": request.authority_id,
                     "import_id": request.import_id, "revision": request.revision,
                     "source_revision": request.source_revision, "state": "queued",
                     "created_at": time.time(), "updated_at": time.time(), "calls": [],
                     "local_io_settled": True, "error_code": None, "proposal_digest": None}
            self.db.execute("INSERT INTO attempts VALUES (?,?)",
                            (value["attempt_id"], canonical_bytes(value).decode()))
        return value

    def check(self, attempt_id, generation, request):
        value = self.get(attempt_id)
        if (value["generation"] != generation or value["request_digest"] != request.digest
                or value["state"] != "running"):
            raise PreparationError("agent_stale_attempt")
        return value

    def reserve_call(self, attempt_id, generation, request, role, size):
        with self.transaction():
            value = self.check(attempt_id, generation, request)
            calls = value["calls"]
            if len(calls) >= request.limits.max_calls or any(call["role"] == role for call in calls):
                raise PreparationError("agent_call_limit")
            if sum(call["transfer_bytes"] for call in calls) + size > request.limits.max_transfer_bytes:
                raise PreparationError("agent_transfer_limit")
            calls.append({"role": role, "transfer_bytes": size,
                          "reserved_output_tokens": request.limits.max_output_tokens,
                          "usage": None})
            self.save(value)

    def account(self, attempt_id, generation, request, role, usage):
        with self.transaction():
            value = self.check(attempt_id, generation, request)
            call = next(item for item in value["calls"] if item["role"] == role)
            if call["usage"] is not None:
                raise PreparationError("agent_usage_duplicate")
            call["usage"] = usage.model_dump()
            self.save(value)

    def finish(self, attempt_id, generation, state, code=None):
        with self.transaction():
            value = self.get(attempt_id)
            if value["generation"] != generation or value["state"] in TERMINAL:
                return
            if value["state"] == "cancel_requested":
                state, code = "canceled", None
            value.update(state=state, error_code=code, local_io_settled=True)
            self.save(value)

    def cancel(self, attempt_id):
        with self.transaction():
            value = self.get(attempt_id)
            if value["state"] in TERMINAL:
                return value
            value["state"] = "canceled" if value["state"] == "queued" else "cancel_requested"
            self.save(value)
        return value

    def publish(self, request, result, validate_current):
        with self.transaction():
            value = self.check(result.attempt_id, result.generation, request)
            validate_current(request)
            if time.time() >= request.expires_at:
                raise PreparationError("agent_authority_expired")
            data = canonical_bytes(result.model_dump())
            if len(data) > request.limits.max_artifact_bytes:
                raise PreparationError("agent_artifact_limit")
            self.db.execute("INSERT INTO artifacts VALUES (?,?)", (result.attempt_id, data.decode()))
            value.update(state="succeeded", proposal_digest=result.digest, local_io_settled=True)
            self.save(value)

    def proposal(self, attempt_id, request, validate_current):
        """Host must revalidate again before deriving or admitting any candidate."""
        validate_current(request)
        value = self.get(attempt_id)
        if value["state"] != "succeeded" or value["request_digest"] != request.digest:
            raise PreparationError("agent_proposal_unavailable")
        row = self.db.execute("SELECT data FROM artifacts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise PreparationError("agent_proposal_unavailable")
        result = AgentProposalV1.model_validate(json.loads(row["data"]))
        if (result.digest != value["proposal_digest"] or result.attempt_id != attempt_id
                or result.generation != value["generation"] or result.request_digest != request.digest):
            raise PreparationError("agent_proposal_identity_mismatch")
        return result


class AgentCoordinator:
    def __init__(self, store: AgentAttemptStore, backend: AgentBackend,
                 validate_current: Callable[[AnalysisRequestV1], None]):
        self.store, self.backend, self.validate_current = store, backend, validate_current
        self._running: dict[str, asyncio.Task] = {}

    def _authority(self, request):
        if time.time() >= request.expires_at:
            raise PreparationError("agent_authority_expired")
        if self.backend.backend_id != request.provider.backend:
            raise PreparationError("agent_provider_mismatch")
        self.validate_current(request)

    def create(self, request):
        request = AnalysisRequestV1.model_validate(request.model_dump())
        self._authority(request)
        return self.store.create(request)

    async def cancel(self, attempt_id):
        value = self.store.cancel(attempt_id)
        task = self._running.get(attempt_id)
        if task is not None and not task.done() and value["state"] == "cancel_requested":
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.store.get(attempt_id)

    async def run(self, attempt_id, request, source):
        # Freeze caller-owned nested lists and source bytes before first await.
        request = AnalysisRequestV1.model_validate(request.model_dump())
        selected = request.selected_source(dict(source))
        self._authority(request)
        with self.store.transaction():
            value = self.store.get(attempt_id)
            if value["state"] != "queued" or value["request_digest"] != request.digest:
                raise PreparationError("agent_stale_attempt")
            value.update(state="running", local_io_settled=False)
            self.store.save(value)
        generation = value["generation"]
        self._running[attempt_id] = asyncio.current_task()
        children = []
        failure = None
        semaphore = asyncio.Semaphore(request.limits.max_parallel)

        async def execute(role, prior):
            async with semaphore:
                self._authority(request)
                self.store.check(attempt_id, generation, request)
                body = self.backend.encode(request, role, selected, prior)
                self.store.reserve_call(attempt_id, generation, request, role, len(body))
                reply = await self.backend.complete(request, role, body)
                self._authority(request)
                if not isinstance(reply, AgentReply):
                    raise PreparationError("agent_backend_reply_invalid")
                proposal = TaskProposalV1.model_validate(reply.proposal.model_dump())
                if proposal.role != role or any(not file.path.startswith(f"proposed/{role}/")
                                                for file in proposal.files):
                    raise PreparationError("agent_proposal_scope_invalid")
                if reply.usage.output_tokens > request.limits.max_output_tokens:
                    raise PreparationError("agent_provider_usage_invalid")
                self.store.account(attempt_id, generation, request, role, reply.usage)
                return proposal

        try:
            async with asyncio.timeout(request.limits.wall_seconds):
                children = [asyncio.create_task(execute(role, []))
                            for role in ("interface", "dependencies", "fixtures")]
                proposals = await asyncio.gather(*children)
                adapter = await execute("adapter", proposals)
                result = AgentProposalV1(request_digest=request.digest, attempt_id=attempt_id,
                                         generation=generation, tasks=[*proposals, adapter])
                self.store.publish(request, result, self.validate_current)
                return result
        except asyncio.CancelledError:
            self.store.cancel(attempt_id)
            raise
        except TimeoutError:
            failure = "agent_deadline"
            raise PreparationError("agent_deadline") from None
        except Exception:
            # Error values supplied by backend/plugin code cannot enter logs or
            # status. Only this module's fixed public failure code is persisted.
            failure = "agent_attempt_failed"
            raise PreparationError("agent_attempt_failed") from None
        finally:
            for task in children:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            if failure is not None:
                self.store.finish(attempt_id, generation, "failed", failure)
            current = self.store.get(attempt_id)
            if current["state"] == "cancel_requested":
                self.store.finish(attempt_id, generation, "canceled")
            self._running.pop(attempt_id, None)
