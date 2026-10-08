"""Native consent for local management integrations, over a private leased channel.

Profiles remember an explicit macOS-account consent policy. Reported MCP client
names are display metadata, never application identity. Bearers are published
once to their bound connection; existing grant stores remain the scope engine.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass, field
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import sqlite3
import struct
import threading
import time
import uuid
import unicodedata

from mcpme_local_transport import bind_private_listener, checked_socket, socket_path
from .grants import GrantError, plan_digest
from .private import check_private_file, read_private_json, write_private_json

SCOPES = ("inspect", "plan")
PACKAGE_ACTIONS = frozenset({"install", "upgrade"})
LEDGER_ACTIONS = PACKAGE_ACTIONS | {"base"}
REQUEST_TTL = 120
HANDOFF_TTL = 10
SESSION_TTL = 3600
MAX_PENDING = 32
MAX_PROFILES = 4096
FRAME_BYTES = 8192


def identity(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (TypeError, ValueError, AttributeError):
        raise GrantError("authorization_invalid_request", 400) from None


def scopes(value):
    if (type(value) is not list or not value or any(type(item) is not str for item in value)
            or len(set(value)) != len(value) or set(value) - set(SCOPES) or "inspect" not in value):
        raise GrantError("authorization_invalid_scope", 400)
    return [item for item in SCOPES if item in value]


def scope_digest(value):
    return hashlib.sha256(json.dumps({"schema_version": 1, "scopes": value}, separators=(",", ":")).encode()).hexdigest()


def label(value, *, maximum=128):
    if (type(value) is not str or not 1 <= len(value.encode("utf-8")) <= maximum
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise GrantError("authorization_invalid_request", 400)
    return value


def process_start(pid):
    """Read only OS process start identity; never command lines or arguments."""
    if platform.system() == "Darwin":
        # proc_bsdinfo layout from the macOS SDK, PROC_PIDTBSDINFO=3.
        class BsdInfo(ctypes.Structure):
            _fields_ = [("ids", ctypes.c_uint32 * 12), ("comm", ctypes.c_char * 16),
                        ("name", ctypes.c_char * 32), ("status", ctypes.c_uint32 * 6),
                        ("seconds", ctypes.c_uint64), ("micros", ctypes.c_uint64)]
        value = BsdInfo()
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        read = library.proc_pidinfo(pid, 3, ctypes.c_uint64(0), ctypes.byref(value), ctypes.sizeof(value))
        if read != ctypes.sizeof(value) or value.ids[3] != pid:
            raise GrantError("authorization_peer_unavailable", 401)
        return (value.seconds, value.micros)
    try:
        # Fields following the comm parentheses begin at field 3; starttime is 22.
        data = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(data[19]),)
    except (OSError, ValueError, IndexError):
        raise GrantError("authorization_peer_unavailable", 401) from None


def peer_identity(channel):
    if platform.system() == "Darwin":
        uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
        library = ctypes.CDLL(None)
        if library.getpeereid(channel.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise GrantError("authorization_peer_unavailable", 401)
        pid = struct.unpack("i", channel.getsockopt(0, 2, 4))[0]  # SOL_LOCAL, LOCAL_PEERPID
        observed_uid = uid.value
    else:
        pid, observed_uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if observed_uid != os.getuid() or pid <= 0:
        raise GrantError("authorization_peer_unavailable", 401)
    return pid, process_start(pid)


@dataclass
class Lease:
    lease_id: str
    pid: int
    start: tuple
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    current: str | None = None
    base: str | None = None
    grant_ids: set[str] = field(default_factory=set)
    closed: bool = False

    def live(self):
        if self.closed or self.reader.at_eof() or self.writer.is_closing():
            return False
        try:
            return process_start(self.pid) == self.start
        except GrantError:
            return False


class AuthorizationBroker:
    def __init__(self, host):
        self.host, self.db = host, host.registry._connection
        self.registry_thread = threading.get_ident()
        registry_details = host.registry.path.lstat()
        self.registry_identity = (registry_details.st_dev, registry_details.st_ino)
        self.generation = str(uuid.uuid4())
        self.server = None
        self.closing = False
        self.leases: dict[str, Lease] = {}
        self.requests: dict[str, dict] = {}
        self.credentials: dict[str, dict] = {}
        self.attempts = defaultdict(deque)
        self.tasks = set()
        self.manager_endpoint = None
        self.listener_identity = None
        path = host.root / "authorization-installation.json"
        state_digest = hashlib.sha256(os.fsencode(str(host.root.resolve()))).hexdigest()
        if path.exists() or path.is_symlink():
            installation = read_private_json(path, max_bytes=1024)
            if (set(installation) != {"schema_version", "installation_id", "account_uid", "state_digest"}
                    or type(installation["schema_version"]) is not int or installation["schema_version"] != 1
                    or installation["account_uid"] != os.getuid() or installation["state_digest"] != state_digest):
                raise GrantError("authorization_state_unavailable", 503)
            self.installation_id = identity(installation["installation_id"])
        else:
            self.installation_id = str(uuid.uuid4())
            write_private_json(path, {"schema_version": 1, "installation_id": self.installation_id,
                                      "account_uid": os.getuid(), "state_digest": state_digest})
        with host.registry.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS authorization_profiles (profile_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS authorization_credentials (
                grant_id TEXT PRIMARY KEY, profile_id TEXT, revision INTEGER NOT NULL, generation TEXT NOT NULL,
                lease_id TEXT NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL, expires_at REAL NOT NULL)""")
            self.db.execute("DELETE FROM authorization_credentials WHERE expires_at<=?", (time.time(),))
        self.profiles = {}
        for row in self.db.execute("SELECT profile_id,data FROM authorization_profiles"):
            try:
                value = json.loads(row["data"])
                self._validate_profile(value)
                if value["profile_id"] != row["profile_id"]:
                    raise ValueError()
                self.profiles[value["profile_id"]] = value
            except (ValueError, KeyError, TypeError, GrantError):
                # Unsupported or damaged policies cannot automatically approve.
                continue
        for row in self.db.execute("SELECT * FROM authorization_credentials"):
            self.credentials[row["grant_id"]] = dict(row)
        host.grants.session_guard = self.guard
        host.workflow_grants.session_guard = self.guard

    def _validate_profile(self, value):
        expected = {"schema_version", "profile_id", "label", "identity_mode", "installation_id", "account_uid",
                    "scopes", "session_ttl_seconds", "revision", "created_at", "updated_at", "revoked_at", "approved"}
        if (type(value) is not dict or set(value) != expected or type(value["schema_version"]) is not int
                or value["schema_version"] != 1 or value["identity_mode"] != "account_scoped_integration"
                or value["installation_id"] != self.installation_id or value["account_uid"] != os.getuid()
                or type(value["approved"]) is not bool or type(value["revision"]) is not int or value["revision"] < 0
                or type(value["account_uid"]) is not int or type(value["session_ttl_seconds"]) is not int
                or value["session_ttl_seconds"] != SESSION_TTL
                or any(type(value[key]) not in {int, float} or not math.isfinite(value[key]) or value[key] < 0
                       for key in ("created_at", "updated_at"))
                or (value["revoked_at"] is not None and (type(value["revoked_at"]) not in {int, float}
                    or not math.isfinite(value["revoked_at"]) or value["revoked_at"] < 0))
                or type(value["scopes"]) is not list):
            raise ValueError()
        identity(value["profile_id"]);label(value["label"])
        if value["scopes"]:
            if scopes(value["scopes"]) != value["scopes"]:
                raise ValueError()
        elif value["approved"]:
            raise ValueError()

    def create_profile(self, payload):
        if type(payload) is not dict or set(payload) - {"label"}:
            raise GrantError("authorization_invalid_request", 400)
        if self.db.execute("SELECT count(*) FROM authorization_profiles").fetchone()[0] >= MAX_PROFILES:
            raise GrantError("authorization_profile_limit", 409)
        now = time.time()
        value = {"schema_version": 1, "profile_id": str(uuid.uuid4()), "label": label(payload.get("label", "Management integration")),
                 "identity_mode": "account_scoped_integration", "installation_id": self.installation_id, "account_uid": os.getuid(),
                 "scopes": [], "session_ttl_seconds": SESSION_TTL, "revision": 0, "created_at": now,
                 "updated_at": now, "revoked_at": None, "approved": False}
        self._save_profile(value)
        return {"profile": value}

    def _save_profile(self, value):
        self._validate_profile(value)
        with self.host.registry.transaction():
            self.db.execute("INSERT OR REPLACE INTO authorization_profiles VALUES (?,?)", (
                value["profile_id"], json.dumps(value, sort_keys=True, separators=(",", ":"))))
        self.profiles[value["profile_id"]] = value

    def _profile(self, profile_id):
        identity(profile_id)
        if profile_id not in self.profiles:
            raise GrantError("authorization_profile_unavailable", 404)
        return self.profiles[profile_id]

    def guard(self, grant_id, *, required=True):
        """Called by both existing ledgers, including exact job/operation checks."""
        record = self.credentials.get(grant_id)
        if record is None:
            if required:
                raise GrantError("grant_revoked", 401)
            return  # Explicit file grants preserve their existing lifetime.
        lease = self.leases.get(record["lease_id"])
        if (self.closing or record["generation"] != self.generation or not record["active"]
                or lease is None or not lease.live()):
            raise GrantError("grant_revoked", 401)
        if record["expires_at"] <= time.time():
            raise GrantError("grant_expired", 401)
        # Consent associations cannot revive capabilities revoked through the
        # owner's grant API while package review work was yielding. Read the
        # durable row directly to avoid recursively invoking the session guard.
        ledger = self.host.grants if record["kind"] in LEDGER_ACTIONS else self.host.workflow_grants
        try:
            row = self._guard_row(ledger, grant_id)
        except GrantError:
            raise GrantError("grant_revoked", 401) from None
        if row["revoked"]:
            raise GrantError("grant_revoked", 401)
        if row["expires_at"] <= time.time():
            raise GrantError("grant_expired", 401)
        if record["profile_id"] is not None:
            profile = self.profiles.get(record["profile_id"])
            if profile is None or profile["revoked_at"] is not None or profile["revision"] != record["revision"]:
                raise GrantError("grant_revoked", 401)
        if record["kind"] != "base":
            if lease.base is None:
                raise GrantError("grant_revoked", 401)
            base = self.requests[lease.base].get("grant_id")
            if base is None:
                raise GrantError("grant_revoked", 401)
            self.guard(base)

    def _guard_row(self, ledger, grant_id):
        if ledger is not self.host.grants or threading.get_ident() == self.registry_thread:
            return ledger._row(grant_id)
        # Preparation admission and authority monitors hold the imports lock on
        # worker threads. Never send a synchronous guard back to the host loop,
        # which may be waiting for that lock, or share its writable connection.
        # A short-lived read-only connection sees current durable base revocation
        # and expiry without weakening the registry's thread affinity.
        path = self.host.registry.path
        try:
            check_private_file(path)
            before = path.lstat()
            if (before.st_dev, before.st_ino) != self.registry_identity:
                raise ValueError()
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                         timeout=0.25, isolation_level=None)) as database:
                database.row_factory = sqlite3.Row
                row = database.execute("SELECT revoked,expires_at FROM management_grants WHERE grant_id=?",
                                       (grant_id,)).fetchone()
                check_private_file(path)
                after = path.lstat()
                if (after.st_dev, after.st_ino) != self.registry_identity or row is None:
                    raise ValueError()
                return row
        except (OSError, ValueError, sqlite3.Error):
            raise GrantError("grant_revoked", 401) from None

    async def start(self, manager_endpoint):
        if self.server is not None or self.closing:
            raise GrantError("authorization_unavailable", 503)
        self.manager_endpoint = manager_endpoint
        listener = bind_private_listener(self.host.root, "authorization")
        details = Path(listener.getsockname()).lstat()
        self.listener_identity = (details.st_dev, details.st_ino)
        try:
            self.server = await asyncio.start_unix_server(self._connected, sock=listener, limit=FRAME_BYTES)
            write_private_json(self.host.root / "authorization-bootstrap.json", {"schema_version": 1,
                "socket_path": str(socket_path(self.host.root, "authorization")), "installation_id": self.installation_id,
                "generation": self.generation, "supervisor_pid": os.getpid(), "manager_endpoint": manager_endpoint})
        except BaseException:
            listener.close()
            if self.server is not None:
                self.server.close()
            raise

    async def _connected(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        lease = None
        try:
            if self.closing:
                return
            pid, start = peer_identity(writer.get_extra_info("socket"))
            lease = Lease(str(uuid.uuid4()), pid, start, reader, writer)
            self.leases[lease.lease_id] = lease
            while not self.closing and lease.live():
                try:
                    data = await asyncio.wait_for(reader.readline(), timeout=1)
                except TimeoutError:
                    self._settle_deadlines(lease)
                    continue
                if not data:
                    break
                if len(data) > FRAME_BYTES or not data.endswith(b"\n"):
                    break
                try:
                    def unique(pairs):
                        result = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError()
                            result[key] = value
                        return result
                    frame = json.loads(data, object_pairs_hook=unique)
                    install_effects = upgrade_effects = None
                    if (type(frame) is dict and type(frame.get("version")) is int and frame["version"] == 1
                            and frame.get("type") == "request_action" and frame.get("kind") in PACKAGE_ACTIONS):
                        # Package verification can read large sealed artifacts.
                        # Keep it off the host event loop and join its owned work
                        # during host shutdown before closing persistent state.
                        _, _, plan, _ = self._validate_action(lease, frame)
                        if frame["kind"] == "upgrade":
                            verified = await self.host.static_work(self._upgrade_review_data, plan)
                            upgrade_effects = self._upgrade_effects(plan, verified=verified)
                        else:
                            install_effects = await self.host.static_work(self._install_effects, plan)
                    response = self.frame(lease, frame, install_effects=install_effects, upgrade_effects=upgrade_effects)
                except (ValueError, TypeError, KeyError, GrantError) as error:
                    code = error.code if isinstance(error, GrantError) else "authorization_invalid_request"
                    response = {"version": 1, "status": "unavailable", "error": {"code": code}}
                except Exception:
                    response = {"version": 1, "status": "unavailable", "error": {"code": "authorization_unavailable"}}
                writer.write(json.dumps(response, allow_nan=False, separators=(",", ":")).encode() + b"\n")
                await asyncio.wait_for(writer.drain(), timeout=2)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            if lease is not None:
                try:
                    self.disconnect(lease.lease_id)
                except Exception:
                    lease.closed = True
                finally:
                    self.leases.pop(lease.lease_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except (Exception, asyncio.CancelledError):
                pass
            self.tasks.discard(task)

    def frame(self, lease, frame, *, install_effects=None, upgrade_effects=None):
        if (not lease.live() or type(frame) is not dict or type(frame.get("version")) is not int or frame["version"] != 1):
            raise GrantError("authorization_invalid_request", 400)
        self._settle_deadlines(lease)
        kind = frame.get("type")
        if kind == "request_access":
            self.request_access(lease, frame)
        elif kind == "request_action":
            self.request_action(lease, frame, install_effects=install_effects, upgrade_effects=upgrade_effects)
        elif kind == "ack":
            self.ack(lease, frame)
        elif kind == "renew":
            self.renew(lease, frame)
        elif kind == "status":
            if set(frame) != {"version", "type"}:
                raise GrantError("authorization_invalid_request", 400)
        else:
            raise GrantError("authorization_invalid_request", 400)
        return self.status(lease)

    def _rate(self, profile_id):
        now = time.time()
        self._prune()
        for key in ("account", profile_id or "anonymous"):
            values = self.attempts[key]
            while values and values[0] <= now - 60:
                values.popleft()
            if len(values) >= (64 if key == "account" else 6):
                raise GrantError("authorization_rate_limited", 429)
        self.attempts["account"].append(now)
        self.attempts[profile_id or "anonymous"].append(now)
        if sum(value["status"] == "pending" for value in self.requests.values()) >= MAX_PENDING:
            raise GrantError("authorization_queue_full", 429)

    def request_access(self, lease, frame):
        if set(frame) != {"version", "type", "profile_id", "client", "scopes"}:
            raise GrantError("authorization_invalid_request", 400)
        profile_id, requested = frame["profile_id"], scopes(frame["scopes"])
        profile = self._profile(profile_id) if profile_id is not None else None
        client = frame["client"]
        if type(client) is not dict or set(client) != {"name", "version"}:
            raise GrantError("authorization_invalid_request", 400)
        client = {"name": label(client["name"]), "version": label(client["version"], maximum=64)}
        current = self.requests.get(lease.current)
        if current is not None and current["status"] in {"pending", "approved"}:
            effective = current.get("selected_scopes", current["scopes"]) if current["status"] == "approved" else current["scopes"]
            if current["kind"] == "base" and current["profile_id"] == profile_id and effective == requested:
                return
            if current["status"] == "pending":
                raise GrantError("authorization_request_pending", 409)
        self._rate(profile_id)
        now = time.time()
        value = {"request_id": str(uuid.uuid4()), "lease_id": lease.lease_id, "profile_id": profile_id,
                 "client": client, "label": profile["label"] if profile else client["name"],
                 "identity_mode": "account_scoped_integration", "scopes": requested, "scope_digest": scope_digest(requested),
                 "policy_revision": profile["revision"] if profile else 0, "generation": self.generation,
                 "created_at": now, "expires_at": now + REQUEST_TTL, "kind": "base", "status": "pending"}
        if lease.base is not None:
            value["previous_base"] = lease.base
        self.requests[value["request_id"]] = value
        lease.current = value["request_id"]
        if (profile is not None and profile["approved"] and profile["revoked_at"] is None
                and set(requested) <= set(profile["scopes"])):
            self._prepare(value, requested)

    def _bound_request(self, request_id):
        identity(request_id)
        value = self.requests.get(request_id)
        if value is None:
            raise GrantError("authorization_request_unavailable", 404)
        lease = self.leases.get(value["lease_id"])
        if lease is None or not lease.live() or self.closing:
            self._deny(value, "revoked")
            raise GrantError("authorization_request_stale", 409)
        self._settle_deadlines(lease)
        if value["status"] != "pending" or value.get("grant_id") is not None:
            raise GrantError("authorization_request_stale", 409)
        if value["generation"] != self.generation:
            raise GrantError("authorization_request_stale", 409)
        if value["profile_id"] is not None and self._profile(value["profile_id"])["revision"] != value["policy_revision"]:
            raise GrantError("authorization_policy_changed", 409)
        return value, lease

    def approve(self, request_id, payload, *, upgrade_review=None):
        expected = {"remember", "account_scoped_consent", "scopes", "scope_digest", "policy_revision", "generation"}
        if (type(payload) is not dict or set(payload) != expected or type(payload["remember"]) is not bool
                or type(payload["account_scoped_consent"]) is not bool
                or type(payload["scopes"]) is not list or any(type(item) is not str for item in payload["scopes"])
                or type(payload["scope_digest"]) is not str or type(payload["generation"]) is not str
                or type(payload["policy_revision"]) is not int):
            raise GrantError("authorization_request_stale", 409)
        decision_digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        identity(request_id)
        previous = self.requests.get(request_id)
        if previous is not None and previous.get("decision_digest") == decision_digest:
            lease = self.leases.get(previous["lease_id"])
            if lease is not None:
                self._settle_deadlines(lease)
            if (lease is None or not lease.live() or self.closing or previous["status"] not in {"pending", "approved"}
                    or previous.get("grant_id") is None):
                raise GrantError("authorization_request_stale", 409)
            if previous["status"] == "approved":
                self.guard(previous["grant_id"])
            return {"request": self.summary(previous)}
        value, lease = self._bound_request(request_id)
        if (payload["scope_digest"] != value["scope_digest"] or payload["generation"] != self.generation
                or payload["policy_revision"] != value["policy_revision"]):
            raise GrantError("authorization_request_stale", 409)
        selected = scopes(payload["scopes"]) if value["kind"] == "base" else payload["scopes"]
        if not set(selected) <= set(value["scopes"]):
            raise GrantError("authorization_invalid_scope", 400)
        if value["kind"] != "base" and (selected or payload["remember"]):
            raise GrantError("authorization_invalid_request", 400)
        if payload["remember"]:
            if value["profile_id"] is None or not payload["account_scoped_consent"]:
                raise GrantError("authorization_remember_consent_required", 400)
            profile = {**self._profile(value["profile_id"]), "scopes": selected, "approved": True,
                       "revoked_at": None, "revision": value["policy_revision"] + 1, "updated_at": time.time()}
            if profile["label"] in {"Management integration", "External management integration"}:
                profile["label"] = value["client"]["name"]
            self._save_profile(profile)
            value["policy_revision"] = profile["revision"]
            value["label"] = profile["label"]
        if not lease.live():
            raise GrantError("authorization_request_stale", 409)
        self._prepare(value, selected, upgrade_review=upgrade_review)
        value["decision_digest"] = decision_digest
        return {"request": self.summary(value)}

    def _prepare(self, value, selected, *, upgrade_review=None):
        lease = self.leases[value["lease_id"]]
        if not lease.live() or value["status"] != "pending":
            raise GrantError("authorization_request_stale", 409)
        if value["kind"] == "base":
            credential = self.host.grants.issue({"mode": "prepare" if "plan" in selected else "read", "ttl_seconds": SESSION_TTL},
                                                export_credential=False)
        else:
            self.guard(self.requests[lease.base]["grant_id"])
            plan = self._action_plan(value["kind"], value["review"]["plan_id"])
            if self._action_digest(value["kind"], plan) != value["review"]["plan_digest"]:
                raise GrantError("grant_plan_changed", 409)
            if value["kind"] in PACKAGE_ACTIONS:
                if value["kind"] == "upgrade" and self._upgrade_effects(plan, verified=upgrade_review) != value["review"]["upgrade_effects"]:
                    raise GrantError("grant_plan_changed", 409)
                credential = self.host.grants.issue({"mode": value["kind"], "plan_id": plan["plan_id"]}, plan=plan, export_credential=False)
            else:
                credential = self.host.workflow_grants.issue({"mode": value["kind"] + "-execution", "plan_id": plan["plan_id"],
                    "plan_digest": value["review"]["plan_digest"], "idempotency_key": value["review"]["idempotency_key"]}, export_credential=False)
        record = {"grant_id": credential["grant_id"], "profile_id": value["profile_id"], "revision": value["policy_revision"],
                  "generation": self.generation, "lease_id": lease.lease_id, "kind": value["kind"], "active": 0,
                  "expires_at": credential["expires_at"]}
        try:
            with self.host.registry.transaction():
                self.db.execute("INSERT INTO authorization_credentials VALUES (?,?,?,?,?,?,?,?)", tuple(record.values()))
        except BaseException:
            (self.host.grants if value["kind"] in LEDGER_ACTIONS else self.host.workflow_grants).revoke(record["grant_id"])
            raise
        self.credentials[record["grant_id"]] = record
        lease.grant_ids.add(record["grant_id"])
        value.update(grant_id=record["grant_id"], selected_scopes=selected, handoff=credential,
                     handoff_expires_at=min(value["expires_at"], time.time() + HANDOFF_TTL), handoff_sent=False)

    def ack(self, lease, frame):
        value = self.requests.get(lease.current)
        if (set(frame) != {"version", "type", "request_id", "grant_id"} or value is None
                or frame["request_id"] != value["request_id"] or frame["grant_id"] != value.get("grant_id")):
            raise GrantError("authorization_request_stale", 409)
        if value["status"] == "approved":
            self.guard(frame["grant_id"])
            return
        if (value["status"] != "pending" or not value.get("handoff_sent")
                or time.time() >= value["handoff_expires_at"] or not lease.live()):
            self._deny(value, "expired")
            raise GrantError("authorization_request_stale", 409)
        record = self.credentials[value["grant_id"]]
        if value["profile_id"] is not None:
            profile = self._profile(value["profile_id"])
            if profile["revision"] != record["revision"] or profile["revoked_at"] is not None:
                self._deny(value, "revoked")
                raise GrantError("authorization_policy_changed", 409)
        with self.host.registry.transaction():
            self.db.execute("UPDATE authorization_credentials SET active=1 WHERE grant_id=?", (record["grant_id"],))
        record["active"] = 1
        ledger = self.host.grants if value["kind"] in LEDGER_ACTIONS else self.host.workflow_grants
        try:
            # Owner revocation or ledger expiry may race the one-time delivery.
            ledger._active(record["grant_id"])
        except GrantError:
            self._deny(value, "revoked")
            raise GrantError("authorization_request_stale", 409) from None
        value["status"] = "approved"
        value.pop("handoff", None)
        if value["kind"] == "base":
            lease.base = value["request_id"]
            if value.get("previous_base") is not None:
                self._deny(self.requests[value["previous_base"]], "revoked")

    def _action_plan(self, kind, plan_id):
        identity(plan_id)
        if kind == "upgrade":
            plan = self.host.read_upgrade_plan(plan_id)
            current = self.host.registry.get_slot(plan["slot_id"])
            self.host.check_mutation(current)
            if current.to_dict() != plan["old"]:
                raise GrantError("grant_plan_changed", 409)
            return plan
        if kind == "install":
            plan = read_private_json(self.host.root / "plans" / (plan_id + ".json"))
            if "import_id" in plan:
                self.host.reconcile_imports()
                self.host.imports.validate_binding(plan["import_id"], plan["import_revision"], plan["import_digest"])
            return plan
        with self.host.imports._lock:
            plan = self.host.workflow_grants._plan(kind, plan_id)
            if plan["expires_at"] <= time.time():
                raise GrantError("grant_plan_mismatch", 409)
            binding = plan if kind == "preparation" else plan["request"]
            try:
                draft = self.host.imports._get(binding["import_id"], binding["revision"])
                self.host.imports._mutable(draft)
            except ValueError:
                raise GrantError("grant_plan_changed", 409) from None
            if draft["source"]["revision"] != binding["source_revision"]:
                raise GrantError("grant_plan_changed", 409)
            self._action_digest(kind, plan)
            return plan

    @staticmethod
    def _action_digest(kind, plan):
        if kind in PACKAGE_ACTIONS:
            return plan_digest(plan)
        from .preparation_contracts import contract_digest
        domain = "preparation-execution-plan-v1" if kind == "preparation" else "preparation-resolution-plan-v1"
        digest = contract_digest(domain, {key: value for key, value in plan.items() if key not in {"job_id", "plan_digest"}})
        if digest != plan["plan_digest"]:
            raise GrantError("grant_plan_changed", 409)
        return digest

    def _install_effects(self, plan):
        # This reads the independently validated package and settings; an
        # untrusted bridge never provides the approval's effect summary.
        from .contracts import validate_package
        package = validate_package(Path(plan["package_path"]))
        if package.digest != plan["package_digest"]:
            raise GrantError("grant_plan_changed", 409)
        self.host.check_plan_settings(plan, package)
        manifest = package.manifest
        declarations = manifest.access.model_dump()
        for key in ("filesystem", "network", "secrets"):
            declarations[key] = [label(item, maximum=256) for item in declarations[key]]
        return {"name": label(plan["name"], maximum=256), "profile": manifest.profile,
            "start": plan["start"], "start_policy": plan["start_policy"],
            "resources": manifest.resources.model_dump(), "declared_access": declarations,
            "configuration_fields": sorted(plan["settings"]["configuration"]),
            "secret_reference_names": sorted(plan["settings"]["secret_references"]),
            "installed_bytes": sum(artifact.size for artifact in package.lock.artifacts)}

    def _upgrade_review_data(self, plan):
        # Only file/package work runs on the worker. Registry state and consent
        # deadlines are rechecked on their owning event-loop thread afterwards.
        from .registry import Slot
        try:
            inspected = self.host.inspect_upgrade_plan(plan["plan_id"])
            if inspected["plan_digest"] != plan_digest(plan):
                raise ValueError()
            old = Slot(**plan["old"])
            package = self.host.package(old)
            settings = self.host.package_settings(old, package)

            def metadata(manifest, saved_settings, digest, start, policy, installed_bytes):
                declarations = dict(manifest["access"])
                for key in ("filesystem", "network", "secrets"):
                    declarations[key] = [label(item, maximum=256) for item in declarations[key]]
                return {"name": label(plan["name"], maximum=256),
                    "package_id": label(manifest["package_id"], maximum=128),
                    "version": label(manifest["version"], maximum=128), "package_digest": digest,
                    "profile": manifest["profile"], "start": start, "start_policy": policy,
                    "resources": manifest["resources"], "declared_access": declarations,
                    "tools": sorted(label(tool["name"], maximum=256) for tool in manifest["tools"]),
                    "configuration_fields": sorted(saved_settings["configuration"]),
                    "secret_reference_names": sorted(saved_settings["secret_references"]),
                    "installed_bytes": installed_bytes, "public_port": plan["public_port"]}

            current = metadata(package.manifest.model_dump(), settings, old.package_digest,
                old.desired_state == "running", old.start_policy, sum(item.size for item in package.lock.artifacts))
            current["generation"] = old.generation
            candidate = metadata(inspected["package"], plan["settings"], plan["package_digest"],
                plan["start"], plan["start_policy"], inspected["installed_bytes"])
            candidate["expected_generation"] = plan["expected_generation"]
            effects = {"slot_id": plan["slot_id"], "current": current, "candidate": candidate,
                "data_policy": plan["data_policy"], "rollback_policy": plan["rollback_policy"],
                "validation": "Initialize and validate the replacement using a private clone of the app data. "
                    "Automatic rollback is allowed before exposure; preserve the selected data after exposure."}
            return inspected, effects
        except (ValueError, TypeError, KeyError):
            raise GrantError("grant_plan_changed", 409) from None

    def _upgrade_effects(self, plan, *, verified=None):
        inspected, effects = self._upgrade_review_data(plan) if verified is None else verified
        try:
            self.host.validate_upgrade_review(plan["plan_id"], inspected=inspected)
            if inspected["plan_digest"] != plan_digest(plan):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise GrantError("grant_plan_changed", 409) from None
        return effects

    def _validate_action(self, lease, frame):
        allowed = {"version", "type", "kind", "plan_id", "plan_digest", "idempotency_key"}
        if (set(frame) - allowed or not allowed - {"idempotency_key"} <= set(frame)
                or frame["kind"] not in {"preparation", "resolution", *PACKAGE_ACTIONS} or lease.base is None):
            raise GrantError("authorization_invalid_request", 400)
        base = self.requests[lease.base]
        self.guard(base["grant_id"])
        if "plan" not in base.get("selected_scopes", []):
            raise GrantError("grant_scope", 403)
        kind = frame["kind"]
        if kind not in PACKAGE_ACTIONS:
            identity(frame.get("idempotency_key"))
        elif "idempotency_key" in frame:
            raise GrantError("authorization_invalid_request", 400)
        plan = self._action_plan(kind, frame["plan_id"])
        digest = self._action_digest(kind, plan)
        if frame["plan_digest"] != digest:
            raise GrantError("grant_plan_changed", 409)
        return base, kind, plan, digest

    def _workflow_effects(self, kind, plan):
        from .preparation_inputs import parse_preparation_descriptor
        from .preparation_service_contracts import parse_policy
        from .preparation_resolution import ResolutionRequestV1
        if kind == "preparation":
            descriptor = parse_preparation_descriptor(plan["descriptor"])
            policy = parse_policy(plan["execution_policy"]).model_dump(mode="json")
            probe = policy.pop("service_probe", None)
            effects = {"recipe": descriptor.recipe.model_dump(mode="json"),
                "resources": descriptor.resources.model_dump(mode="json"),
                "declared_access": descriptor.access.model_dump(mode="json"), "execution_policy": policy,
                "import_id": plan["import_id"], "revision": plan["revision"], "source_revision": plan["source_revision"],
                "descriptor_digest": plan["descriptor_digest"]}
            if probe is not None:
                effects["service_probe"] = {key: value for key, value in probe.items() if key not in {"secret_reference"}}
        else:
            request = ResolutionRequestV1.model_validate(plan["request"])
            effects = {"execution_policy": request.policy.model_dump(mode="json"),
                "import_id": request.import_id, "revision": request.revision, "source_revision": request.source_revision,
                "candidate_id": request.candidate_id, "candidate_kind": request.candidate_kind,
                "candidate_digest": request.candidate_digest, "runtime": request.runtime.model_dump(mode="json"),
                "producer": request.producer.model_dump(mode="json")}
        for container in (effects.get("declared_access", {}), effects["execution_policy"]):
            for key in ("filesystem", "network", "secrets"):
                if key in container:
                    container[key] = [label(item, maximum=256) for item in container[key]]
        return effects

    def request_action(self, lease, frame, *, install_effects=None, upgrade_effects=None):
        base, kind, plan, digest = self._validate_action(lease, frame)
        current = self.requests.get(lease.current)
        review = {"family": kind, "plan_id": plan["plan_id"], "plan_digest": digest}
        if kind not in PACKAGE_ACTIONS:
            review["idempotency_key"] = frame["idempotency_key"]
        if kind == "install":
            review.update(package_digest=plan["package_digest"], slot_id=plan["slot_id"])
            review["install_effects"] = install_effects if install_effects is not None else self._install_effects(plan)
            review["access_disclosure"] = "Declared filesystem and network access is not enforced as a sandbox."
            review["summary"] = "Install the reviewed local package as an app."
        elif kind == "upgrade":
            review.update(package_digest=plan["package_digest"], slot_id=plan["slot_id"], expected_generation=plan["expected_generation"])
            review["upgrade_effects"] = upgrade_effects if upgrade_effects is not None else self._upgrade_effects(plan)
            review["access_disclosure"] = "Declared filesystem and network access is not enforced as a sandbox."
            review["summary"] = "Upgrade the existing app using the reviewed local package and saved settings."
        else:
            review["execution"] = {"operations": ["build", "probe"] if kind == "preparation" else ["resolve"]}
            review["effects"] = self._workflow_effects(kind, plan)
            review["access_disclosure"] = "Execution is bounded by the reviewed policy. Declared app access is not a sandbox."
            review["provider_disclosure"] = ("One synthetic request is sent to the reviewed loopback service."
                if "service_probe" in review["effects"] else
                "Dependency acquisition may contact the reviewed network destinations; no external model provider is selected.")
            review["summary"] = ("Run the reviewed preparation build and probe plan." if kind == "preparation"
                                 else "Run the reviewed dependency resolution plan.")
        if current is not None and current["status"] == "pending":
            if current["kind"] == kind and current.get("review") == review:
                return
            raise GrantError("authorization_request_pending", 409)
        self._rate(base["profile_id"])
        now = time.time()
        value = {key: base[key] for key in ("profile_id", "client", "label", "identity_mode", "policy_revision", "generation")}
        value.update(request_id=str(uuid.uuid4()), lease_id=lease.lease_id, kind=kind, status="pending", scopes=[],
                     scope_digest=hashlib.sha256(json.dumps(review, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                     created_at=now, expires_at=now + REQUEST_TTL, review=review)
        self.requests[value["request_id"]] = value
        lease.current = value["request_id"]

    def renew(self, lease, frame):
        if set(frame) != {"version", "type"} or lease.base is None:
            raise GrantError("authorization_invalid_request", 400)
        base = self.requests[lease.base]
        self.guard(base["grant_id"])
        if base["profile_id"] is None:
            raise GrantError("authorization_remember_consent_required", 403)
        profile = self._profile(base["profile_id"])
        if not profile["approved"] or not set(base["selected_scopes"]) <= set(profile["scopes"]):
            raise GrantError("authorization_policy_changed", 409)
        current = self.requests.get(lease.current)
        if current is not None and current["status"] == "pending":
            raise GrantError("authorization_request_pending", 409)
        # Rotate through the same ACK protocol; old authority ends on activation.
        value = {key: base[key] for key in ("profile_id", "client", "label", "identity_mode", "policy_revision", "generation")}
        now = time.time()
        value.update(request_id=str(uuid.uuid4()), lease_id=lease.lease_id, kind="base", status="pending",
                     scopes=base["selected_scopes"], scope_digest=scope_digest(base["selected_scopes"]), created_at=now,
                     expires_at=now + REQUEST_TTL, previous_base=base["request_id"])
        self.requests[value["request_id"]] = value
        lease.current = value["request_id"]
        self._prepare(value, value["scopes"])

    def _deny(self, value, status):
        value["status"] = status
        value.pop("handoff", None)
        if value.get("grant_id") is not None:
            self._revoke_credential(value["grant_id"])

    def _revoke_credential(self, grant_id):
        record = self.credentials[grant_id]
        record["active"] = 0
        with self.host.registry.transaction():
            self.db.execute("UPDATE authorization_credentials SET active=0 WHERE grant_id=?", (grant_id,))
        ledger = self.host.grants if record["kind"] in LEDGER_ACTIONS else self.host.workflow_grants
        try:
            ledger.revoke(grant_id)
        except GrantError as error:
            if error.code != "grant_not_found":
                raise

    def _settle_deadlines(self, lease):
        now = time.time()
        for value in self.requests.values():
            if value["lease_id"] != lease.lease_id:
                continue
            if value["status"] == "pending" and now >= min(value["expires_at"], value.get("handoff_expires_at", float("inf"))):
                self._deny(value, "expired")
            elif value["status"] == "approved":
                try:
                    self.guard(value["grant_id"])
                except GrantError as error:
                    self._deny(value, "expired" if error.code == "grant_expired" else "revoked")

    def status(self, lease):
        self._settle_deadlines(lease)
        value = self.requests.get(lease.current)
        if value is None:
            return {"version": 1, "status": "not_requested", "generation": self.generation}
        result = {"version": 1, **self.summary(value, details=False)}
        if lease.base is not None:
            result["base_status"] = self.requests[lease.base]["status"]
        if value.get("handoff") is not None and not value["handoff_sent"]:
            value["handoff_sent"] = True
            result["handoff"] = {**value.pop("handoff"), "request_id": value["request_id"], "kind": value["kind"]}
        return result

    @staticmethod
    def summary(value, *, details=True):
        fields = ("request_id", "profile_id", "client", "label", "identity_mode", "scopes", "scope_digest", "policy_revision",
                  "generation", "created_at", "expires_at", "kind", "status", "review", "selected_scopes", "grant_id")
        result = {key: value[key] for key in fields if key in value}
        if not details and "review" in result:
            result["review"] = {key: item for key, item in result["review"].items() if key not in {"install_effects", "upgrade_effects", "effects"}}
        return result

    def list_requests(self):
        self._prune()
        for lease in list(self.leases.values()):
            self._settle_deadlines(lease)
        return {"requests": [self.summary(value, details=False) for value in self.requests.values()
                             if value["status"] == "pending" and not value.get("grant_id")], "generation": self.generation}

    def _prune(self):
        # Terminal request history has no recovery capability. Retain only live
        # current/base bindings and a short owner-visible settlement receipt.
        now = time.time()
        protected = {item for lease in self.leases.values() for item in (lease.current, lease.base)}
        for request_id, value in list(self.requests.items()):
            if request_id not in protected and value["status"] not in {"pending", "approved"} and value["expires_at"] < now:
                self.requests.pop(request_id, None)
        for key in list(self.attempts):
            values = self.attempts[key]
            while values and values[0] <= now - 60:
                values.popleft()
            if not values:
                self.attempts.pop(key, None)
        for grant_id, record in list(self.credentials.items()):
            if record["expires_at"] <= now and record["lease_id"] not in self.leases:
                self.credentials.pop(grant_id, None)
                self.db.execute("DELETE FROM authorization_credentials WHERE grant_id=?", (grant_id,))

    def disconnect(self, lease_id):
        lease = self.leases.get(lease_id)
        if lease is None:
            return
        lease.closed = True
        for value in self.requests.values():
            if value["lease_id"] == lease_id and value["status"] in {"pending", "approved"}:
                self._deny(value, "revoked")
        lease.writer.close()

    def list_sessions(self):
        result = []
        for record in self.credentials.values():
            if record["kind"] != "base" or not record["active"]:
                continue
            try:
                self.guard(record["grant_id"])
            except GrantError:
                continue
            lease = self.leases[record["lease_id"]]
            value = self.requests[lease.base]
            result.append({"session_id": record["grant_id"], "profile_id": record["profile_id"], "expires_at": record["expires_at"],
                           "label": value["label"], "client_name": value["client"]["name"],
                           "identity_mode": "account_scoped_integration", "generation": self.generation})
        return {"sessions": result}

    async def owner_request(self, method, path, payload):
        upgrade_review = None
        parts = path.removeprefix("/v1/authorization/").split("/")
        if (len(parts) == 2 and parts[0] == "requests" and method == "GET"
                or len(parts) == 3 and parts[0] == "requests" and parts[2] == "approve" and method == "POST"):
            value = self.requests.get(parts[1])
            if (value is not None and value["kind"] == "upgrade" and value["status"] == "pending"
                    and value.get("grant_id") is None):
                value, _ = self._bound_request(parts[1])
                plan = self._action_plan("upgrade", value["review"]["plan_id"])
                upgrade_review = await self.host.static_work(self._upgrade_review_data, plan)
        return self.owner_route(method, path, payload, upgrade_review=upgrade_review)

    def owner_route(self, method, path, payload, *, upgrade_review=None):
        suffix = path.removeprefix("/v1/authorization/").split("/")
        if suffix == ["profiles"]:
            if method == "GET":
                return {"profiles": list(self.profiles.values())}
            if method == "POST":
                return self.create_profile(payload)
        if suffix == ["requests"] and method == "GET":
            return self.list_requests()
        if suffix == ["sessions"] and method == "GET":
            return self.list_sessions()
        if len(suffix) == 2 and suffix[0] == "requests" and method == "GET":
            identity(suffix[1])
            value = self.requests.get(suffix[1])
            if value is None:
                raise GrantError("authorization_request_unavailable", 404)
            if value["kind"] != "base":
                value, _ = self._bound_request(suffix[1])
                current = self._action_plan(value["kind"], value["review"]["plan_id"])
                if self._action_digest(value["kind"], current) != value["review"]["plan_digest"]:
                    raise GrantError("grant_plan_changed", 409)
                if value["kind"] == "upgrade" and self._upgrade_effects(current, verified=upgrade_review) != value["review"]["upgrade_effects"]:
                    raise GrantError("grant_plan_changed", 409)
            return {"request": self.summary(value)}
        if len(suffix) == 3 and method == "POST":
            family, object_id, action = suffix
            identity(object_id)
            if family == "requests" and action == "approve":
                return self.approve(object_id, payload, upgrade_review=upgrade_review)
            if payload != {}:
                raise GrantError("authorization_invalid_request", 400)
            if family == "requests" and action == "deny":
                value, _ = self._bound_request(object_id)
                self._deny(value, "denied")
                return {"request": self.summary(value)}
            if family == "profiles" and action == "revoke":
                profile = {**self._profile(object_id), "revoked_at": time.time(), "updated_at": time.time(),
                           "revision": self._profile(object_id)["revision"] + 1, "approved": False}
                self._save_profile(profile)
                for value in self.requests.values():
                    if value["profile_id"] == object_id and value["status"] in {"pending", "approved"}:
                        self._deny(value, "revoked")
                return {"profile": profile}
            if family == "sessions" and action == "disconnect":
                record = self.credentials.get(object_id)
                if record is None or record["kind"] != "base":
                    raise GrantError("authorization_session_unavailable", 404)
                self.disconnect(record["lease_id"])
                return {"session_id": object_id, "status": "revoked"}
        raise GrantError("authorization_invalid_request", 400)

    async def close(self):
        self.closing = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        path = socket_path(self.host.root, "authorization") if self.server is not None else None
        if path is not None:
            try:
                checked_socket(path)
                details = path.lstat()
                if (details.st_dev, details.st_ino) == self.listener_identity:
                    path.unlink()
            except (OSError, ValueError):
                pass
        (self.host.root / "authorization-bootstrap.json").unlink(missing_ok=True)
        for lease_id in list(self.leases):
            self.disconnect(lease_id)
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
