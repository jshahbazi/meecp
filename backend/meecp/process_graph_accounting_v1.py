"""Trusted fixed foreground graph accounting; never a descendant resource cap."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def _require(value):
    if not value:
        raise ValueError("process_graph_contract_invalid")


@dataclass(frozen=True)
class GraphMemberV1:
    role: str
    executable: Path
    executable_sha256: str
    declared_memory_bytes: int

    def __post_init__(self):
        _require(type(self.role) is str and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.role))
        _require(isinstance(self.executable, Path) and self.executable.is_absolute())
        _require(type(self.executable_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", self.executable_sha256))
        _require(type(self.declared_memory_bytes) is int and 1 <= self.declared_memory_bytes <= 2**40)


@dataclass(frozen=True)
class GraphMemberReportV1:
    role: str
    state: str
    terminal_foreground_rss_max_bytes: int | None


@dataclass(frozen=True)
class GraphAccountingReportV1:
    contract_digest: str
    members: tuple[GraphMemberReportV1, ...]
    declared_memory_bytes: int
    conservative_member_rss_max_sum_bytes: int | None
    ownership_settled: bool
    foreground_measurements_complete: bool
    ownership_lost: bool
    incomplete: bool
    lifetime_descendant_coverage_complete: bool = False
    hard_aggregate_enforced: bool = False


class FixedProcessGraphV1:
    """Construct only in a trusted factory; no API/package deserialization path.

    One execution per declared role. The owner records reservations before
    native creation. Missing/unknown members never become settlement evidence.
    This in-memory report cannot certify durable recovery or replace receipts.
    """
    def __init__(self, package_digest: str, authority_digest: str, members: tuple[GraphMemberV1, ...]):
        _require(all(type(v) is str and re.fullmatch(r"[0-9a-f]{64}", v) for v in (package_digest, authority_digest)))
        _require(type(members) is tuple and 1 <= len(members) <= 16 and all(type(m) is GraphMemberV1 for m in members))
        _require(len({m.role for m in members}) == len(members))
        self._members = {m.role: m for m in members}
        self._records: dict[str, tuple[str, str, int | None]] = {}
        self._verified = {}
        self._owner = None
        self._contract_digest = hashlib.sha256(json.dumps(["fixed-process-graph-v1", package_digest, authority_digest,
            [(m.role, str(m.executable), m.executable_sha256, m.declared_memory_bytes) for m in members]],
            separators=(",", ":")).encode()).hexdigest()

    @property
    def contract_digest(self):
        return self._contract_digest

    def bind_owner(self, owner):
        _require(self._owner is None)
        self._owner = owner

    def reserve(self, role, identifier, executable):
        _require(type(role) is str and role in self._members and role not in self._records and
                 type(identifier) is str and re.fullmatch(r"[0-9a-f]{32}", identifier) and
                 all(record[0] != identifier for record in self._records.values()))
        _require(Path(executable).absolute() == self._members[role].executable)
        self._records[role] = (identifier, "reserved", None)

    def verify_executable(self, role):
        member = self._members[role]
        try:
            fd = os.open(member.executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                before = os.fstat(fd)
                _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                         0 < before.st_size <= 64 * 1024**2 and before.st_mode & 0o111)
                digest = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    block = os.read(fd, min(65536, remaining))
                    _require(block)
                    remaining -= len(block); digest.update(block)
                _require(not os.read(fd, 1) and self._identity(before) == self._identity(os.fstat(fd)) and
                         digest.hexdigest() == member.executable_sha256)
                self._verified[role] = self._identity(before)
            finally:
                os.close(fd)
        except (OSError, ValueError):
            raise ValueError("process_graph_executable_mismatch") from None

    @staticmethod
    def _identity(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)

    def check_launch(self, identifier):
        for role, (current, state, _) in self._records.items():
            if current == identifier:
                _require(state == "reserved" and self._identity(self._members[role].executable.lstat()) == self._verified.get(role))
                return
        _require(False)

    def record(self, identifier, state, peak=None):
        _require(state in {"active", "settled", "nonlaunch", "unknown"})
        for role, (current, prior, _) in self._records.items():
            if current == identifier:
                if prior in {"settled", "nonlaunch", "unknown"}:
                    return
                if state == "settled" and not (type(peak) is int and 0 < peak <= 2**63 - 1):
                    peak = None
                self._records[role] = (current, state, peak if state == "settled" else None)
                return
        _require(False)

    def report(self):
        members = tuple(GraphMemberReportV1(role, self._records.get(role, ("", "missing", None))[1],
            self._records.get(role, ("", "missing", None))[2]) for role in self._members)
        settled = all(m.state in {"settled", "nonlaunch"} for m in members)
        measured = all(m.state == "settled" and m.terminal_foreground_rss_max_bytes is not None for m in members)
        total = sum(m.terminal_foreground_rss_max_bytes for m in members) if measured else None
        if total is not None and total > 2**63 - 1:
            total, measured = None, False
        return GraphAccountingReportV1(self.contract_digest, members,
            sum(m.declared_memory_bytes for m in self._members.values()),
            total,
            settled, measured, any(m.state == "unknown" for m in members), not (settled and measured))
