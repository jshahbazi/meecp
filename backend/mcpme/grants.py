"""Owner-issued, expiring management capabilities; plaintext stays in private files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

from .private import check_private_file, ensure_private_directory, read_private_json, write_private_json
from .registry import Registry, RegistryError, _uuid

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
IDENTITY = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
LIFECYCLE = {"start", "stop", "restart"}
MAX_GRANTS = 4096


class GrantError(ValueError):
    def __init__(self, code: str, status: int = 403):
        self.code, self.status = code, status
        super().__init__(code)


def read_credential(path: Path) -> dict:
    """Read only a private grant file. Never fall back to owner credentials."""
    try:
        path = Path(path).absolute()
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError()
        value = read_private_json(path, max_bytes=1024)
        if set(value) != {"schema_version", "grant_id", "token"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError()
        _uuid(value["grant_id"])
        if not isinstance(value["token"], str) or not TOKEN.fullmatch(value["token"]):
            raise ValueError()
        return value
    except Exception:
        raise GrantError("grant_credential_unavailable", 401) from None


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class GrantStore:
    """Shares the registry transaction so apply and one-operation binding are atomic."""

    def __init__(self, registry: Registry):
        self.registry = registry
        self.root = ensure_private_directory(registry.state_root / "grants")
        self.db = registry._connection
        # Uncommitted credential files confer no authority. Reclaim only known,
        # private UUID files; never follow or remove unexpected filesystem entries.
        for path in self.root.iterdir():
            if not re.fullmatch(IDENTITY + r"\.json", path.name):
                continue
            row = self.db.execute("SELECT revoked FROM management_grants WHERE grant_id=?", (path.stem,)).fetchone()
            if row is None or row["revoked"]:
                try:
                    check_private_file(path)
                    path.unlink()
                except Exception:
                    pass

    def _row(self, grant_id: str):
        _uuid(grant_id)
        row = self.db.execute("SELECT * FROM management_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise GrantError("grant_not_found", 404)
        return row

    def _active(self, grant_id: str):
        row = self._row(grant_id)
        if row["revoked"]:
            raise GrantError("grant_revoked", 401)
        if time.time() >= row["expires_at"]:
            raise GrantError("grant_expired", 401)
        return row

    def inspect(self, grant_id: str) -> dict:
        row = self._row(grant_id)
        scope = json.loads(row["scope"])
        return {"grant_id": row["grant_id"], "mode": row["mode"], "created_at": row["created_at"],
                "expires_at": row["expires_at"], "revoked": bool(row["revoked"]),
                "expired": time.time() >= row["expires_at"], "operation_id": row["operation_id"],
                "credential_file": str(self.root / (grant_id + ".json")),
                **{key: value for key, value in scope.items() if key != "slot_digests"}}

    def list(self) -> dict:
        return {"grants": [self.inspect(row[0]) for row in self.db.execute(
            "SELECT grant_id FROM management_grants ORDER BY created_at DESC LIMIT 100")],
            "total": self.db.execute("SELECT count(*) FROM management_grants").fetchone()[0]}

    def _reclaim_credential(self, grant_id: str) -> None:
        path = self.root / (grant_id + ".json")
        try:
            if path.exists():
                check_private_file(path)
                path.unlink()
        except Exception:
            pass

    def issue(self, payload: dict, *, plan: dict | None = None) -> dict:
        if not isinstance(payload, dict) or payload.get("mode") not in {"read", "prepare", "lifecycle", "install"}:
            raise GrantError("invalid_grant", 400)
        mode = payload["mode"]
        allowed = {"mode", "ttl_seconds"}
        scope = {}
        if mode == "install":
            allowed.add("plan_id")
            if (plan is None or payload.get("plan_id") != plan["plan_id"]
                    or type(payload.get("ttl_seconds", 900)) is not int or payload.get("ttl_seconds", 900) != 900):
                raise GrantError("invalid_grant", 400)
            scope = {"plan_id": plan["plan_id"], "plan_digest": plan_digest(plan),
                     "package_digest": plan["package_digest"], "slot_ids": [plan["slot_id"]], "actions": ["apply"]}
            ttl = 900
        else:
            ttl = payload.get("ttl_seconds", 3600)
            if type(ttl) is not int or not 60 <= ttl <= 86400:
                raise GrantError("invalid_grant", 400)
            if mode == "lifecycle":
                allowed |= {"slot_ids", "actions"}
                slots, actions = payload.get("slot_ids"), payload.get("actions")
                if (not isinstance(slots, list) or not 1 <= len(slots) <= 16
                        or not all(isinstance(value, str) for value in slots) or len(set(slots)) != len(slots)
                        or not isinstance(actions, list) or not actions or not all(isinstance(value, str) for value in actions)
                        or len(set(actions)) != len(actions) or set(actions) - LIFECYCLE):
                    raise GrantError("invalid_grant", 400)
                scope = {"slot_ids": slots, "actions": actions,
                         "slot_digests": {value: self.registry.get_slot(value).package_digest for value in slots}}
        if set(payload) - allowed:
            raise GrantError("invalid_grant", 400)
        grant_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        path = self.root / (grant_id + ".json")
        retired = []
        try:
            with self.registry.transaction():
                if self.db.execute("SELECT count(*) FROM management_grants").fetchone()[0] >= MAX_GRANTS:
                    # Retire inactive history under capacity pressure. Removing
                    # its hash cannot restore authority to a copied credential.
                    retired = [row[0] for row in self.db.execute(
                        "SELECT grant_id FROM management_grants WHERE revoked=1 OR expires_at<=?", (time.time(),))]
                    self.db.executemany("DELETE FROM management_grants WHERE grant_id=?", [(value,) for value in retired])
                    if self.db.execute("SELECT count(*) FROM management_grants").fetchone()[0] >= MAX_GRANTS:
                        raise GrantError("grant_limit", 409)
                # Write the file before committing the record. If file publication
                # fails, no capability becomes active; crash leftovers fail closed.
                write_private_json(path, {"schema_version": 1, "grant_id": grant_id, "token": token})
                created = time.time()
                self.db.execute("INSERT INTO management_grants VALUES (?,?,?,?,?,?,?,?)", (
                    grant_id, hashlib.sha256(token.encode()).hexdigest(), mode, created, created + ttl,
                    0, json.dumps(scope, sort_keys=True, separators=(",", ":")), None))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        for retired_id in retired:
            self._reclaim_credential(retired_id)
        return self.inspect(grant_id)

    def authenticate(self, token: str) -> dict:
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise GrantError("unauthorized", 401)
        row = self.db.execute("SELECT grant_id FROM management_grants WHERE token_hash=?", (
            hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise GrantError("unauthorized", 401)
        self._active(row[0])
        return {"grant_id": row[0]}

    def revoke(self, grant_id: str) -> dict:
        with self.registry.transaction():
            self._row(grant_id)
            self.db.execute("UPDATE management_grants SET revoked=1 WHERE grant_id=?", (grant_id,))
        # Revocation already holds if a credential was copied or unlink fails.
        self._reclaim_credential(grant_id)
        return self.inspect(grant_id)

    def _slot_allowed(self, row, slot_id: str) -> bool:
        if row["mode"] in {"read", "prepare"}:
            return True
        scope = json.loads(row["scope"])
        if slot_id not in scope.get("slot_ids", []):
            return False
        if row["mode"] == "install":
            if row["operation_id"] is None:
                return False
            try:
                return self.registry.get_slot(slot_id).package_digest == scope["package_digest"]
            except RegistryError:
                return False
        try:
            return self.registry.get_slot(slot_id).package_digest == scope["slot_digests"][slot_id]
        except RegistryError:
            return False

    def filter_slots(self, grant_id: str, slots: list[dict]) -> list[dict]:
        row = self._active(grant_id)
        return [slot for slot in slots if self._slot_allowed(row, slot["slot_id"])]

    def authorize_route(self, grant_id: str, method: str, path: str, payload: dict | None = None) -> None:
        row = self._active(grant_id)
        mode, scope = row["mode"], json.loads(row["scope"])
        if method == "GET" and path in {"/v1/capabilities", "/v1/slots"}:
            return
        if method == "GET" and path == "/v1/status" and mode in {"read", "prepare"}:
            return
        if method == "GET" and re.fullmatch(r"/v1/slots/" + IDENTITY + r"(?:/details)?", path):
            if self._slot_allowed(row, path.split("/")[3]):
                return
        if method == "GET" and re.fullmatch(r"/v1/operations/" + IDENTITY, path):
            operation = self.registry.get_operation(path.split("/")[3])
            if mode in {"read", "prepare"} or (mode == "install" and operation.id == row["operation_id"]):
                return
            if mode == "lifecycle" and self._slot_allowed(row, operation.slot_id):
                return
        if method == "GET" and re.fullmatch(r"/v1/plans/" + IDENTITY, path):
            if mode in {"read", "prepare"} or path.split("/")[3] == scope.get("plan_id"):
                return
        if mode in {"read", "prepare"}:
            if method == "GET" and (path in {"/v1/recipes", "/v1/recipes/schema", "/v1/preparations", "/v1/resolutions"}
                    or re.fullmatch(r"/v1/(?:preparations|resolutions)/" + IDENTITY, path)):
                return
            if method == "POST" and path in {"/v1/recipes/inspect", "/v1/preparations/inspect-source", "/v1/preparations/form"}:
                return
            if method == "GET" and (path == "/v1/imports" or re.fullmatch(r"/v1/imports/" + IDENTITY, path)):
                return
            if method == "POST" and re.fullmatch(r"/v1/imports/" + IDENTITY + r"/export", path):
                return
        if mode == "prepare" and method == "POST":
            if path in {"/v1/preparations/plan", "/v1/preparations/plan-form", "/v1/resolutions/plan"}:
                return
            if path in {"/v1/imports", "/v1/install/plan"} or re.fullmatch(
                    r"/v1/imports/" + IDENTITY + r"/(?:revise|attach|cancel|expire)", path):
                return
        if method == "POST" and path == "/v1/install/apply" and mode == "install":
            if isinstance(payload, dict) and set(payload) == {"plan_id"} and payload["plan_id"] == scope["plan_id"]:
                return
        if mode == "lifecycle" and method == "POST" and re.fullmatch(
                r"/v1/slots/" + IDENTITY + r"/(?:start|stop|restart)", path):
            if path.split("/")[4] in scope["actions"] and self._slot_allowed(row, path.split("/")[3]):
                return
        raise GrantError("grant_scope", 403)

    def validate_apply(self, grant_id: str, plan: dict) -> None:
        row = self._active(grant_id)
        scope = json.loads(row["scope"])
        if row["mode"] != "install" or scope.get("plan_id") != plan["plan_id"]:
            raise GrantError("grant_scope", 403)
        if scope["plan_digest"] != plan_digest(plan):
            raise GrantError("grant_plan_changed", 409)

    def bind_operation(self, grant_id: str, plan: dict, operation) -> None:
        """Must run inside the same registry transaction as operation creation."""
        if not self.db.in_transaction:
            raise GrantError("grants_unavailable", 503)
        self.validate_apply(grant_id, plan)
        row = self._row(grant_id)
        if (operation.action != "install" or operation.slot_id != plan["slot_id"]
                or operation.details.get("plan_id") != plan["plan_id"]
                or operation.details.get("package_digest") != plan["package_digest"]
                or operation.details.get("package_path") != plan["package_path"]
                or row["operation_id"] not in {None, operation.id}):
            raise GrantError("grant_scope", 403)
        self.db.execute("UPDATE management_grants SET operation_id=? WHERE grant_id=?", (operation.id, grant_id))
